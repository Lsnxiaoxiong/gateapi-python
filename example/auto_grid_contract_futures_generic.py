"""Gate USDT 永续合约 SQLite 网格。

本文件复用 ``auto_grid_futures_generic.py`` 已验证的网格状态机、数据库结构、
GridChecker 和订单恢复逻辑，只把交易接口替换为 Gate FuturesApi。

重要差异：
- ``contract`` 是合约代码（例如 SOXL_USDT），不是现货代码 SOXLG_USDT；
- 数据库中的数量字段在本文件中表示“合约张数”；
- 买单为正张数，止盈卖单为负张数且 ``reduce_only=True``；
- 当前实现只支持单向持仓模式，防止止盈单在双向模式下产生歧义；
- 实盘需要 GATE_LIVE_CONFIRM=RUN_REAL_FUTURES_DB_GRID，并在 .env 中设置
  GATE_API_KEY、GATE_API_SECRET、GATE_USER_ID。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api.exceptions import GateApiException

try:
    from . import auto_grid_futures_generic as core
except ImportError:
    import auto_grid_futures_generic as core


LIVE_CONFIRM_TEXT = "RUN_REAL_FUTURES_DB_GRID"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = SCRIPT_DIR / "futures_grid_orders.sqlite3"
DEFAULT_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"


@dataclass(frozen=True)
class FuturesContractSpec:
    grid_spec: core.PairSpec
    multiplier: Decimal
    leverage_min: Decimal
    leverage_max: Decimal


class _MappedJob:
    """把 SDK 异步请求结果转换成现货状态机可消费的统一订单对象。"""

    def __init__(self, job, mapper):
        self.job = job
        self.mapper = mapper

    def get(self):
        return [self.mapper(item) for item in self.job.get()]


class FuturesGridApi:
    """将 FuturesApi 适配到原网格状态机使用的最小订单接口。"""

    def __init__(self, api: FuturesApi, settle: str):
        self.api = api
        self.settle = settle

    @staticmethod
    def _normalise(order):
        raw_size = Decimal(str(getattr(order, "size", None) or "0"))
        raw_left = Decimal(str(getattr(order, "left", None) or "0"))
        amount = abs(raw_size)
        left = abs(raw_left)
        finish_as = str(getattr(order, "finish_as", "") or "")
        raw_status = str(getattr(order, "status", "") or "")
        if raw_status == "open":
            status = "open"
        elif raw_status == "finished" and finish_as == "filled":
            status = "closed"
        elif raw_status == "finished":
            status = "cancelled"
        else:
            status = raw_status
        return SimpleNamespace(
            id=str(getattr(order, "id", "") or ""),
            text=str(getattr(order, "text", "") or ""),
            currency_pair=str(getattr(order, "contract", "") or ""),
            account="futures",
            side="buy" if raw_size > 0 else "sell",
            status=status,
            finish_as=finish_as,
            amount=core.decimal_text(amount),
            left=core.decimal_text(left),
            filled_amount=core.decimal_text(max(Decimal("0"), amount - left)),
            price=str(getattr(order, "price", "0") or "0"),
        )

    def list_tickers(self, currency_pair=None, **kwargs):
        return self.api.list_futures_tickers(
            self.settle, contract=currency_pair, **kwargs
        )

    def create_order(self, body, **kwargs):
        contracts = Decimal(str(body.amount))
        signed_size = contracts if body.side == "buy" else -contracts
        futures_order = FuturesOrder(
            contract=body.currency_pair,
            size=core.decimal_text(signed_size),
            price=str(body.price),
            tif="gtc",
            text=body.text,
            reduce_only=body.side == "sell",
        )
        result = self.api.create_futures_order(
            self.settle, futures_order, **kwargs
        )
        return self._normalise(result)

    def list_orders(self, pair, status, account=None, **kwargs):
        async_req = bool(kwargs.get("async_req"))
        result = self.api.list_futures_orders(
            self.settle, status, contract=pair,
            **{key: value for key, value in kwargs.items() if key != "account"},
        )
        if async_req:
            return _MappedJob(result, self._normalise)
        return [self._normalise(item) for item in result]

    def get_order(self, order_id, pair, account=None, **kwargs):
        return self._normalise(
            self.api.get_futures_order(self.settle, order_id, **kwargs)
        )

    def cancel_order(self, order_id, pair, account=None, **kwargs):
        return self._normalise(
            self.api.cancel_futures_order(self.settle, order_id, **kwargs)
        )

    def get_directional_position_size(self, pair: str, direction: str) -> Decimal:
        """读取指定方向的实际合约张数；单向/双向持仓模式均可使用。"""
        def read() -> Decimal:
            if getattr(self, "position_mode", "single") == "dual":
                positions = self.api.get_dual_mode_position(
                    self.settle, pair, _request_timeout=core.NETWORK_TIMEOUT
                )
                sizes = [Decimal(str(getattr(item, "size", "0") or "0")) for item in positions]
                return (
                    sum((max(Decimal("0"), size) for size in sizes), Decimal("0"))
                    if direction == "long" else
                    sum((max(Decimal("0"), -size) for size in sizes), Decimal("0"))
                )
            position = self.api.get_position(
                self.settle, pair, _request_timeout=core.NETWORK_TIMEOUT
            )
            size = Decimal(str(getattr(position, "size", "0") or "0"))
            return max(Decimal("0"), size) if direction == "long" else max(Decimal("0"), -size)

        return core.call_with_retry("读取合约实际持仓", read)


def make_configuration(host: str, key=None, secret=None) -> Configuration:
    configuration = Configuration(host=host, key=key, secret=secret)
    configuration.retries = False
    proxy = os.getenv("GATE_PROXY", "").strip()
    if proxy:
        configuration.proxy = proxy
    return configuration


def get_contract_spec(
    api: FuturesApi, settle: str, contract: str
) -> FuturesContractSpec:
    item = core.call_with_retry(
        "读取合约规格",
        lambda: api.get_futures_contract(
            settle, contract, _request_timeout=core.NETWORK_TIMEOUT
        ),
    )
    if str(item.name) != contract or str(item.status) != "trading":
        raise RuntimeError(f"合约 {contract} 不存在或当前不可交易")
    multiplier = core.decimal_value("quanto_multiplier", item.quanto_multiplier)
    min_size = core.decimal_value("order_size_min", item.order_size_min)
    size_step = min_size if bool(item.enable_decimal) else Decimal("1")
    grid_spec = core.PairSpec(
        base_currency=contract.split("_")[0],
        quote_currency=settle.upper(),
        amount_step=size_step,
        price_step=core.decimal_value("order_price_round", item.order_price_round),
        min_base_amount=min_size,
        min_quote_amount=Decimal("0"),
        max_base_amount=Decimal(str(item.order_size_max or "Infinity")),
        max_quote_amount=Decimal("Infinity"),
    )
    return FuturesContractSpec(
        grid_spec=grid_spec,
        multiplier=multiplier,
        leverage_min=Decimal(str(item.leverage_min or "1")),
        leverage_max=Decimal(str(item.leverage_max or "1")),
    )


def calculate_contract_size(
    margin: Decimal,
    leverage: Decimal,
    price: Decimal,
    spec: FuturesContractSpec,
) -> Decimal:
    """将每格保证金换算成合约张数，向下对齐合约数量步长。"""
    raw = margin * leverage / (price * spec.multiplier)
    size = core.round_to_step(
        raw, spec.grid_spec.amount_step, ROUND_DOWN
    )
    if size <= 0 or size < spec.grid_spec.min_base_amount:
        minimum_notional = (
            spec.grid_spec.min_base_amount * price * spec.multiplier
        )
        minimum_margin = minimum_notional / leverage
        raise ValueError(
            f"单格保证金不足：当前 {core.decimal_text(margin)} USDT，"
            f"此价格和杠杆下至少约需 {core.decimal_text(minimum_margin)} USDT"
        )
    if size > spec.grid_spec.max_base_amount:
        raise ValueError("单格张数超过合约最大下单数量")
    return size


def desired_buy_orders(
    reference: Decimal,
    current: Decimal,
    step_pct: Decimal,
    count: int,
    margin: Decimal,
    leverage: Decimal,
    spec: FuturesContractSpec,
) -> list[core.PlannedOrder]:
    return [
        core.PlannedOrder(
            price, calculate_contract_size(margin, leverage, price, spec)
        )
        for price in core.desired_buy_prices(
            reference, current, step_pct, count, spec.grid_spec.price_step
        )
    ]


def futures_strategy_code(
    settle: str,
    contract: str,
    reference: Decimal,
    step_pct: Decimal,
    take_profit_pct: Decimal,
    margin: Decimal,
    leverage: Decimal,
    grid_tag: str,
    max_unclosed_per_grid: int = 2,
) -> str:
    exposure_suffix = (
        "" if max_unclosed_per_grid == 2
        else f"|max-unclosed={max_unclosed_per_grid}"
    )
    raw = (
        f"{grid_tag}|futures|{settle}|{contract}|{reference}|{step_pct}|"
        f"{take_profit_pct}|{margin}|{leverage}{exposure_suffix}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def configure_futures_account(
    api: FuturesApi, settle: str, contract: str, leverage: Decimal
) -> str:
    account = core.call_with_retry(
        "读取合约账户",
        lambda: api.list_futures_accounts(
            settle, _request_timeout=core.NETWORK_TIMEOUT
        ),
    )
    position_mode = str(getattr(account, "position_mode", "") or "")
    in_dual_mode = bool(getattr(account, "in_dual_mode", False))
    dual_mode = in_dual_mode or position_mode in {"dual", "dual_plus"}
    core.call_with_retry(
        "设置合约杠杆",
        lambda: (
            api.update_dual_mode_position_leverage(
                settle, contract, core.decimal_text(leverage),
                _request_timeout=core.NETWORK_TIMEOUT,
            )
            if dual_mode else api.update_position_leverage(
                settle, contract, core.decimal_text(leverage),
                _request_timeout=core.NETWORK_TIMEOUT,
            )
        ),
    )
    return "dual" if dual_mode else (position_mode or "single")


TP_EXTERNALLY_CLOSED = "仓位已外部平仓"
POSITION_EXTERNALLY_CLOSED = "仓位已外部平仓"


def reconcile_futures_position(
    api: FuturesGridApi,
    store: core.GridStore,
    pair: str,
    code: str,
    direction: str,
) -> Decimal:
    """以 Gate 实际仓位为准清理失联周期，并返回可用于新平仓单的数量。"""
    # GridChecker、WebSocket 监听器可能同时触发持仓对账。对同一个数据库串行
    # 执行完整对账，避免两个线程按同一份旧快照重复分配实际仓位。
    with store.lock:
        reconcile_lock = getattr(store, "_futures_position_reconcile_lock", None)
        if reconcile_lock is None:
            reconcile_lock = threading.Lock()
            store._futures_position_reconcile_lock = reconcile_lock
    with reconcile_lock:
        return _reconcile_futures_position_locked(
            api, store, pair, code, direction
        )


def _reconcile_futures_position_locked(
    api: FuturesGridApi,
    store: core.GridStore,
    pair: str,
    code: str,
    direction: str,
) -> Decimal:
    actual_size = api.get_directional_position_size(pair, direction)
    rows = [
        row for row in store.active(pair, code)
        if row["buy_status"] == core.BUY_FILLED and not row["completed"]
    ]
    expected_size = sum((Decimal(row["remaining_size"]) for row in rows), Decimal("0"))
    if actual_size <= 0:
        if not rows:
            return Decimal("0")
        for row in rows:
            if row["tp_order_id"] and row["tp_status"] == core.TP_OPEN:
                core.cancel_api_order(api, pair, row["tp_order_id"], ignore_absent=True)
        store.execute(
            "UPDATE grid_cycles SET remaining_size='0',tp_order_id=NULL,"
            "tp_order_text=NULL,tp_status=?,position_status=?,completed=1,"
            "updated_at=? WHERE currency_pair=? AND strategy_code=? "
            "AND completed=0 AND buy_status=?",
            (
                TP_EXTERNALLY_CLOSED, POSITION_EXTERNALLY_CLOSED, time.time(),
                pair, code, core.BUY_FILLED,
            ),
        )
        print(
            f"[{core.now_text()}] 合约实际{('多' if direction == 'long' else '空')}仓为0，"
            f"已关闭 {len(rows)} 条数据库残留周期"
        )
        return Decimal("0")
    if expected_size > actual_size:
        print(
            f"[{core.now_text()}] 合约实际{('多' if direction == 'long' else '空')}仓="
            f"{actual_size}，数据库待平数量={expected_size}；"
            "开始清理超额数据库周期"
        )
        # 合约仓位不能按每次开仓逐笔区分。优先把真实仓位分配给已经存在
        # Gate 止盈单的周期，其次保留较早记录；超出实际仓位的记录必须完成，
        # 否则 activity_count 会一直把这些空仓记录当成网格占位。
        priority = {
            core.TP_OPEN: 0,
            core.TP_SUBMITTING: 1,
        }
        rows.sort(key=lambda row: (
            priority.get(row["tp_status"], 2), row["created_at"], row["id"]
        ))
        capacity = actual_size
        closed_count = 0
        adjusted_count = 0
        for original in rows:
            row = store.one(int(original["id"]))
            if row is None or row["completed"]:
                continue
            recorded_size = Decimal(row["remaining_size"])
            if capacity >= recorded_size:
                capacity -= recorded_size
                continue

            # 若该记录已有大于剩余真实仓位的止盈单，先撤单，避免调整数据库
            # 后 Gate 上仍保留超量 reduce-only 委托。
            if row["tp_status"] == core.TP_OPEN and row["tp_order_id"]:
                result = core.cancel_api_order(
                    api, pair, row["tp_order_id"], ignore_absent=True
                )
                if result is not None:
                    core.mark_tp_result(store, row, result)
                row = store.one(int(original["id"]))
                if row is None or row["completed"]:
                    continue
                recorded_size = Decimal(row["remaining_size"])

            keep_size = min(capacity, recorded_size)
            if keep_size > 0:
                store.execute(
                    "UPDATE grid_cycles SET remaining_size=?,tp_order_id=NULL,"
                    "tp_order_text=NULL,tp_status=?,position_status=?,updated_at=? "
                    "WHERE id=? AND completed=0",
                    (
                        core.decimal_text(keep_size), core.TP_WAIT_PLACE,
                        core.POSITION_WAIT_TP_ORDER, time.time(), row["id"],
                    ),
                )
                adjusted_count += 1
                capacity -= keep_size
                continue

            store.execute(
                "UPDATE grid_cycles SET remaining_size='0',tp_order_id=NULL,"
                "tp_order_text=NULL,tp_status=?,position_status=?,completed=1,"
                "updated_at=? WHERE id=? AND completed=0",
                (
                    TP_EXTERNALLY_CLOSED, POSITION_EXTERNALLY_CLOSED,
                    time.time(), row["id"],
                ),
            )
            closed_count += 1
        print(
            f"[{core.now_text()}] 仓位对账完成：关闭超额周期={closed_count}，"
            f"调整剩余数量周期={adjusted_count}，数据库待平数量已对齐至="
            f"{actual_size - capacity}"
        )
    return actual_size


class FuturesOrderListener(core.OrderListener):
    """订阅合约订单，并复用核心的20格范围、最近10单止盈维护逻辑。"""

    channel_name = "futures.orders"
    account_name = "futures"

    def __init__(
        self,
        stop,
        store,
        api,
        pair,
        code,
        spec,
        take_profit_pct,
        buy_prefix,
        sell_prefix,
        ws_url,
        price_state=None,
        reference=None,
        step_pct=None,
        take_profit_grid_count=None,
        take_profit_count=None,
        *,
        user_id: str,
    ):
        # 父类仅传入旧版也支持的基础参数，避免合约文件与核心文件部署不同步时
        # 因新增窗口参数产生 ``takes 11 positional arguments``。
        super().__init__(
            stop, store, api, pair, code, spec, take_profit_pct,
            buy_prefix, sell_prefix, ws_url,
        )
        self.price_state = price_state
        self.reference = reference
        self.step_pct = step_pct
        self.take_profit_grid_count = take_profit_grid_count
        self.take_profit_count = take_profit_count
        self.user_id = user_id

    def _maintain_take_profits_after_buy_fill(self) -> None:
        if (
            self.price_state is None
            or self.reference is None
            or self.step_pct is None
            or self.take_profit_grid_count is None
            or self.take_profit_count is None
        ):
            return
        current = self.price_state.get()
        available_size = reconcile_futures_position(
            self.api, self.store, self.pair, self.code, "long"
        )
        if available_size <= 0:
            return
        upper = core.take_profit_window_upper(
            self.reference, current, self.step_pct,
            self.take_profit_grid_count, self.spec,
        )
        core.reconcile_take_profits(
            self.api, self.store, self.pair, self.code, current, upper,
            self.spec, self.sell_prefix, self.take_profit_count, available_size,
        )

    @staticmethod
    def _normalise_event(event: dict) -> dict:
        size = Decimal(str(event.get("size") or "0"))
        left = Decimal(str(event.get("left") or "0"))
        amount = abs(size)
        remaining = abs(left)
        finish_as = str(event.get("finish_as") or "")
        raw_status = str(event.get("status") or "")
        if raw_status == "open":
            status = "open"
        elif raw_status == "finished" and finish_as == "filled":
            status = "closed"
        elif raw_status == "finished":
            status = "cancelled"
        else:
            status = raw_status
        return {
            "id": str(event.get("id_string") or event.get("id") or ""),
            "text": str(event.get("text") or ""),
            "currency_pair": str(event.get("contract") or ""),
            "account": "futures",
            "side": "buy" if size > 0 else "sell",
            "status": status,
            "finish_as": finish_as,
            "amount": core.decimal_text(amount),
            "left": core.decimal_text(remaining),
            "filled_amount": core.decimal_text(
                max(Decimal("0"), amount - remaining)
            ),
            "price": str(event.get("price") or "0"),
        }

    def _receive_event(self, event: dict) -> None:
        super()._receive_event(self._normalise_event(event))

    def _subscription(self, api_key: str, api_secret: str) -> str:
        timestamp = int(time.time())
        channel = "futures.orders"
        event = "subscribe"
        signing = f"channel={channel}&event={event}&time={timestamp}"
        signature = hmac.new(
            api_secret.encode("utf-8"), signing.encode("utf-8"), hashlib.sha512
        ).hexdigest()
        return json.dumps({
            "time": timestamp,
            "channel": channel,
            "event": event,
            "payload": [self.user_id, self.pair],
            "auth": {"method": "api_key", "KEY": api_key, "SIGN": signature},
        })

    def run(self, api_key: str, api_secret: str) -> None:
        print(
            f"[{core.now_text()}] FuturesOrderListener 准备订阅 "
            f"{self.pair} futures.orders"
        )
        super().run(api_key, api_secret)


def run_grid(
    contract: str,
    reference_price,
    margin_amount_per_order,
    leverage="2",
    settle: str = "usdt",
    grid_tag: str = "futgrid01",
    buy_client_order_prefix: str = "fb",
    sell_client_order_prefix: str = "ft",
    buy_grid_count: int = 10,
    take_profit_count: int = 10,
    take_profit_grid_count: int = 20,
    max_unclosed_per_grid: int = 2,
    grid_step_pct="0.003",
    take_profit_pct="0.003",
    startup_mode: str = "resume",
    database_path: Path = DEFAULT_DATABASE,
    poll_interval: float = 2.5,
    max_cycles: Optional[int] = None,
) -> None:
    contract = str(contract).strip().upper().replace("-", "_")
    settle = str(settle).strip().lower()
    startup_mode = str(startup_mode).strip().lower()
    if startup_mode not in {"resume", "reset"}:
        raise ValueError("startup_mode 只能是 resume 或 reset")
    if buy_grid_count <= 0 or take_profit_count <= 0 or take_profit_grid_count <= 0:
        raise ValueError("买格、止盈单数量和止盈网格区间必须大于 0")
    if max_unclosed_per_grid <= 0:
        raise ValueError("max_unclosed_per_grid 必须大于 0")
    if poll_interval < 0.5:
        raise ValueError("poll_interval 不能小于 0.5 秒")

    reference = core.decimal_value("reference_price", reference_price)
    margin = core.decimal_value("margin_amount_per_order", margin_amount_per_order)
    leverage_value = core.decimal_value("leverage", leverage)
    step_pct = core.decimal_value("grid_step_pct", grid_step_pct)
    tp_pct = core.decimal_value("take_profit_pct", take_profit_pct)
    if step_pct >= 1 or tp_pct >= 1:
        raise ValueError("网格间距和止盈比例必须小于 1")

    core.load_env_file()
    host = os.getenv("GATE_HOST", "https://api.gateio.ws/api/v4").strip()
    ws_url = os.getenv("GATE_FUTURES_WS_URL", DEFAULT_WS_URL).strip()
    live = os.getenv("GATE_LIVE_CONFIRM", "") == LIVE_CONFIRM_TEXT
    public_client = ApiClient(make_configuration(host))
    try:
        public_futures = FuturesApi(public_client)
        contract_spec = get_contract_spec(public_futures, settle, contract)
        if not contract_spec.leverage_min <= leverage_value <= contract_spec.leverage_max:
            raise ValueError(
                f"杠杆必须在 {contract_spec.leverage_min} 到 "
                f"{contract_spec.leverage_max} 之间"
            )
        public_api = FuturesGridApi(public_futures, settle)
        current = core.get_last_price(public_api, contract)
        desired = desired_buy_orders(
            reference, current, step_pct, buy_grid_count, margin,
            leverage_value, contract_spec,
        )
        if not live:
            print(
                f"合约安全预览：{contract}，结算币={settle.upper()}，"
                f"当前价={current}，每张={contract_spec.multiplier} "
                f"{contract_spec.grid_spec.base_currency}，数据库={database_path}"
            )
            for order in desired:
                notional = order.size * order.price * contract_spec.multiplier
                print(
                    f"买价={order.price} 张数={order.size} "
                    f"名义价值={core.decimal_text(notional)} USDT "
                    f"计划止盈={core.take_profit_price(order.price, tp_pct, contract_spec.grid_spec)}"
                )
            print(f"实盘需 GATE_LIVE_CONFIRM={LIVE_CONFIRM_TEXT}")
            return
    finally:
        public_client.close()

    api_key = core.required_env("GATE_API_KEY")
    api_secret = core.required_env("GATE_API_SECRET")
    user_id = core.required_env("GATE_USER_ID")
    process_lock = core.StrategyProcessLock(
        Path(f"{Path(database_path)}.{settle}.{contract}.lock")
    )
    clients = [
        ApiClient(make_configuration(host, api_key, api_secret))
        for _ in range(4)
    ]
    futures_apis = [FuturesApi(client) for client in clients]
    apis = [FuturesGridApi(api, settle) for api in futures_apis]
    try:
        process_lock.acquire()
        store = core.GridStore(Path(database_path))
    except Exception:
        for client in clients:
            client.close()
        process_lock.release()
        raise

    code = futures_strategy_code(
        settle, contract, reference, step_pct, tp_pct, margin,
        leverage_value, grid_tag, max_unclosed_per_grid,
    )
    price_state = core.PriceState(current)
    stop = threading.Event()
    grid_checker = core.GridChecker(
        stop, store, apis[1], contract, code, price_state, reference, step_pct,
        buy_grid_count, take_profit_grid_count, take_profit_count,
        contract_spec.grid_spec, sell_client_order_prefix,
        max_unclosed_per_grid,
        take_profit_available_size=lambda: reconcile_futures_position(
            apis[1], store, contract, code, "long"
        ),
    )
    order_listener = FuturesOrderListener(
        stop, store, apis[2], contract, code, contract_spec.grid_spec, tp_pct,
        buy_client_order_prefix, sell_client_order_prefix, ws_url,
        price_state, reference, step_pct, take_profit_grid_count,
        take_profit_count,
        user_id=user_id,
    )
    state_reconciler = FuturesOrderListener(
        stop, store, apis[3], contract, code, contract_spec.grid_spec, tp_pct,
        buy_client_order_prefix, sell_client_order_prefix, ws_url,
        price_state, reference, step_pct, take_profit_grid_count,
        take_profit_count,
        user_id=user_id,
    )
    order_state_checker = core.OrderStateChecker(stop, state_reconciler)
    checker = threading.Thread(
        target=grid_checker.run, name=f"futures-grid-check-{contract}", daemon=True
    )
    listener = threading.Thread(
        target=order_listener.run, args=(api_key, api_secret),
        name=f"futures-order-listener-{contract}", daemon=True,
    )
    state_sync = threading.Thread(
        target=order_state_checker.run,
        name=f"futures-order-state-check-{contract}",
        daemon=True,
    )
    try:
        mode = configure_futures_account(
            futures_apis[0], settle, contract, leverage_value
        )
        for api in apis:
            api.position_mode = mode
        if startup_mode == "reset":
            core.reset_pair(apis[0], store, contract)
        else:
            conflicts = store.unfinished_other_strategies(contract, code)
            if conflicts:
                raise RuntimeError(
                    f"数据库中存在 {contract} 的其他未完成策略；"
                    "确认撤销旧策略后使用 startup_mode='reset'"
                )
        state_sync.start()
        listener.start()
        checker.start()
        print(
            f"[{core.now_text()}] 启动合约数据库网格：{contract}，"
            f"持仓模式={mode}，杠杆={leverage_value}，策略码={code}"
        )
        cycle = 0
        failures = 0
        while not stop.is_set() and (max_cycles is None or cycle < max_cycles):
            cycle += 1
            try:
                current = core.get_last_price(apis[0], contract)
                price_state.set(current)
                desired = desired_buy_orders(
                    reference, current, step_pct, buy_grid_count, margin,
                    leverage_value, contract_spec,
                )
                core.place_missing_buys(
                    apis[0], store, contract, code, desired, tp_pct,
                    contract_spec.grid_spec, False, buy_client_order_prefix,
                    max_unclosed_per_grid,
                )
                upper = core.take_profit_window_upper(
                    reference, current, step_pct, take_profit_grid_count,
                    contract_spec.grid_spec,
                )
                rows = store.active(contract, code)
                pending = sum(row["buy_status"] == core.WAIT_BUY for row in rows)
                open_tps = sum(row["tp_status"] == core.WAIT_TP for row in rows)
                waiting = sum(
                    row["buy_status"] == core.BUY_FILLED and row["tp_status"] in {
                        core.TP_NOT_SET, core.TP_WAIT_PLACE,
                        core.TP_SUBMITTING, core.TP_CANCELLED,
                    }
                    for row in rows
                )
                print(
                    f"[{core.now_text()}] 当前价={current}，"
                    f"买单={pending}/{buy_grid_count}，"
                    f"止盈单={open_tps}/{take_profit_count}，"
                    f"止盈区间=({current},{upper}]，等待止盈={waiting}，"
                    f"活动周期={len(rows)}"
                )
                failures = 0
            except Exception as exc:
                failures += 1
                detail = (
                    f"label={exc.label}, message={exc.message}"
                    if isinstance(exc, GateApiException) else str(exc)
                )
                print(
                    f"[{core.now_text()}] 合约主循环失败："
                    f"{type(exc).__name__}: {detail}"
                )
                if failures >= 5:
                    raise RuntimeError("连续 5 轮失败，自动停止合约网格") from exc
            if max_cycles is None or cycle < max_cycles:
                stop.wait(poll_interval)
    except KeyboardInterrupt:
        print("\n收到停止信号；数据库已保存，现有合约挂单不会自动撤销。")
    finally:
        stop.set()
        shutdown_timeout = (
            core.NETWORK_RETRY_ATTEMPTS * core.NETWORK_TIMEOUT[1] + 5
        )
        checker.join(timeout=shutdown_timeout)
        listener.join(timeout=shutdown_timeout)
        state_sync.join(timeout=shutdown_timeout)
        if checker.is_alive() or listener.is_alive() or state_sync.is_alive():
            print(
                f"[{core.now_text()}] 合约异步线程仍在退出中，"
                "暂不关闭其数据库和客户端"
            )
            clients[0].close()
        else:
            store.close()
            process_lock.release()
            for client in clients:
                client.close()


def run_SOXL() -> None:
    run_grid(
        contract="SOXL_USDT",
        reference_price="150",
        margin_amount_per_order="1.6",  # 每格保证金；名义仓位约为保证金×杠杆
        leverage="2",
        settle="usdt",
        buy_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,  # 检查现价上方20个网格，最多保留10个止盈单
        max_unclosed_per_grid=1,  # 单个网格价位最多保留的已成交未止盈周期数
        grid_step_pct="0.003",
        take_profit_pct="0.003",
        startup_mode="resume",
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_SOXL()
