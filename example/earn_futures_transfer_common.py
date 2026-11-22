"""余币宝活期与经典 USDT 永续账户划转的公共流程。"""
import hashlib
import json
import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from gate_api import ApiClient, CreateUniLend, EarnUniApi, FuturesApi, Transfer, WalletApi
from gate_api.exceptions import GateApiException

try:
    from .auto_grid_futures_generic import StrategyProcessLock, load_env_file, make_configuration
except ImportError:
    from auto_grid_futures_generic import StrategyProcessLock, load_env_file, make_configuration

TIMEOUT = (5, 15)
STATE_DIR = Path(__file__).resolve().parent / ".transfer_state"


def transfer_account_id(config):
    return hashlib.sha256((config.host + config.key).encode()).hexdigest()[:16]


def transfer_account_lock(config):
    return StrategyProcessLock(STATE_DIR / f"account-{transfer_account_id(config)}.lock")


def positive_decimal(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("金额/利率必须是大于零的有限数值")
    return amount


def spot_available(client):
    # 本项目 SDK 没有 list_spot_accounts；使用同一个 ApiClient 完成签名及反序列化。
    rows = client.call_api(
        "/spot/accounts", "GET", {}, [("currency", "USDT")], {},
        response_type="object", auth_settings=["apiv4"],
        _return_http_data_only=True, _request_timeout=TIMEOUT,
    )
    return sum((Decimal(row["available"]) for row in rows
                if row["currency"] == "USDT"), Decimal("0"))


def save_state(path, state, stage, **values):
    state.update(values, stage=stage)
    # 先落盘再发资金请求；临时文件+替换避免中断留下半份 JSON。
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def archive_state(path):
    backup = path.with_name(f"{path.stem}.{time.time_ns()}.bak{path.suffix}")
    path.rename(backup)
    return backup


def wait_until(check, message, timeout, poll_interval):
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(message + "；进度已保存，使用同一 STATE_FILE 重启继续查询")
        time.sleep(min(poll_interval, remaining))


def check_transfer_detail(result, tx_id, amount, source, destination):
    """接受字段大小写差异；仍核对流水号、币种、金额和方向。"""
    def normalize(value):
        return str(value if value is not None else "").strip().lower()

    expected = {"tx_id": str(tx_id), "currency": "usdt", "amount": amount,
                "from_account": source, "to_account": destination, "settle": "usdt"}
    differences = []
    for field, wanted in expected.items():
        actual = getattr(result, field, None)
        normalized = normalize(actual)
        if field == "settle" and not normalized:
            # 已指定 USDT，且同一回执的流水号、金额及账户方向仍需匹配。
            continue
        if field == "amount":
            try:
                matches = Decimal(normalized) == wanted
            except InvalidOperation:
                matches = False
        else:
            matches = normalized == wanted
        if not matches:
            differences.append(f"{field}: 预期={wanted!s}，返回={actual!r}")
    if differences:
        raise RuntimeError(f"划转回执不一致，tx_id={tx_id}：" + "；".join(differences))
    return normalize(result.status)


def run_steps(client, direction, amount, min_rate, path, *, wait_timeout, poll_interval,
              retry_rejected_transfer=False, retry_rejected_lend=False, reset=False):
    """每个状态只提交一次；提交结果未知时要求人工核对，而不是再次发送 POST。"""
    earn, wallet = EarnUniApi(client), WalletApi(client)
    identity = {
        "direction": direction, "amount": str(amount),
        "host": client.configuration.host,
        "key_fingerprint": hashlib.sha256(client.configuration.key.encode()).hexdigest(),
    }
    state = None
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if any(state.get(key) != value for key, value in identity.items()
               if not (reset and key == "amount")):
            raise RuntimeError("STATE_FILE 属于其他金额、账户密钥或方向；先完成原任务，新任务使用新的文件名")
        if reset:
            backup = archive_state(path)
            print(f"原进度已归档：{backup}；开始新任务")
            state = None
    if state is not None:
        if retry_rejected_lend:
            if (direction != "futures_to_earn" or not state.get("tx_id")
                    or state["stage"] not in {"lend_submitting", "lend_ready"}):
                raise RuntimeError("仅能重试已确认划入现货、且上次申购被明确拒绝的原任务")
            print("仅重试余币宝申购，不重新划转")
            save_state(path, state, "lend_ready")
        if retry_rejected_transfer:
            if state["stage"] not in {"transfer_submitting", "transfer_ready"} or state.get("tx_id"):
                raise RuntimeError("仅能重试尚无 tx_id、停在划转步骤的原任务")
            if state["stage"] == "transfer_submitting":
                print("按已确认拒绝处理：仅重试划转，不重复余币宝操作")
                save_state(path, state, "transfer_ready")
    else:
        if retry_rejected_transfer or retry_rejected_lend:
            raise RuntimeError("没有原任务的进度文件，无法重试已拒绝的请求")
        account = FuturesApi(client).list_futures_accounts("usdt", _request_timeout=TIMEOUT)
        if account.margin_mode != 0:
            raise RuntimeError("此示例要求经典合约账户；统一账户请使用对应的账户资金流程")
        rate = None
        if direction == "futures_to_earn":
            product = earn.get_uni_currency("USDT", _request_timeout=TIMEOUT)
            if amount < Decimal(product.min_lend_amount):
                raise ValueError(f"余币宝最低申购数量为 {product.min_lend_amount} USDT")
            rate = positive_decimal(product.min_rate if min_rate is None else min_rate)
            if not Decimal(product.min_rate) <= rate <= Decimal(product.max_rate):
                raise ValueError("MIN_RATE 不在当前产品允许的小时利率范围内")
            # 可划出额度以服务端划转校验为准，不能把 total 当成可划出金额。
            if Decimal(account.available) < amount:
                raise ValueError(f"合约可用资金不足：available={account.available}")
        state = dict(identity, spot_before=str(spot_available(client)),
                     min_rate=format(rate, "f") if rate is not None else None)
        save_state(path, state, "created")

    if state["stage"] == "done":
        print(f"该任务已完成，不重复执行：{path}")
        return
    allowed = {"created", "redeem_submitting", "redeem_wait", "transfer_ready",
               "transfer_submitting", "transfer_wait", "transfer_failed",
               "lend_ready", "lend_submitting"}
    if state["stage"] not in allowed:
        raise RuntimeError(f"无法识别进度状态：{state['stage']}")
    if state["stage"].endswith("_submitting") or state["stage"] == "transfer_failed":
        raise RuntimeError(
            f"任务停在 {state['stage']}，上次提交结果需要核对。"
            f"请检查余币宝/划转记录及 {path}，确认结果后再恢复；不要直接删文件重跑"
        )

    if direction == "earn_to_futures" and state["stage"] == "created":
        save_state(path, state, "redeem_submitting")
        earn.create_uni_lend(CreateUniLend(currency="USDT", amount=str(amount), type="redeem"),
                             _request_timeout=TIMEOUT)
        save_state(path, state, "redeem_wait")
    if state["stage"] == "redeem_wait":
        target = Decimal(state["spot_before"]) + amount
        print(f"赎回请求已接受，等待现货可用 USDT 达到 {target}")
        wait_until(lambda: spot_available(client) >= target, "等待赎回到账超时",
                   wait_timeout, poll_interval)
        save_state(path, state, "transfer_ready")
    if direction == "futures_to_earn" and state["stage"] == "created":
        save_state(path, state, "transfer_ready")
    if state["stage"] == "transfer_ready":
        source, destination = (("spot", "futures") if direction == "earn_to_futures"
                               else ("futures", "spot"))
        if source == "spot" and spot_available(client) < amount:
            raise RuntimeError("现货可用余额不足，保留当前进度，补足余额后重启")
        save_state(path, state, "transfer_submitting")
        try:
            result = wallet.transfer(Transfer(currency="USDT", amount=str(amount),
                                             _from=source, to=destination, settle="usdt"),
                                     _request_timeout=TIMEOUT)
        except GateApiException as exc:
            if (exc.status == 403 and exc.label == "FORBIDDEN"
                    and "does not have wallet permission" in (exc.message or "").lower()):
                save_state(path, state, "transfer_ready")
                raise RuntimeError("Gate 拒绝划转：API Key 缺少 Wallet 读写权限。"
                                   "开启后用原进度文件重试即可。") from exc
            raise
        if result.tx_id is None:
            raise RuntimeError("划转响应未返回 tx_id，请核对实际划转记录")
        save_state(path, state, "transfer_wait", tx_id=str(result.tx_id))
        print(f"已提交 {source} → {destination}：{amount} USDT，tx_id={result.tx_id}")
    if state["stage"] == "transfer_wait":
        def transferred():
            result = wallet.get_transfer(state["tx_id"], _request_timeout=TIMEOUT)
            source, destination = (("spot", "futures") if direction == "earn_to_futures"
                                   else ("futures", "spot"))
            status = check_transfer_detail(result, state["tx_id"], amount, source, destination)
            if status == "fail":
                save_state(path, state, "transfer_failed")
                raise RuntimeError(f"划转失败：tx_id={state['tx_id']}")
            if status not in {"pending", "success"}:
                raise RuntimeError(f"未知划转状态：{result.status}")
            return status == "success"
        wait_until(transferred, "等待划转确认超时", wait_timeout, poll_interval)
        print(f"已确认划转成功：tx_id={state['tx_id']}")
        save_state(path, state, "done" if direction == "earn_to_futures" else "lend_ready")
    if state["stage"] == "lend_ready":
        # 钱包回执已确认这笔划转；这里只检查申购所需的可用余额。
        wait_until(lambda: spot_available(client) >= amount, "现货可用 USDT 不足以申购",
                   wait_timeout, poll_interval)
        # Decimal 的 str() 可能产生 1.1E-7；旧进度中的利率也要重新格式化。
        rate_text = format(positive_decimal(state["min_rate"]), "f")
        print(f"开始申购余币宝：{amount} USDT，最低小时利率={rate_text}")
        save_state(path, state, "lend_submitting", min_rate=rate_text)
        try:
            earn.create_uni_lend(CreateUniLend(currency="USDT", amount=str(amount),
                                             type="lend", min_rate=rate_text),
                                 _request_timeout=TIMEOUT)
        except GateApiException as exc:
            if (exc.status == 400 and exc.label == "INVALID_PARAM_VALUE"
                    and "`min_rate`" in (exc.message or "")):
                save_state(path, state, "lend_ready")
            raise
        save_state(path, state, "done")
    print(f"完成：{direction}，{amount} USDT；进度文件：{path}")


def execute_transfer(direction, amount, state_file, *, min_rate=None,
                     wait_timeout=3900, poll_interval=2, execute=False,
                     retry_rejected_transfer=False, retry_rejected_lend=False, reset=False):
    if direction not in {"earn_to_futures", "futures_to_earn"}:
        raise ValueError("不支持的划转方向")
    if sum((reset, retry_rejected_transfer, retry_rejected_lend)) > 1:
        raise ValueError("重置和重试选项不能同时使用")
    amount = positive_decimal(amount)
    if amount != amount.quantize(Decimal("0.00000001")):
        raise ValueError("划转金额最多支持 8 位小数")
    amount = Decimal(format(amount.normalize(), "f"))
    if wait_timeout <= 0 or not 1 <= poll_interval <= 60:
        raise ValueError("等待时间必须大于零，查询间隔需在 1～60 秒之间")
    load_env_file()
    print(f"流程：{'余币宝赎回 → 现货 → USDT 永续' if direction == 'earn_to_futures' else 'USDT 永续 → 现货 → 余币宝申购'}，金额={amount} USDT")
    if not execute:
        print("当前为预览。运行时添加 --execute 后执行真实划转。")
        return
    config = make_configuration(os.getenv("GATE_HOST", "https://api.gateio.ws/api/v4").strip(),
                                os.environ["GATE_API_KEY"], os.environ["GATE_API_SECRET"])
    path = Path(state_file).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 两个方向共用账户锁，防止它们同时修改现货余额基线。
    lock = transfer_account_lock(config)
    lock.acquire()
    try:
        with ApiClient(config) as client:
            run_steps(client, direction, amount, min_rate, path,
                      wait_timeout=wait_timeout, poll_interval=poll_interval,
                      retry_rejected_transfer=retry_rejected_transfer,
                      retry_rejected_lend=retry_rejected_lend, reset=reset)
    finally:
        lock.release()
