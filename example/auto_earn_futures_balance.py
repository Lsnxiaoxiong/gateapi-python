"""每分钟检查经典 USDT 永续可用余额，按阈值在合约和余币宝间调度资金。"""
import json
import os
import time
from decimal import Decimal

from gate_api import ApiClient, EarnUniApi, FuturesApi
from gate_api.exceptions import GateApiException

try:
    from . import earn_futures_transfer_common as flow
except ImportError:
    import earn_futures_transfer_common as flow

# 修改为 True 后启用实际申赎和划转；默认只读监控，避免误操作真实资金。
EXECUTE = True
INTERVAL_SECONDS = 30
UPPER_BALANCE = Decimal("200")
LOWER_BALANCE = Decimal("100")
TRANSFER_AMOUNT = Decimal("50")
TO_EARN = "futures_to_earn"
TO_FUTURES = "earn_to_futures"


def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def pick_direction(available):
    if available > UPPER_BALANCE:
        return TO_EARN
    if available < LOWER_BALANCE:
        return TO_FUTURES
    return None


def pending_job(state_dir):
    active = []
    for direction in (TO_EARN, TO_FUTURES):
        path = state_dir / f"{direction}.json"
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if state["stage"] != "done":
                active.append((direction, path, state))
    if len(active) > 1:
        raise RuntimeError("发现两笔未完成的自动划转，请先核对进度文件")
    return active[0] if active else None


def earn_can_redeem(client):
    rows = EarnUniApi(client).list_user_uni_lends(currency="USDT", _request_timeout=flow.TIMEOUT)
    usdt = [row for row in rows if row.currency == "USDT"]
    if not usdt:
        log("余币宝查询未返回 USDT 活期记录，本轮跳过")
        return False
    if any(Decimal(row.frozen_amount or "0") > 0 for row in usdt):
        log("余币宝已有赎回待到账，本轮不再提交赎回")
        return False
    total = Decimal("0")
    used_fallback = False
    for row in usdt:
        value = row.current_amount
        if value is None or value == "":
            # Gate 实际响应可能只返回 amount。它是出借数量，仅作初筛。
            value = row.amount
            used_fallback = True
        if value is None or value == "":
            log("余币宝记录缺少 current_amount 和 amount，本轮跳过")
            return False
        quantity = Decimal(value)
        if not quantity.is_finite() or quantity < 0:
            raise ValueError("Gate 返回的余币宝 USDT 数量无效")
        total += quantity
    if used_fallback:
        log(f"余币宝 current_amount 缺失，使用 amount 初筛：{total} USDT")
    if total < TRANSFER_AMOUNT:
        log(f"余币宝 USDT 初筛数量={total}，不足 {TRANSFER_AMOUNT}，本轮跳过")
        return False
    return True


def check_once(client, state_dir, execute):
    account = FuturesApi(client).list_futures_accounts("usdt", _request_timeout=flow.TIMEOUT)
    if account.margin_mode != 0:
        raise RuntimeError("自动划转要求经典 USDT 永续账户")
    available = Decimal(account.available)
    if not available.is_finite():
        raise ValueError("Gate 返回的合约可用余额无效")
    log(f"USDT 永续可用余额={available}")

    job = pending_job(state_dir)
    if job:
        direction, path, state = job
        log(f"已有任务 {direction}，阶段={state['stage']}，进度={path}")
        if not execute:
            return
        if (direction == TO_EARN and state["stage"] in {"created", "transfer_ready"}
                and not state.get("tx_id") and available <= UPPER_BALANCE):
            # 尚未划出或已被明确拒绝；撤销本地计划，允许低余额时走补充流程。
            log(f"余额已不高于上限，归档尚未划出的计划：{flow.archive_state(path)}")
        else:
            # 每轮只查询一次等待条件，下一分钟使用同一进度继续。
            flow.run_steps(client, direction, Decimal(state["amount"]), None, path,
                           wait_timeout=0, poll_interval=INTERVAL_SECONDS)
            return

    direction = pick_direction(available)
    if direction is None:
        log(f"余额在 [{LOWER_BALANCE}, {UPPER_BALANCE}] 内，本轮不划转")
        return
    description = "合约划出并申购余币宝" if direction == TO_EARN else "赎回余币宝并补充合约"
    if not execute:
        log(f"预览：{description} {TRANSFER_AMOUNT} USDT")
        return
    if direction == TO_FUTURES and not earn_can_redeem(client):
        return
    path = state_dir / f"{direction}.json"
    log(f"开始：{description} {TRANSFER_AMOUNT} USDT")
    flow.run_steps(client, direction, TRANSFER_AMOUNT, None, path, reset=path.exists(),
                   wait_timeout=0, poll_interval=INTERVAL_SECONDS)


def monitor(client, state_dir, *, execute, once):
    while True:
        started = time.monotonic()
        try:
            check_once(client, state_dir, execute)
        except GateApiException as exc:
            log(f"Gate 拒绝请求：label={exc.label}，message={exc.message}")
            if once:
                return 1
        except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
            log(f"本轮未完成：{exc}")
            if once:
                return 1
        except Exception as exc:
            # SDK 还可能抛出连接/HTTP 库异常；保留进度，下轮继续核对。
            log(f"本轮异常：{type(exc).__name__}；保留进度")
            if once:
                return 1
        if once:
            return 0
        time.sleep(max(0, INTERVAL_SECONDS - (time.monotonic() - started)))


def main():
    flow.load_env_file()
    key, secret = os.getenv("GATE_API_KEY"), os.getenv("GATE_API_SECRET")
    if not key or not secret:
        raise RuntimeError("请先配置 GATE_API_KEY 和 GATE_API_SECRET")
    config = flow.make_configuration(
        os.getenv("GATE_HOST", "https://api.gateio.ws/api/v4").strip(), key, secret,
    )
    state_dir = flow.STATE_DIR / "auto" / flow.transfer_account_id(config)
    lock = flow.transfer_account_lock(config) if EXECUTE else None
    try:
        if lock:
            lock.acquire()
            state_dir.mkdir(parents=True, exist_ok=True)
        with ApiClient(config) as client:
            log(f"启动{'实际划转' if EXECUTE else '只读监测'}，进度目录={state_dir}")
            return monitor(client, state_dir, execute=EXECUTE, once=False)
    except KeyboardInterrupt:
        log("已停止，现有进度保留")
        return 0
    finally:
        if lock:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
