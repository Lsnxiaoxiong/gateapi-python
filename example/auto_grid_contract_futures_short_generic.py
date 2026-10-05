"""Gate USDT 永续合约 做空（Short）SQLite 网格。

本文件参考 ``auto_grid_contract_futures_generic.py``（多头合约网格）编写，
把多空方向整体反转，实现**做空合约网格**：

- ``contract`` 是合约代码（例如 SOXL_USDT）；数据库数量字段表示“合约张数”。
- 做空开仓：在现价上方网格价位挂 **SELL** 单（负张数、``reduce_only=False``）开空；
- 做空平仓/止盈：开空成交后，在开空价下方挂 **BUY** 单（正张数、``reduce_only=True``）
  平掉空仓；
- 网格价位从参考价**向上**排列（``reference * (1+step)^index``）；
- 止盈窗口为**现价下方**：只维护落在 ``[lower, current)`` 区间的平仓单；
- 复用 ``auto_grid_futures_generic.py`` 的 SQLite 状态机（GridStore）、价格/小数工具、
  进程锁、REST 对账（OrderStateChecker）等方向无关基础设施；
- 与多头版本的关键区别：开仓是 sell、平仓是 buy，且 ``reduce_only`` 语义相反；
- 支持单向和双向持仓模式；启动时会按账户当前模式设置对应合约杠杆；
- 实盘需要 GATE_LIVE_CONFIRM=RUN_REAL_FUTURES_SHORT_DB_GRID，并在 .env 中设置
  GATE_API_KEY、GATE_API_SECRET、GATE_USER_ID。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from gate_api import ApiClient, Configuration, FuturesApi, FuturesOrder
from gate_api.exceptions import GateApiException

try:
    from . import auto_grid_futures_generic as core
except ImportError:
    import auto_grid_futures_generic as core

try:
    from . import auto_grid_contract_futures_generic as ref
except ImportError:
    import auto_grid_contract_futures_generic as ref


LIVE_CONFIRM_TEXT = "RUN_REAL_FUTURES_SHORT_DB_GRID"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = SCRIPT_DIR / "futures_short_grid_orders.sqlite3"
DEFAULT_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"

# 复用多头版本的合约规格读取、张数换算、账户配置和配置构建
FuturesContractSpec = ref.FuturesContractSpec
get_contract_spec = ref.get_contract_spec
calculate_contract_size = ref.calculate_contract_size
configure_futures_account = ref.configure_futures_account
make_configuration = ref.make_configuration
_MappedJob = ref._MappedJob


def short_futures_strategy_code(
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
    """生成包含做空方向的策略码，避免与同参数做多策略发生碰撞。"""
    exposure_suffix = (
        "" if max_unclosed_per_grid == 2
        else f"|max-unclosed={max_unclosed_per_grid}"
    )
    raw = (
        f"{grid_tag}|futures-short|{settle}|{contract}|{reference}|{step_pct}|"
        f"{take_profit_pct}|{margin}|{leverage}{exposure_suffix}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def resume_short_strategy_code(
    store: core.GridStore,
    pair: str,
) -> Optional[str]:
    """返回交易对唯一的未完成策略码，不再校对当前启动参数。"""
    rows = store.rows(
        "SELECT strategy_code,COUNT(*) AS cycle_count "
        "FROM grid_cycles WHERE currency_pair=? AND completed=0 "
        "GROUP BY strategy_code ORDER BY strategy_code",
        (pair,),
    )
    if not rows:
        return None
    if len(rows) > 1:
        details = "、".join(
            f"{row['strategy_code']}({row['cycle_count']}条)" for row in rows
        )
        raise RuntimeError(
            f"数据库中存在 {pair} 的多个未完成策略，无法确定应恢复哪一个："
            f"{details}；请先整理数据库或使用 startup_mode='reset'"
        )
    return str(rows[0]["strategy_code"])


# ---------------------------------------------------------------------------
# 做空方向的价格函数（与多头镜像：网格向上排列、止盈在开空价下方）
# ---------------------------------------------------------------------------

def short_raw_grid_price(reference: Decimal, step_pct: Decimal, index: int) -> Decimal:
    return reference * ((Decimal("1") + step_pct) ** index)


def short_grid_price(
    reference: Decimal, step_pct: Decimal, index: int, price_step: Decimal
) -> Decimal:
    return core.round_to_step(
        short_raw_grid_price(reference, step_pct, index), price_step, ROUND_HALF_UP
    )


def first_short_index(
    reference: Decimal, current: Decimal, step_pct: Decimal, price_step: Decimal
) -> int:
    """返回现价上方第一个锚定网格价格的 index（做空开仓位）。"""
    ratio = float(current / reference)
    step_ratio = float(Decimal("1") + step_pct)
    index = math.floor(math.log(ratio) / math.log(step_ratio)) + 1
    while short_grid_price(reference, step_pct, index, price_step) <= current:
        index += 1
    while short_grid_price(reference, step_pct, index - 1, price_step) > current:
        index -= 1
    return index


def desired_short_prices(
    reference: Decimal,
    current: Decimal,
    step_pct: Decimal,
    count: int,
    price_step: Decimal,
) -> list[Decimal]:
    """独立计算现价上方最近的锚定网格价格（做空开仓价位）。"""
    start = first_short_index(reference, current, step_pct, price_step)
    result: list[Decimal] = []
    index = start
    while len(result) < count:
        price = short_grid_price(reference, step_pct, index, price_step)
        index += 1
        if price <= 0:
            raise RuntimeError("网格价格小于等于 0")
        if result and result[-1] == price:
            continue
        result.append(price)
        if index - start > count * 100:
            raise RuntimeError("网格间距小于价格精度")
    return result


def short_take_profit_price(
    short_price: Decimal, take_profit_pct: Decimal, spec: core.PairSpec
) -> Decimal:
    """做空平仓价：开空价下方（低买平空）。"""
    return core.round_to_step(
        short_price * (Decimal("1") - take_profit_pct), spec.price_step, ROUND_DOWN
    )


def short_take_profit_window_lower(
    reference: Decimal,
    current: Decimal,
    step_pct: Decimal,
    grid_count: int,
    spec: core.PairSpec,
) -> Decimal:
    """返回当前价下方第 ``grid_count`` 个锚定网格的价格（做空平仓窗口下界）。

    做空网格价格随 index 增大而升高（``reference*(1+step)^index``）；
    ``first_above`` 是现价上方第一个开空价位（最小的 index），平仓窗口在现价
    下方，因此下界取 ``first_above - grid_count``（更小的 index = 更低价格）。
    """
    first_above = first_short_index(reference, current, step_pct, spec.price_step)
    return short_grid_price(
        reference, step_pct, first_above - grid_count, spec.price_step
    )


def desired_short_orders(
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
        for price in desired_short_prices(
            reference, current, step_pct, count, spec.grid_spec.price_step
        )
    ]


# ---------------------------------------------------------------------------
# 做空适配器：把 FuturesApi 适配到做空状态机使用的订单接口
# ---------------------------------------------------------------------------

class ShortFuturesGridApi:
    """与参考程序 FuturesGridApi 的区别：多空方向相反。

    - 开空（body.side == "sell"）→ 负张数，``reduce_only=False``（开空仓）；
    - 平仓/止盈（body.side == "buy"）→ 正张数，``reduce_only=True``（平空仓）。
    """

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
        signed_size = -contracts if body.side == "sell" else contracts
        futures_order = FuturesOrder(
            contract=body.currency_pair,
            size=core.decimal_text(signed_size),
            price=str(body.price),
            tif="gtc",
            text=body.text,
            reduce_only=body.side == "buy",  # 买入平空仓，卖出开空仓
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

    get_directional_position_size = ref.FuturesGridApi.get_directional_position_size


def reconcile_short_position(
    api: ShortFuturesGridApi,
    store: core.GridStore,
    pair: str,
    code: str,
) -> Decimal:
    """按 Gate 实际空仓清理数据库超额周期并返回可平仓张数。"""
    return ref.reconcile_futures_position(api, store, pair, code, "short")


def create_futures_limit_order(
    api: ShortFuturesGridApi,
    pair: str,
    side: str,
    price: Decimal,
    size: Decimal,
    prefix: str,
    code: str,
    order_text: Optional[str] = None,
):
    text = order_text or core.make_order_text(prefix, code)
    body = SimpleNamespace(
        currency_pair=pair,
        side=side,
        amount=size,
        price=price,
        text=text,
    )
    return api.create_order(body, _request_timeout=core.NETWORK_TIMEOUT)


# ---------------------------------------------------------------------------
# 做空下单 / 平仓逻辑
# ---------------------------------------------------------------------------

def place_missing_shorts(
    api: ShortFuturesGridApi,
    store: core.GridStore,
    pair: str,
    code: str,
    desired: list[core.PlannedOrder],
    tp_pct: Decimal,
    spec: core.PairSpec,
    prefix: str,
    max_unclosed_per_grid: int = 2,
) -> bool:
    """在现价上方补做空开仓单（SELL）。"""
    rows = store.active(pair, code)
    pending_prices = {
        Decimal(row["buy_price"]) for row in rows if row["buy_status"] == core.WAIT_BUY
    }
    for planned in desired:
        activity = store.activity_count(pair, code, planned.price)
        if activity >= max_unclosed_per_grid:
            print(
                f"[{core.now_text()}] {planned.price} 活动数量={activity}/"
                f"{max_unclosed_per_grid}（待成交+已成交未平仓），跳过新开空单"
            )
            continue
        if planned.price in pending_prices:
            continue
        tp = short_take_profit_price(planned.price, tp_pct, spec)
        order_text = core.make_order_text(prefix, code)
        reservation_id = store.reserve_buy_slot(
            pair, code, planned, tp, order_text, max_unclosed_per_grid
        )
        if reservation_id is None:
            print(
                f"[{core.now_text()}] {planned.price} 挂单前原子占位失败："
                f"活动数量已达到 {max_unclosed_per_grid}"
            )
            continue
        try:
            order = create_futures_limit_order(
                api, pair, "sell", planned.price, planned.size,
                prefix, code, order_text=order_text,
            )
        except GateApiException as exc:
            if core.retryable(exc):
                print(
                    f"[{core.now_text()}] 开空单请求结果不确定，保留网格占位等待订单同步："
                    f"价格={planned.price}，记录={reservation_id}"
                )
            else:
                store.release_buy_reservation(reservation_id)
            if exc.label in {"BALANCE_NOT_ENOUGH", "AUTO_BORROW_TOO_MUCH"}:
                print(f"[{core.now_text()}] 开空保证金不足，暂停本轮补单")
                return False
            raise
        except Exception:
            print(
                f"[{core.now_text()}] 开空单请求异常，保留网格占位等待订单同步："
                f"价格={planned.price}，记录={reservation_id}"
            )
            raise
        try:
            store.bind_reserved_buy(reservation_id, order)
        except Exception:
            core.cancel_api_order(api, pair, str(order.id))
            raise
        pending_prices.add(planned.price)
        print(
            f"[{core.now_text()}] 新增开空卖单并写库：价格={planned.price}，"
            f"张数={planned.size}，计划平仓={tp}，id={order.id}"
        )
    return True


def submit_short_take_profit(
    api: ShortFuturesGridApi,
    store: core.GridStore,
    row,
    pair: str,
    code: str,
    spec: core.PairSpec,
    prefix: str,
) -> bool:
    """为一个已成交开空单挂平仓买单；数据库抢占确保监听器和检查器不会重复下单。"""
    refreshed = store.one(int(row["id"]))
    if (
        refreshed is None
        or refreshed["completed"]
        or refreshed["buy_status"] != core.BUY_FILLED
        or refreshed["tp_order_id"]
    ):
        return False
    size = Decimal(refreshed["remaining_size"])
    price = Decimal(refreshed["tp_price"])
    notional = size * price
    if size < spec.min_base_amount or notional < spec.min_quote_amount:
        store.execute(
            "UPDATE grid_cycles SET tp_status=?,position_status=?,updated_at=? "
            "WHERE id=?",
            (core.TP_DUST, core.POSITION_WAIT_TP_ORDER, time.time(), refreshed["id"]),
        )
        print(
            f"[{core.now_text()}] 平仓剩余转为碎片：记录={refreshed['id']}，"
            f"数量={size}，金额={core.decimal_text(notional)} USDT"
        )
        return False
    if not store.claim_take_profit(int(refreshed["id"])):
        return False
    try:
        order = create_futures_limit_order(
            api, pair, "buy", price, size, prefix, code
        )
    except GateApiException as exc:
        message = str(exc.message or "").lower()
        if exc.label == "INVALID_PARAM_VALUE" and (
            "too small" in message or "minimum" in message
        ):
            store.execute(
                "UPDATE grid_cycles SET tp_status=?,position_status=?,updated_at=? "
                "WHERE id=?",
                (core.TP_DUST, core.POSITION_WAIT_TP_ORDER, time.time(), refreshed["id"]),
            )
            print(
                f"[{core.now_text()}] Gate拒绝过小平仓单，已转为碎片："
                f"记录={refreshed['id']}，数量={size}，金额="
                f"{core.decimal_text(notional)} USDT"
            )
            return False
        store.release_take_profit_claim(int(refreshed["id"]))
        if exc.label == "BALANCE_NOT_ENOUGH":
            print(f"[{core.now_text()}] 平仓保证金不足，暂不能挂平仓单：记录{refreshed['id']}")
        raise
    except Exception:
        store.release_take_profit_claim(int(refreshed["id"]))
        raise
    try:
        store.set_take_profit_open(int(refreshed["id"]), order)
    except Exception:
        core.cancel_api_order(api, pair, str(order.id))
        raise
    print(
        f"[{core.now_text()}] 已挂平仓买单：记录={refreshed['id']}，"
        f"买价={price}，数量={size}，id={order.id}"
    )
    return True


def reconcile_short_take_profits(
    api: ShortFuturesGridApi,
    store: core.GridStore,
    pair: str,
    code: str,
    current: Decimal,
    lower_price: Decimal,
    spec: core.PairSpec,
    prefix: str,
    limit: int,
    available_size: Optional[Decimal] = None,
) -> None:
    """维护现价下方的平仓单窗口：撤销窗口外的平仓单，为已开空仓位补平仓单。"""
    for row in store.active(pair, code):
        if (
            row["buy_status"] != core.BUY_FILLED
            or row["tp_status"] in {core.TP_OPEN, core.TP_SUBMITTING}
            or Decimal(row["remaining_size"]) <= 0
        ):
            continue
        size = Decimal(row["remaining_size"])
        price = Decimal(row["tp_price"])
        notional = size * price
        if size >= spec.min_base_amount and notional >= spec.min_quote_amount:
            continue
        if row["tp_status"] != core.TP_DUST:
            store.execute(
                "UPDATE grid_cycles SET tp_order_id=NULL,tp_order_text=NULL,"
                "tp_status=?,position_status=?,updated_at=? WHERE id=?",
                (
                    core.TP_DUST, core.POSITION_WAIT_TP_ORDER, time.time(), row["id"],
                ),
            )
            print(
                f"[{core.now_text()}] 平仓剩余转为碎片：记录={row['id']}，"
                f"数量={size}，金额={core.decimal_text(notional)} USDT，"
                f"最低金额={core.decimal_text(spec.min_quote_amount)} USDT"
            )

    # 做空平仓窗口：lower_price <= tp_price < current（现价下方）
    eligible = [
        row for row in store.active(pair, code)
        if row["buy_status"] == core.BUY_FILLED
        and row["tp_status"] in {
            core.TP_NOT_SET, core.TP_WAIT_PLACE, core.TP_CANCELLED, core.TP_PLANNED
        }
        and lower_price <= Decimal(row["tp_price"]) < current
        and Decimal(row["remaining_size"]) > 0
    ]
    eligible.extend(
        row for row in store.active(pair, code)
        if row["buy_status"] == core.BUY_FILLED
        and row["tp_status"] == core.TP_OPEN
        and lower_price <= Decimal(row["tp_price"]) < current
        and Decimal(row["remaining_size"]) > 0
    )
    # 距离现价最近的平仓价优先保留
    eligible.sort(key=lambda row: current - Decimal(row["tp_price"]))
    selected_ids = {row["id"] for row in eligible[:limit]}

    for row in store.active(pair, code):
        if row["tp_status"] != core.TP_OPEN or row["id"] in selected_ids:
            continue
        result = core.cancel_api_order(api, pair, row["tp_order_id"])
        core.mark_tp_result(store, row, result)
        print(
            f"[{core.now_text()}] 注销远端平仓单：买价={row['tp_price']}，"
            f"有效区间=[{lower_price}, {current})，id={row['tp_order_id']}"
        )

    if not selected_ids:
        return
    placeholders = ",".join("?" for _ in selected_ids)
    rows = store.rows(
        f"SELECT * FROM grid_cycles WHERE id IN ({placeholders}) "
        "ORDER BY CAST(tp_price AS REAL) DESC",
        tuple(selected_ids),
    )
    remaining_capacity = available_size
    if remaining_capacity is not None:
        remaining_capacity = max(Decimal("0"), remaining_capacity)
        for row in rows:
            if row["tp_status"] == core.TP_OPEN and not row["completed"]:
                remaining_capacity = max(
                    Decimal("0"),
                    remaining_capacity - Decimal(row["remaining_size"]),
                )
    for row in rows:
        if row["tp_status"] in {core.TP_OPEN, core.TP_SUBMITTING} or row["completed"]:
            continue
        size = Decimal(row["remaining_size"])
        if remaining_capacity is not None and size > remaining_capacity:
            print(
                f"[{core.now_text()}] 实际空仓可用数量={remaining_capacity}，"
                f"暂不新增平仓单：记录={row['id']}，需要={size}"
            )
            continue
        if submit_short_take_profit(api, store, row, pair, code, spec, prefix):
            if remaining_capacity is not None:
                remaining_capacity -= size


def cancel_orphan_short_covers(
    api: ShortFuturesGridApi,
    store: core.GridStore,
    pair: str,
    code: str,
    cover_prefix: str,
    opened: list,
) -> int:
    """撤销属于当前策略、但没有已成交开空数据库记录的平仓买单。"""
    text_prefix = f"t-{cover_prefix}{code}"
    linked_ids = store.linked_take_profit_ids(pair, code)
    cancelled = 0
    for order in opened:
        order_id = str(order.id)
        if (
            str(getattr(order, "side", "")) != "buy"
            or not str(getattr(order, "text", "")).startswith(text_prefix)
            or order_id in linked_ids
        ):
            continue
        create_time = float(getattr(order, "create_time", 0) or 0)
        if create_time and time.time() - create_time < core.ORPHAN_ORDER_GRACE_SECONDS:
            continue
        if order_id in store.linked_take_profit_ids(pair, code):
            continue
        core.cancel_api_order(api, pair, order_id)
        cancelled += 1
        print(
            f"[{core.now_text()}] 撤销无关联开空的平仓买单："
            f"id={order_id}，价格={order.price}，数量={order.amount}"
        )
    return cancelled


# ---------------------------------------------------------------------------
# 做空订单监听器（futures.orders：sell=开空，buy=平仓）
# ---------------------------------------------------------------------------

class ShortOrderListener:
    """订阅 ``futures.orders``，实时推进开空与平仓状态，REST 仅作断线校验。

    与多头版本差异：**sell 事件=开空（open leg），buy 事件=平仓（close leg）**。
    """

    channel_name = "futures.orders"
    account_name = "futures"

    def __init__(
        self,
        stop: threading.Event,
        store: core.GridStore,
        api: ShortFuturesGridApi,
        pair: str,
        code: str,
        spec: core.PairSpec,
        take_profit_pct: Decimal,
        open_prefix: str,
        cover_prefix: str,
        ws_url: str,
        price_state: Optional[core.PriceState] = None,
        reference: Optional[Decimal] = None,
        step_pct: Optional[Decimal] = None,
        take_profit_grid_count: Optional[int] = None,
        take_profit_count: Optional[int] = None,
        *,
        user_id: str,
    ):
        self.stop = stop
        self.store = store
        self.api = api
        self.pair = pair
        self.code = code
        self.spec = spec
        self.take_profit_pct = take_profit_pct
        self.open_prefix = open_prefix
        self.cover_prefix = cover_prefix
        self.ws_url = ws_url
        self.price_state = price_state
        self.reference = reference
        self.step_pct = step_pct
        self.take_profit_grid_count = take_profit_grid_count
        self.take_profit_count = take_profit_count
        self.user_id = user_id
        self.pending_events: dict[str, tuple[dict, float]] = {}

    def _maintain_covers_after_short_fill(self) -> None:
        """开空成交后按现价下方窗口和挂单上限维护平仓单。"""
        if (
            self.price_state is None
            or self.reference is None
            or self.step_pct is None
            or self.take_profit_grid_count is None
            or self.take_profit_count is None
        ):
            return
        current = self.price_state.get()
        available_size = reconcile_short_position(
            self.api, self.store, self.pair, self.code
        )
        if available_size <= 0:
            return
        lower = short_take_profit_window_lower(
            self.reference, current, self.step_pct,
            self.take_profit_grid_count, self.spec,
        )
        reconcile_short_take_profits(
            self.api, self.store, self.pair, self.code, current, lower,
            self.spec, self.cover_prefix, self.take_profit_count, available_size,
        )

    def _strategy_text(self, side: str) -> str:
        prefix = self.cover_prefix if side == "buy" else self.open_prefix
        return f"t-{prefix}{self.code}"

    def _belongs_to_strategy(self, event: dict) -> bool:
        side = str(event.get("side", ""))
        return (
            str(event.get("currency_pair", "")) == self.pair
            and side in {"buy", "sell"}
            and str(event.get("text", "")).startswith(self._strategy_text(side))
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

    def _event_from_order(self, order) -> dict:
        return {
            "_rest_snapshot": True,
            "finish_as": str(getattr(order, "finish_as", "") or ""),
            "id": str(order.id),
            "text": str(getattr(order, "text", "") or ""),
            "currency_pair": str(
                getattr(order, "currency_pair", self.pair) or self.pair
            ),
            "account": "futures",
            "side": str(getattr(order, "side", "")),
            "status": str(getattr(order, "status", "")),
            "filled_amount": core.decimal_text(core.order_filled_amount(order)),
            "amount": str(getattr(order, "amount", "0") or "0"),
            "left": str(getattr(order, "left", "0") or "0"),
            "price": str(getattr(order, "price", "0") or "0"),
        }

    def _insert_missing_open(self, event: dict) -> bool:
        """补建开空记录（sell 事件，DB 的 buy_* 字段表示 open leg）。"""
        if str(event.get("side", "")) != "sell":
            return False
        try:
            price = core.decimal_value("WebSocket开空价格", event.get("price"))
            size = core.decimal_value("WebSocket开空数量", event.get("amount"))
        except ValueError as exc:
            print(f"[{core.now_text()}] ShortOrderListener 无法补建开空记录：{exc}")
            return False
        order = SimpleNamespace(
            id=str(event.get("id", "")), text=str(event.get("text", "") or "")
        )
        row_id = self.store.insert_buy(
            self.pair, self.code, order, core.PlannedOrder(price, size),
            short_take_profit_price(price, self.take_profit_pct, self.spec),
        )
        print(
            f"[{core.now_text()}] ShortOrderListener 补建开空记录："
            f"价格={price}，数量={size}，id={order.id}，记录={row_id}"
        )
        return True

    def _attach_missing_cover(self, event: dict) -> bool:
        if str(event.get("side", "")) != "buy":
            return False
        try:
            price = core.decimal_value("平仓买价", event.get("price"))
            size = core.decimal_value("平仓买单数量", event.get("amount"))
        except ValueError:
            return False
        row = self.store.attach_missing_take_profit(
            self.pair, self.code, str(event.get("id", "")),
            str(event.get("text", "") or ""), price, size,
        )
        if row is None:
            return False
        print(
            f"[{core.now_text()}] ShortOrderListener 恢复平仓买单关联："
            f"记录={row['id']}，买价={price}，数量={size}，"
            f"id={event.get('id', '')}"
        )
        return True

    def handle_order_update(self, event: dict) -> bool:
        """处理一个订单事件；返回是否找到并更新了对应数据库周期。"""
        if str(event.get("currency_pair", "")) != self.pair:
            return False
        account = str(event.get("account", "") or "")
        if account and account != self.account_name:
            return False
        # 旧整数推送可能把 ±0.1 张变为 0，连方向也无法确定；只能等待 REST。
        if "amount" in event and Decimal(str(event.get("amount") or "0")) <= 0:
            core.flag_open_verification(
                self.store, self.pair, self.code, str(event.get("id", ""))
            )
            return False
        side = str(event.get("side", ""))
        if side not in {"buy", "sell"}:
            return False
        order_id = str(event.get("id", ""))
        order_text = str(event.get("text", "") or "")
        # 开空=DB open leg(buy_*)，平仓=DB close leg(tp_*)
        db_side = "buy" if side == "sell" else "sell"
        row = self.store.find_order(self.pair, self.code, order_id, db_side)
        if row is None and order_text:
            row = self.store.find_order_text(
                self.pair, self.code, order_text, db_side
            )
        if row is None:
            return False
        if side == "sell" and not row["buy_order_id"] and order_id:
            self.store.bind_reserved_buy(
                int(row["id"]),
                SimpleNamespace(id=order_id, text=order_text),
            )
            row = self.store.one(int(row["id"]))
            if row is None:
                return False
        result = SimpleNamespace(**event)
        if side == "sell":
            # 开空 leg 成交
            was_filled = row["buy_status"] == core.BUY_FILLED
            updated = core.apply_buy_update(self.api, self.store, self.pair, row, result)
            if (
                not was_filled
                and updated is not None
                and updated["buy_status"] == core.BUY_FILLED
                and not updated["tp_order_id"]
            ):
                self._maintain_covers_after_short_fill()
        else:
            # 平仓 leg 成交
            core.mark_tp_result(self.store, row, result)
        return True

    def _receive_event(self, event: dict) -> None:
        if self.handle_order_update(event):
            return
        if (
            self._belongs_to_strategy(event)
            and self._attach_missing_cover(event)
            and self.handle_order_update(event)
        ):
            return
        if (
            self._belongs_to_strategy(event)
            and str(event.get("side", "")) == "sell"
            and self._insert_missing_open(event)
            and self.handle_order_update(event)
        ):
            return
        if self._belongs_to_strategy(event):
            self.pending_events[str(event.get("id", ""))] = (
                event, time.monotonic() + core.ORPHAN_ORDER_GRACE_SECONDS
            )

    def _flush_pending_events(self) -> None:
        now = time.monotonic()
        for order_id, (event, deadline) in list(self.pending_events.items()):
            if self.handle_order_update(event) or now >= deadline:
                self.pending_events.pop(order_id, None)

    def reconcile_rest(self, exact_missing: bool = False) -> None:
        opened, finished = core.list_order_snapshot(self.api, self.pair)

        def current_pair_only(order) -> bool:
            order_pair = str(
                getattr(order, "currency_pair", None)
                or getattr(order, "contract", None)
                or self.pair
            )
            return order_pair == self.pair

        opened = [order for order in opened if current_pair_only(order)]
        finished = [order for order in finished if current_pair_only(order)]
        for order in opened:
            event = self._event_from_order(order)
            if self._belongs_to_strategy(event):
                self._receive_event(event)
        for order in finished:
            event = self._event_from_order(order)
            if self._belongs_to_strategy(event):
                self.handle_order_update(event)
        snapshot = {str(order.id): order for order in (*opened, *finished)}
        for row, kind, _ in self.store.pending_legs(
            self.pair, self.code, Decimal("0")
        ):
            order_id = str(
                row["buy_order_id"] if kind == "buy" else row["tp_order_id"]
            )
            result = snapshot.get(order_id)
            if result is None and exact_missing:
                try:
                    result = core.api_order(self.api, self.pair, order_id)
                except Exception as exc:
                    print(
                        f"[{core.now_text()}] ShortOrderListener 补查订单 "
                        f"{order_id} 失败：{exc}"
                    )
                    continue
            if result is None:
                continue
            event = self._event_from_order(result)
            if not event["side"]:
                event["side"] = "buy" if kind == "tp" else "sell"
            self.handle_order_update(event)
        cancel_orphan_short_covers(
            self.api, self.store, self.pair, self.code, self.cover_prefix, opened
        )
        recovered = self.store.recover_stale_take_profit_claims(
            self.pair, self.code, core.TP_SUBMISSION_STALE_SECONDS
        )
        if recovered:
            print(
                f"[{core.now_text()}] ShortOrderListener 恢复超时平仓挂单状态："
                f"{recovered} 条"
            )
        released = self.store.recover_stale_buy_reservations(
            self.pair, self.code, core.BUY_RESERVATION_STALE_SECONDS
        )
        if released:
            print(
                f"[{core.now_text()}] OrderStateChecker 释放超时开空单预留："
                f"{released} 条"
            )

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

    @staticmethod
    def _proxy_options() -> dict:
        proxy = os.getenv("GATE_PROXY", "").strip()
        if not proxy:
            return {}
        from urllib.parse import urlparse
        parsed = urlparse(proxy)
        if not parsed.hostname:
            return {}
        options = {
            "http_proxy_host": parsed.hostname,
            "http_proxy_port": parsed.port,
            "proxy_type": (
                "http" if parsed.scheme in {"", "http", "https"}
                else parsed.scheme
            ),
        }
        if parsed.username:
            options["http_proxy_auth"] = (parsed.username, parsed.password or "")
        return options

    def run(self, api_key: str, api_secret: str) -> None:
        try:
            import websocket
        except ImportError as exc:
            print(
                f"[{core.now_text()}] ShortOrderListener 无法启动：缺少 "
                "websocket-client；请执行 pip install websocket-client"
            )
            self.stop.set()
            return
        while not self.stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(
                    self.ws_url,
                    header={"X-Gate-Size-Decimal": "1"},
                    timeout=core.NETWORK_TIMEOUT[1],
                    **self._proxy_options(),
                )
                ws.send(self._subscription(api_key, api_secret))
                ws.settimeout(1)
                last_ping = time.monotonic()
                print(
                    f"[{core.now_text()}] ShortOrderListener 已订阅 "
                    f"{self.pair} {self.channel_name}"
                )
                while not self.stop.is_set():
                    try:
                        raw = ws.recv()
                        if not raw:
                            raise ConnectionError("WebSocket 连接已关闭")
                        message = json.loads(raw)
                        if message.get("error"):
                            raise RuntimeError(
                                f"WebSocket 返回错误：{message['error']}"
                            )
                        if message.get("event") == "update":
                            result = message.get("result") or []
                            for event in result if isinstance(result, list) else [result]:
                                if isinstance(event, dict):
                                    self._receive_event(self._normalise_event(event))
                    except websocket.WebSocketTimeoutException:
                        pass
                    self._flush_pending_events()
                    now = time.monotonic()
                    if now - last_ping >= 20:
                        ws.ping()
                        last_ping = now
            except Exception as exc:
                if not self.stop.is_set():
                    print(
                        f"[{core.now_text()}] ShortOrderListener 断线：{exc}；准备重连"
                    )
                    self.stop.wait(core.WEBSOCKET_RECONNECT_INTERVAL)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# 做空网格检查器（维护现价上方开空窗口 + 现价下方平仓窗口）
# ---------------------------------------------------------------------------

class ShortGridChecker:
    """维护做空网格窗口：现价上方开空单、现价下方平仓单。"""

    def __init__(
        self,
        stop: threading.Event,
        store: core.GridStore,
        api: ShortFuturesGridApi,
        pair: str,
        code: str,
        price_state: core.PriceState,
        reference: Decimal,
        step_pct: Decimal,
        short_grid_count: int,
        take_profit_grid_count: int,
        take_profit_count: int,
        spec: core.PairSpec,
        cover_prefix: str,
        max_unclosed_per_grid: int = 2,
        take_profit_available_size=None,
    ):
        self.stop = stop
        self.store = store
        self.api = api
        self.pair = pair
        self.code = code
        self.price_state = price_state
        self.reference = reference
        self.step_pct = step_pct
        self.short_grid_count = short_grid_count
        self.take_profit_grid_count = take_profit_grid_count
        self.take_profit_count = take_profit_count
        self.spec = spec
        self.cover_prefix = cover_prefix
        self.max_unclosed_per_grid = max_unclosed_per_grid
        self.take_profit_available_size = take_profit_available_size

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                current = self.price_state.get()
                desired_prices = set(desired_short_prices(
                    self.reference, current, self.step_pct,
                    self.short_grid_count, self.spec.price_step,
                ))
                core.update_grid_flags(
                    self.store, self.pair, self.code, desired_prices
                )
                core.trim_outside_grid(
                    self.api, self.store, self.pair, self.code, desired_prices,
                    self.max_unclosed_per_grid,
                )
                lower = short_take_profit_window_lower(
                    self.reference, current, self.step_pct,
                    self.take_profit_grid_count, self.spec,
                )
                available_size = (
                    self.take_profit_available_size()
                    if self.take_profit_available_size is not None else None
                )
                reconcile_short_take_profits(
                    self.api, self.store, self.pair, self.code, current, lower,
                    self.spec, self.cover_prefix, self.take_profit_count,
                    available_size,
                )
            except Exception as exc:
                detail = (
                    f"label={exc.label}, message={exc.message}"
                    if isinstance(exc, GateApiException) else str(exc)
                )
                print(f"[{core.now_text()}] ShortGridChecker 本轮失败：{detail}")
            self.stop.wait(core.GRID_CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_short_grid(
    contract: str,
    reference_price,
    margin_amount_per_order,
    leverage="2",
    settle: str = "usdt",
    grid_tag: str = "futshort01",
    open_client_order_prefix: str = "ss",
    cover_client_order_prefix: str = "sb",
    short_grid_count: int = 10,
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
    if short_grid_count <= 0 or take_profit_count <= 0 or take_profit_grid_count <= 0:
        raise ValueError("开空格、平仓单数量和平仓网格区间必须大于 0")
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
        public_api = ShortFuturesGridApi(public_futures, settle)
        current = core.get_last_price(public_api, contract)
        desired = desired_short_orders(
            reference, current, step_pct, short_grid_count, margin,
            leverage_value, contract_spec,
        )
        if not live:
            print(
                f"合约做空安全预览：{contract}，结算币={settle.upper()}，"
                f"当前价={current}，每张={contract_spec.multiplier} "
                f"{contract_spec.grid_spec.base_currency}，数据库={database_path}"
            )
            for order in desired:
                notional = order.size * order.price * contract_spec.multiplier
                print(
                    f"开空卖价={order.price} 张数={order.size} "
                    f"名义价值={core.decimal_text(notional)} USDT "
                    f"计划平仓={short_take_profit_price(order.price, tp_pct, contract_spec.grid_spec)}"
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
    apis = [ShortFuturesGridApi(api, settle) for api in futures_apis]
    try:
        process_lock.acquire()
        store = core.GridStore(Path(database_path))
    except Exception:
        for client in clients:
            client.close()
        process_lock.release()
        raise

    generated_code = short_futures_strategy_code(
        settle, contract, reference, step_pct, tp_pct, margin,
        leverage_value, grid_tag, max_unclosed_per_grid,
    )
    code = generated_code
    if startup_mode == "resume":
        existing_code = resume_short_strategy_code(store, contract)
        if existing_code:
            code = existing_code
    if code != generated_code:
        print(
            f"[{core.now_text()}] 当前参数生成策略码={generated_code}，"
            f"数据库已有策略码={code}；resume 模式按数据库策略继续运行，"
            f"已有订单数量保持不变，新单每格保证金={margin}，杠杆={leverage_value}"
        )
    price_state = core.PriceState(current)
    stop = threading.Event()
    grid_checker = ShortGridChecker(
        stop, store, apis[1], contract, code, price_state, reference, step_pct,
        short_grid_count, take_profit_grid_count, take_profit_count,
        contract_spec.grid_spec, cover_client_order_prefix,
        max_unclosed_per_grid,
        take_profit_available_size=lambda: reconcile_short_position(
            apis[1], store, contract, code
        ),
    )
    order_listener = ShortOrderListener(
        stop, store, apis[2], contract, code, contract_spec.grid_spec, tp_pct,
        open_client_order_prefix, cover_client_order_prefix, ws_url,
        price_state, reference, step_pct, take_profit_grid_count,
        take_profit_count,
        user_id=user_id,
    )
    state_reconciler = ShortOrderListener(
        stop, store, apis[3], contract, code, contract_spec.grid_spec, tp_pct,
        open_client_order_prefix, cover_client_order_prefix, ws_url,
        price_state, reference, step_pct, take_profit_grid_count,
        take_profit_count,
        user_id=user_id,
    )
    order_state_checker = core.OrderStateChecker(stop, state_reconciler)
    checker = threading.Thread(
        target=grid_checker.run, name=f"futures-short-grid-check-{contract}", daemon=True
    )
    listener = threading.Thread(
        target=order_listener.run, args=(api_key, api_secret),
        name=f"futures-short-order-listener-{contract}", daemon=True,
    )
    state_sync = threading.Thread(
        target=order_state_checker.run,
        name=f"futures-short-order-state-check-{contract}",
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
        state_sync.start()
        listener.start()
        checker.start()
        print(
            f"[{core.now_text()}] 启动合约做空数据库网格：{contract}，"
            f"持仓模式={mode}，杠杆={leverage_value}，策略码={code}"
        )
        cycle = 0
        failures = 0
        while not stop.is_set() and (max_cycles is None or cycle < max_cycles):
            cycle += 1
            try:
                current = core.get_last_price(apis[0], contract)
                price_state.set(current)
                desired = desired_short_orders(
                    reference, current, step_pct, short_grid_count, margin,
                    leverage_value, contract_spec,
                )
                place_missing_shorts(
                    apis[0], store, contract, code, desired, tp_pct,
                    contract_spec.grid_spec, open_client_order_prefix,
                    max_unclosed_per_grid,
                )
                lower = short_take_profit_window_lower(
                    reference, current, step_pct, take_profit_grid_count,
                    contract_spec.grid_spec,
                )
                rows = store.active(contract, code)
                pending = sum(row["buy_status"] == core.WAIT_BUY for row in rows)
                open_covers = sum(row["tp_status"] == core.WAIT_TP for row in rows)
                waiting = sum(
                    row["buy_status"] == core.BUY_FILLED and row["tp_status"] in {
                        core.TP_NOT_SET, core.TP_WAIT_PLACE,
                        core.TP_SUBMITTING, core.TP_CANCELLED,
                    }
                    for row in rows
                )
                print(
                    f"[{core.now_text()}] 当前价={current}，"
                    f"开空单={pending}/{short_grid_count}，"
                    f"平仓单={open_covers}/{take_profit_count}，"
                    f"平仓区间=[{lower},{current})，等待平仓={waiting}，"
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
                    f"[{core.now_text()}] 合约做空主循环失败："
                    f"{type(exc).__name__}: {detail}"
                )
                if failures >= 5:
                    raise RuntimeError("连续 5 轮失败，自动停止做空网格") from exc
            if max_cycles is None or cycle < max_cycles:
                stop.wait(poll_interval)
    except KeyboardInterrupt:
        print("\n收到停止信号；数据库已保存，现有合约挂单不会自动撤销。")
    finally:
        stop.set()
        shutdown_timeout = (
            core.NETWORK_RETRY_ATTEMPTS * core.NETWORK_TIMEOUT[1] + 5
        )
        # 初始化失败或只有部分线程启动时，逐个检查，避免清理异常覆盖原异常。
        for worker in (checker, listener, state_sync):
            if worker.is_alive():
                worker.join(timeout=shutdown_timeout)
        if checker.is_alive() or listener.is_alive() or state_sync.is_alive():
            print(
                f"[{core.now_text()}] 做空异步线程仍在退出中，"
                "暂不关闭其数据库和客户端"
            )
            clients[0].close()
        else:
            store.close()
            process_lock.release()
            for client in clients:
                client.close()


def run_SOXL() -> None:
    run_short_grid(
        contract="SOXL_USDT",
        reference_price="150",
        margin_amount_per_order="1.6",  # 每格保证金；名义仓位约为保证金×杠杆
        leverage="2",
        settle="usdt",
        short_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,  # 检查现价下方20个网格，最多保留10个平仓单
        max_unclosed_per_grid=1,  # 单个网格价位最多保留的已成交未平仓周期数
        grid_step_pct="0.003",
        take_profit_pct="0.003",
        startup_mode="resume",
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_SOXL()
