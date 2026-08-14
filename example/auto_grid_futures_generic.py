"""Gate 逐仓杠杆现货、SQLite 持久化网格。

一条数据库记录代表一个完整周期：买单 -> 止盈卖单。程序始终维护现价下方
最近的 ``buy_grid_count`` 个买格。买单成交后才会实际提交止盈卖单；买单尚未
成交时，数据库只保存计划止盈价，避免卖单自动借入基础币形成意外空头。

启动模式：
- ``resume``：从当前交易对已有数据库状态恢复。
- ``reset``：撤销数据库记录的当前交易对未成交订单，清空其记录并重新初始化。

实盘必须设置 ``GATE_LIVE_CONFIRM=RUN_REAL_MARGIN_DB_GRID``。
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Callable, Optional, TypeVar

from urllib3.exceptions import HTTPError

from gate_api import (
    ApiClient,
    Configuration,
    MarginApi,
    MarginMarketLeverage,
    Order,
    SpotApi,
)
from gate_api.exceptions import ApiException, GateApiException


LIVE_CONFIRM_TEXT = "RUN_REAL_MARGIN_DB_GRID"
NETWORK_TIMEOUT = (5, 10)
NETWORK_RETRY_ATTEMPTS = 3
BALANCE_RETRY_INTERVAL = 30
CHECK_BATCH_INTERVAL = 0.5
ORDER_SNAPSHOT_LIMIT = 100
ORPHAN_ORDER_GRACE_SECONDS = 15
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = SCRIPT_DIR / "grid_orders.sqlite3"
T = TypeVar("T")

WAIT_BUY = "等待买单成交"
BUY_FILLED = "买单成交"
BUY_CANCELLED = "买单已取消"
TP_PLANNED = "已设置止盈单"
WAIT_TP = "等待止盈"
TP_FILLED = "止盈单成交"
TP_NOT_SET = "未设置止盈、等待设置止盈"
TP_CANCELLED = "止盈单已取消"
TP_DUST = "止盈部分成交、剩余低于最小下单金额"


@dataclass(frozen=True)
class PairSpec:
    base_currency: str
    quote_currency: str
    amount_step: Decimal
    price_step: Decimal
    min_base_amount: Decimal
    min_quote_amount: Decimal
    max_base_amount: Decimal
    max_quote_amount: Decimal


@dataclass(frozen=True)
class PlannedOrder:
    price: Decimal
    size: Decimal


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def percent_text(ratio: Decimal) -> str:
    return format(ratio * Decimal("100"), ".2f")


def decimal_value(name: str, raw_value) -> Decimal:
    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{name} 必须是数字，当前值：{raw_value!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} 必须大于 0，当前值：{raw_value!r}")
    return value


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"环境变量 {name} 未设置")
    return value


def load_env_file() -> None:
    for env_file in (SCRIPT_DIR / ".env", SCRIPT_DIR.parent / ".env"):
        if not env_file.is_file():
            continue
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name:
                os.environ.setdefault(name, value)
        return


def make_configuration(host: str, key=None, secret=None) -> Configuration:
    configuration = Configuration(host=host, key=key, secret=secret)
    configuration.retries = False
    proxy = os.getenv("GATE_PROXY", "").strip()
    if proxy:
        configuration.proxy = proxy
    return configuration


def retryable(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return True
    return isinstance(exc, ApiException) and getattr(exc, "status", 0) in {
        429, 500, 502, 503, 504
    }


def call_with_retry(action: str, callback: Callable[[], T]) -> T:
    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        try:
            return callback()
        except Exception as exc:
            if not retryable(exc) or attempt >= NETWORK_RETRY_ATTEMPTS:
                raise
            delay = attempt / 2
            print(f"[{now_text()}] {action}失败：{exc}；{delay:g} 秒后重试")
            time.sleep(delay)
    raise AssertionError("unreachable")


def round_to_step(value: Decimal, step: Decimal, rounding) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def get_pair_spec(api: SpotApi, currency_pair: str) -> PairSpec:
    pair = call_with_retry(
        "读取交易对规格",
        lambda: api.get_currency_pair(currency_pair, _request_timeout=NETWORK_TIMEOUT),
    )
    if pair.id != currency_pair or pair.trade_status != "tradable":
        raise RuntimeError(f"交易对 {currency_pair} 不存在或当前不可同时买卖")
    return PairSpec(
        base_currency=str(pair.base),
        quote_currency=str(pair.quote),
        amount_step=Decimal("1").scaleb(-int(pair.amount_precision)),
        price_step=Decimal("1").scaleb(-int(pair.precision)),
        min_base_amount=Decimal(str(pair.min_base_amount or "0")),
        min_quote_amount=Decimal(str(pair.min_quote_amount or "0")),
        max_base_amount=Decimal(str(pair.max_base_amount or "Infinity")),
        max_quote_amount=Decimal(str(pair.max_quote_amount or "Infinity")),
    )


def get_last_price(api: SpotApi, currency_pair: str) -> Decimal:
    rows = call_with_retry(
        "读取最新价格",
        lambda: api.list_tickers(
            currency_pair=currency_pair, _request_timeout=NETWORK_TIMEOUT
        ),
    )
    if len(rows) != 1 or not rows[0].last:
        raise RuntimeError("最新价格响应中没有唯一有效的 last")
    return decimal_value("last", rows[0].last)


def configure_margin_settings(
    api: MarginApi,
    pair: str,
    leverage: Decimal,
    auto_repay: bool,
) -> None:
    leverage_setting = MarginMarketLeverage(pair, decimal_text(leverage))
    call_with_retry(
        "设置逐仓杠杆",
        lambda: api.set_user_market_leverage(
            leverage_setting, _request_timeout=NETWORK_TIMEOUT
        ),
    )
    expected = "on" if auto_repay else "off"
    result = call_with_retry(
        "设置逐仓自动还款",
        lambda: api.set_auto_repay(expected, _request_timeout=NETWORK_TIMEOUT),
    )
    actual = str(getattr(result, "status", ""))
    if actual != expected:
        verified = call_with_retry(
            "确认逐仓自动还款",
            lambda: api.get_auto_repay_status(_request_timeout=NETWORK_TIMEOUT),
        )
        actual = str(getattr(verified, "status", ""))
    if actual != expected:
        raise RuntimeError(
            f"逐仓自动还款设置失败：期望 {expected}，实际 {actual or '未知'}"
        )


def raw_grid_price(reference: Decimal, step_pct: Decimal, index: int) -> Decimal:
    return reference * ((Decimal("1") - step_pct) ** index)


def buy_grid_price(
    reference: Decimal, step_pct: Decimal, index: int, price_step: Decimal
) -> Decimal:
    return round_to_step(
        raw_grid_price(reference, step_pct, index), price_step, ROUND_HALF_UP
    )


def first_buy_index(
    reference: Decimal, current: Decimal, step_pct: Decimal, price_step: Decimal
) -> int:
    ratio = float(current / reference)
    step_ratio = float(Decimal("1") - step_pct)
    index = math.floor(math.log(ratio) / math.log(step_ratio)) + 1
    while buy_grid_price(reference, step_pct, index, price_step) >= current:
        index += 1
    while buy_grid_price(reference, step_pct, index - 1, price_step) < current:
        index -= 1
    return index


def calculate_order_size(
    margin: Decimal, leverage: Decimal, price: Decimal, spec: PairSpec
) -> Decimal:
    size = round_to_step(margin * leverage / price, spec.amount_step, ROUND_DOWN)
    notional = size * price
    if size <= 0 or size < spec.min_base_amount or notional < spec.min_quote_amount:
        raise ValueError(
            f"单格名义金额不足：{decimal_text(notional)} USDT，"
            f"交易对最低要求 {decimal_text(spec.min_quote_amount)} USDT"
        )
    if size > spec.max_base_amount or notional > spec.max_quote_amount:
        raise ValueError("单格订单超过交易对最大数量或金额")
    return size


def desired_buy_orders(
    reference: Decimal,
    current: Decimal,
    step_pct: Decimal,
    count: int,
    margin: Decimal,
    leverage: Decimal,
    spec: PairSpec,
) -> list[PlannedOrder]:
    start = first_buy_index(reference, current, step_pct, spec.price_step)
    result: list[PlannedOrder] = []
    index = start
    while len(result) < count:
        price = buy_grid_price(reference, step_pct, index, spec.price_step)
        index += 1
        if price <= 0:
            raise RuntimeError("网格价格小于等于 0")
        if result and result[-1].price == price:
            continue
        result.append(PlannedOrder(price, calculate_order_size(
            margin, leverage, price, spec
        )))
        if index - start > count * 100:
            raise RuntimeError("网格间距小于价格精度")
    return result


def take_profit_price(
    buy_price: Decimal, take_profit_pct: Decimal, spec: PairSpec
) -> Decimal:
    return round_to_step(
        buy_price * (Decimal("1") + take_profit_pct), spec.price_step, ROUND_UP
    )


def take_profit_window_upper(
    reference: Decimal,
    current: Decimal,
    step_pct: Decimal,
    grid_count: int,
    spec: PairSpec,
) -> Decimal:
    """返回当前价上方第 ``grid_count`` 个锚定网格的价格。"""
    first_below = first_buy_index(reference, current, step_pct, spec.price_step)
    return buy_grid_price(
        reference, step_pct, first_below - grid_count, spec.price_step
    )


def strategy_code(
    currency_pair: str,
    reference: Decimal,
    step_pct: Decimal,
    take_profit_pct: Decimal,
    margin: Decimal,
    leverage: Decimal,
    grid_tag: str,
) -> str:
    raw = (
        f"{grid_tag}|db-margin|{currency_pair}|{decimal_text(reference)}|"
        f"{decimal_text(step_pct)}|{decimal_text(take_profit_pct)}|"
        f"{decimal_text(margin)}|{decimal_text(leverage)}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def make_order_text(prefix: str, code: str) -> str:
    value = f"t-{prefix}{code}{uuid.uuid4().hex[:16]}"
    if len(value.encode("ascii")) > 30:
        raise ValueError("订单 text 超过 Gate 30 字节限制")
    return value


class GridStore:
    """线程安全的 SQLite 网格状态存储。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS grid_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    currency_pair TEXT NOT NULL,
                    strategy_code TEXT NOT NULL,
                    buy_price TEXT NOT NULL,
                    buy_size TEXT NOT NULL,
                    remaining_size TEXT NOT NULL,
                    buy_order_id TEXT,
                    buy_order_text TEXT,
                    buy_status TEXT NOT NULL,
                    tp_price TEXT NOT NULL,
                    tp_order_id TEXT,
                    tp_order_text TEXT,
                    tp_status TEXT NOT NULL,
                    in_grid INTEGER NOT NULL DEFAULT 1,
                    completed INTEGER NOT NULL DEFAULT 0,
                    last_checked_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_grid_pair_state
                ON grid_cycles(currency_pair, strategy_code, completed, in_grid);
                CREATE INDEX IF NOT EXISTS idx_grid_buy_price
                ON grid_cycles(currency_pair, strategy_code, buy_price);
                """
            )
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def rows(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.db.execute(sql, params).fetchall())

    def execute(self, sql: str, params=()) -> int:
        with self.lock:
            cursor = self.db.execute(sql, params)
            self.db.commit()
            return int(cursor.lastrowid or 0)

    def active(self, pair: str, code: str) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT * FROM grid_cycles WHERE currency_pair=? AND strategy_code=? "
            "AND completed=0 ORDER BY CAST(buy_price AS REAL) DESC, id",
            (pair, code),
        )

    def unfinished_other_strategies(
        self, pair: str, code: str
    ) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT strategy_code,COUNT(*) AS cycle_count FROM grid_cycles "
            "WHERE currency_pair=? AND strategy_code<>? AND completed=0 "
            "GROUP BY strategy_code ORDER BY strategy_code",
            (pair, code),
        )

    def pending_legs(
        self, pair: str, code: str, current: Decimal
    ) -> list[tuple[sqlite3.Row, str, Decimal]]:
        result = []
        for row in self.active(pair, code):
            if row["buy_order_id"] and row["buy_status"] == WAIT_BUY:
                result.append((row, "buy", Decimal(row["buy_price"])))
            if row["tp_order_id"] and row["tp_status"] == WAIT_TP:
                result.append((row, "tp", Decimal(row["tp_price"])))
        result.sort(key=lambda item: abs(item[2] - current))
        return result

    def exposure_count(self, pair: str, code: str, price: Decimal) -> int:
        row = self.rows(
            "SELECT COUNT(*) AS n FROM grid_cycles WHERE currency_pair=? "
            "AND strategy_code=? AND buy_price=? AND buy_status=? "
            "AND tp_status<>? AND completed=0",
            (pair, code, decimal_text(price), BUY_FILLED, TP_FILLED),
        )[0]
        return int(row["n"])

    def linked_take_profit_ids(self, pair: str, code: str) -> set[str]:
        rows = self.rows(
            "SELECT tp_order_id FROM grid_cycles WHERE currency_pair=? "
            "AND strategy_code=? AND buy_status=? AND tp_status=? "
            "AND tp_order_id IS NOT NULL AND completed=0",
            (pair, code, BUY_FILLED, WAIT_TP),
        )
        return {str(row["tp_order_id"]) for row in rows}

    def insert_buy(
        self, pair: str, code: str, order, planned: PlannedOrder, tp: Decimal
    ) -> int:
        now = time.time()
        return self.execute(
            "INSERT INTO grid_cycles(currency_pair,strategy_code,buy_price,buy_size,"
            "remaining_size,buy_order_id,buy_order_text,buy_status,tp_price,tp_status,"
            "in_grid,completed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
            (
                pair, code, decimal_text(planned.price), decimal_text(planned.size),
                decimal_text(planned.size), str(order.id), str(order.text or ""),
                WAIT_BUY, decimal_text(tp), TP_PLANNED, now, now,
            ),
        )

    def clear_pair(self, pair: str) -> None:
        self.execute("DELETE FROM grid_cycles WHERE currency_pair=?", (pair,))


class PriceState:
    def __init__(self, value: Decimal):
        self.lock = threading.Lock()
        self.value = value

    def set(self, value: Decimal) -> None:
        with self.lock:
            self.value = value

    def get(self) -> Decimal:
        with self.lock:
            return self.value


class StrategyProcessLock:
    """阻止同一数据库、同一交易对被两个进程同时维护。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.file.close()
            self.file = None
            raise RuntimeError(
                f"{self.path} 已被占用；同一交易对已有网格进程运行"
            ) from exc

    def release(self) -> None:
        if self.file is None:
            return
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def api_order(api: SpotApi, pair: str, order_id: str):
    return call_with_retry(
        f"查询订单 {order_id}",
        lambda: api.get_order(
            order_id, pair, account="margin", _request_timeout=NETWORK_TIMEOUT
        ),
    )


def list_order_snapshot(api: SpotApi, pair: str) -> tuple[list, list]:
    """并发获取当前挂单和最近完结订单；缺失记录再由调用方单查。"""
    def fetch() -> tuple[list, list]:
        open_job = api.list_orders(
            pair, "open", account="margin", limit=ORDER_SNAPSHOT_LIMIT,
            async_req=True, _request_timeout=NETWORK_TIMEOUT,
        )
        finished_job = api.list_orders(
            pair, "finished", account="margin", limit=ORDER_SNAPSHOT_LIMIT,
            async_req=True, _request_timeout=NETWORK_TIMEOUT,
        )
        return list(open_job.get()), list(finished_job.get())

    return call_with_retry("批量读取订单状态", fetch)


def cancel_api_order(api: SpotApi, pair: str, order_id: Optional[str]):
    if not order_id:
        return None
    try:
        return api.cancel_order(
            order_id, pair, account="margin", _request_timeout=NETWORK_TIMEOUT
        )
    except GateApiException as exc:
        if exc.label not in {"ORDER_NOT_FOUND", "ORDER_CLOSED", "ORDER_CANCELLED"}:
            raise
        return api_order(api, pair, order_id)


def create_limit_order(
    api: SpotApi,
    pair: str,
    side: str,
    price: Decimal,
    size: Decimal,
    auto_borrow: bool,
    prefix: str,
    code: str,
):
    text = make_order_text(prefix, code)
    body = Order(
        currency_pair=pair,
        side=side,
        amount=decimal_text(size),
        price=decimal_text(price),
        type="limit",
        account="margin",
        time_in_force="gtc",
        text=text,
        auto_borrow=auto_borrow if side == "buy" else False,
    )
    return api.create_order(body, _request_timeout=NETWORK_TIMEOUT)


def mark_buy_result(store: GridStore, row, result) -> None:
    filled = Decimal(str(result.filled_amount or "0"))
    finished = result.status in {"closed", "cancelled"}
    if finished and filled > 0:
        size = filled
        store.execute(
            "UPDATE grid_cycles SET buy_status=?,buy_size=?,remaining_size=?,"
            "tp_status=?,last_checked_at=?,updated_at=? WHERE id=?",
            (
                BUY_FILLED, decimal_text(size), decimal_text(size), TP_NOT_SET,
                time.time(), time.time(), row["id"],
            ),
        )
        print(f"[{now_text()}] 买单成交：id={row['buy_order_id']}，数量={size}")
    elif finished:
        store.execute("DELETE FROM grid_cycles WHERE id=?", (row["id"],))
    else:
        store.execute(
            "UPDATE grid_cycles SET last_checked_at=? WHERE id=?",
            (time.time(), row["id"]),
        )


def mark_tp_result(store: GridStore, row, result) -> None:
    filled = Decimal(str(result.filled_amount or "0"))
    remaining = Decimal(row["remaining_size"])
    finished = result.status in {"closed", "cancelled"}
    if finished and filled >= remaining:
        store.execute(
            "UPDATE grid_cycles SET remaining_size='0',tp_status=?,completed=1,"
            "last_checked_at=?,updated_at=? WHERE id=?",
            (TP_FILLED, time.time(), time.time(), row["id"]),
        )
        print(f"[{now_text()}] 止盈成交：id={row['tp_order_id']}，周期完成")
    elif finished:
        remaining = max(Decimal("0"), remaining - filled)
        store.execute(
            "UPDATE grid_cycles SET remaining_size=?,tp_order_id=NULL,"
            "tp_order_text=NULL,tp_status=?,last_checked_at=?,updated_at=? WHERE id=?",
            (
                decimal_text(remaining), TP_NOT_SET, time.time(), time.time(),
                row["id"],
            ),
        )
    else:
        store.execute(
            "UPDATE grid_cycles SET last_checked_at=? WHERE id=?",
            (time.time(), row["id"]),
        )


def update_grid_flags(
    store: GridStore, pair: str, code: str, desired_prices: set[Decimal]
) -> None:
    for row in store.active(pair, code):
        in_grid = int(Decimal(row["buy_price"]) in desired_prices)
        if in_grid != row["in_grid"]:
            store.execute(
                "UPDATE grid_cycles SET in_grid=?,updated_at=? WHERE id=?",
                (in_grid, time.time(), row["id"]),
            )


def cancel_orphan_take_profits(
    api: SpotApi,
    store: GridStore,
    pair: str,
    code: str,
    sell_prefix: str,
    opened: list,
) -> int:
    """撤销属于当前策略、但没有已成交买单数据库记录的卖单。"""
    text_prefix = f"t-{sell_prefix}{code}"
    linked_ids = store.linked_take_profit_ids(pair, code)
    cancelled = 0
    for order in opened:
        order_id = str(order.id)
        if (
            str(getattr(order, "side", "")) != "sell"
            or not str(getattr(order, "text", "")).startswith(text_prefix)
            or order_id in linked_ids
        ):
            continue
        create_time = float(getattr(order, "create_time", 0) or 0)
        if create_time and time.time() - create_time < ORPHAN_ORDER_GRACE_SECONDS:
            continue
        # 防止主线程在获取快照后刚好完成数据库写入。
        if order_id in store.linked_take_profit_ids(pair, code):
            continue
        cancel_api_order(api, pair, order_id)
        cancelled += 1
        print(
            f"[{now_text()}] 撤销无关联买单的止盈卖单："
            f"id={order_id}，价格={order.price}，数量={order.amount}"
        )
    return cancelled


def order_checker(
    stop: threading.Event,
    store: GridStore,
    api: SpotApi,
    pair: str,
    code: str,
    price_state: PriceState,
    desired_prices_getter: Callable[[], set[Decimal]],
    sell_prefix: str,
) -> None:
    while not stop.is_set():
        legs = store.pending_legs(pair, code, price_state.get())
        try:
            opened, finished = list_order_snapshot(api, pair)
            snapshot = {
                str(order.id): order for order in (*opened, *finished)
            }
        except Exception as exc:
            print(f"[{now_text()}] 批量检查订单失败：{exc}")
            stop.wait(CHECK_BATCH_INTERVAL)
            continue
        for row, kind, _ in legs:
            order_id = str(
                row["buy_order_id"] if kind == "buy" else row["tp_order_id"]
            )
            try:
                # finished 列表有数量上限；极老挂单刚成交时可能不在快照中。
                result = snapshot.get(order_id) or api_order(api, pair, order_id)
                if kind == "buy":
                    mark_buy_result(store, row, result)
                else:
                    mark_tp_result(store, row, result)
            except Exception as exc:
                print(f"[{now_text()}] 检查订单 {order_id} 失败：{exc}")
        try:
            cancel_orphan_take_profits(
                api, store, pair, code, sell_prefix, opened
            )
        except Exception as exc:
            print(f"[{now_text()}] 检查无关联止盈卖单失败：{exc}")
        update_grid_flags(store, pair, code, desired_prices_getter())
        stop.wait(CHECK_BATCH_INTERVAL)


def place_missing_buys(
    api: SpotApi,
    store: GridStore,
    pair: str,
    code: str,
    desired: list[PlannedOrder],
    tp_pct: Decimal,
    spec: PairSpec,
    auto_borrow: bool,
    prefix: str,
) -> bool:
    rows = store.active(pair, code)
    pending_prices = {
        Decimal(row["buy_price"]) for row in rows if row["buy_status"] == WAIT_BUY
    }
    for planned in desired:
        if planned.price in pending_prices:
            continue
        if store.exposure_count(pair, code, planned.price) >= 2:
            print(f"[{now_text()}] {planned.price} 已有2组未止盈持仓，跳过新买单")
            continue
        try:
            order = create_limit_order(
                api, pair, "buy", planned.price, planned.size,
                auto_borrow, prefix, code
            )
        except GateApiException as exc:
            if exc.label in {"BALANCE_NOT_ENOUGH", "AUTO_BORROW_TOO_MUCH"}:
                print(f"[{now_text()}] 买单资金/可借额度不足，暂停本轮补单")
                return False
            raise
        tp = take_profit_price(planned.price, tp_pct, spec)
        try:
            store.insert_buy(pair, code, order, planned, tp)
        except Exception:
            cancel_api_order(api, pair, str(order.id))
            raise
        print(
            f"[{now_text()}] 新增买单并写库：价格={planned.price}，"
            f"数量={planned.size}，计划止盈={tp}，id={order.id}"
        )
    return True


def reconcile_take_profits(
    api: SpotApi,
    store: GridStore,
    pair: str,
    code: str,
    current: Decimal,
    upper_price: Decimal,
    spec: PairSpec,
    prefix: str,
    limit: int,
) -> None:
    for row in store.active(pair, code):
        if (
            row["buy_status"] != BUY_FILLED
            or row["tp_status"] == WAIT_TP
            or Decimal(row["remaining_size"]) <= 0
        ):
            continue
        size = Decimal(row["remaining_size"])
        price = Decimal(row["tp_price"])
        notional = size * price
        if size >= spec.min_base_amount and notional >= spec.min_quote_amount:
            continue
        if row["tp_status"] != TP_DUST:
            store.execute(
                "UPDATE grid_cycles SET tp_order_id=NULL,tp_order_text=NULL,"
                "tp_status=?,updated_at=? WHERE id=?",
                (TP_DUST, time.time(), row["id"]),
            )
            print(
                f"[{now_text()}] 止盈剩余转为碎片：记录={row['id']}，"
                f"数量={size}，金额={decimal_text(notional)} USDT，"
                f"最低金额={decimal_text(spec.min_quote_amount)} USDT"
            )

    eligible = [
        row for row in store.active(pair, code)
        if row["buy_status"] == BUY_FILLED
        and row["tp_status"] in {TP_NOT_SET, TP_CANCELLED, TP_PLANNED}
        and current < Decimal(row["tp_price"]) <= upper_price
        and Decimal(row["remaining_size"]) > 0
    ]
    # 已挂止盈也参与排序，使有限名额始终留给最接近现价的止盈价。
    eligible.extend(
        row for row in store.active(pair, code)
        if row["buy_status"] == BUY_FILLED
        and row["tp_status"] == WAIT_TP
        and current < Decimal(row["tp_price"]) <= upper_price
        and Decimal(row["remaining_size"]) > 0
    )
    eligible.sort(key=lambda row: Decimal(row["tp_price"]) - current)
    selected_ids = {row["id"] for row in eligible[:limit]}

    for row in store.active(pair, code):
        if row["tp_status"] != WAIT_TP or row["id"] in selected_ids:
            continue
        result = cancel_api_order(api, pair, row["tp_order_id"])
        mark_tp_result(store, row, result)
        print(
            f"[{now_text()}] 注销远端止盈：卖价={row['tp_price']}，"
            f"有效区间=({current}, {upper_price}]，id={row['tp_order_id']}"
        )

    if not selected_ids:
        return
    placeholders = ",".join("?" for _ in selected_ids)
    rows = store.rows(
        f"SELECT * FROM grid_cycles WHERE id IN ({placeholders}) "
        "ORDER BY CAST(tp_price AS REAL)",
        tuple(selected_ids),
    )
    for row in rows:
        if row["tp_status"] == WAIT_TP or row["completed"]:
            continue
        price = Decimal(row["tp_price"])
        size = Decimal(row["remaining_size"])
        try:
            order = create_limit_order(
                api, pair, "sell", price, size, False, prefix, code
            )
        except GateApiException as exc:
            message = str(exc.message or "").lower()
            if exc.label == "INVALID_PARAM_VALUE" and (
                "too small" in message or "minimum" in message
            ):
                store.execute(
                    "UPDATE grid_cycles SET tp_order_id=NULL,tp_order_text=NULL,"
                    "tp_status=?,updated_at=? WHERE id=?",
                    (TP_DUST, time.time(), row["id"]),
                )
                print(
                    f"[{now_text()}] Gate拒绝过小止盈，已转为碎片："
                    f"记录={row['id']}，数量={size}，金额="
                    f"{decimal_text(size * price)} USDT"
                )
                continue
            if exc.label == "BALANCE_NOT_ENOUGH":
                print(f"[{now_text()}] 基础币余额不足，暂不能设置止盈：记录{row['id']}")
                return
            raise
        try:
            store.execute(
                "UPDATE grid_cycles SET tp_order_id=?,tp_order_text=?,tp_status=?,"
                "updated_at=? WHERE id=?",
                (
                    str(order.id), str(order.text or ""), WAIT_TP,
                    time.time(), row["id"],
                ),
            )
        except Exception:
            cancel_api_order(api, pair, str(order.id))
            raise
        print(f"[{now_text()}] 设置止盈：买价={row['buy_price']}，卖价={price}，id={order.id}")


def trim_outside_grid(
    api: SpotApi,
    store: GridStore,
    pair: str,
    code: str,
    desired_prices: set[Decimal],
) -> None:
    minimum_grid_price = min(desired_prices)
    for row in store.active(pair, code):
        buy_price = Decimal(row["buy_price"])
        # 下跌时，高位旧买单可能正在成交，必须保留并继续设置止盈；
        # 只裁剪价格上涨后落到当前窗口最远下方的记录。
        if buy_price >= minimum_grid_price:
            continue
        if row["buy_status"] == WAIT_BUY:
            result = cancel_api_order(api, pair, row["buy_order_id"])
            if row["tp_order_id"]:
                cancel_api_order(api, pair, row["tp_order_id"])
            filled = Decimal(str(getattr(result, "filled_amount", None) or "0"))
            if filled > 0:
                store.execute(
                    "UPDATE grid_cycles SET buy_status=?,buy_size=?,remaining_size=?,"
                    "tp_order_id=NULL,tp_order_text=NULL,tp_status=?,in_grid=0,"
                    "updated_at=? WHERE id=?",
                    (
                        BUY_FILLED, decimal_text(filled), decimal_text(filled),
                        TP_NOT_SET, time.time(), row["id"],
                    ),
                )
                print(
                    f"[{now_text()}] 区间外买单部分成交 {filled}，"
                    f"保留持仓并等待止盈：{row['buy_price']}"
                )
            else:
                store.execute("DELETE FROM grid_cycles WHERE id=?", (row["id"],))
                print(f"[{now_text()}] 移除区间外未成交买单并删库：{row['buy_price']}")
        elif row["buy_status"] == BUY_FILLED:
            if not row["in_grid"] and not row["tp_order_id"]:
                continue
            if row["tp_order_id"] and row["tp_status"] == WAIT_TP:
                result = cancel_api_order(api, pair, row["tp_order_id"])
                mark_tp_result(store, row, result)
                refreshed = store.rows(
                    "SELECT * FROM grid_cycles WHERE id=?", (row["id"],)
                )[0]
                if refreshed["completed"]:
                    continue
            store.execute(
                "UPDATE grid_cycles SET in_grid=0,tp_order_id=NULL,"
                "tp_order_text=NULL,tp_status=?,updated_at=? WHERE id=?",
                (TP_NOT_SET, time.time(), row["id"]),
            )
            print(f"[{now_text()}] 区间外已成交买单保留，止盈转为等待设置：{row['buy_price']}")


def reset_pair(api: SpotApi, store: GridStore, pair: str) -> None:
    rows = store.rows(
        "SELECT * FROM grid_cycles WHERE currency_pair=? AND completed=0", (pair,)
    )
    for row in rows:
        if row["buy_status"] == WAIT_BUY:
            cancel_api_order(api, pair, row["buy_order_id"])
        if row["tp_status"] == WAIT_TP:
            cancel_api_order(api, pair, row["tp_order_id"])
    store.clear_pair(pair)
    print(f"[{now_text()}] 已重置 {pair} 数据库状态及其记录的未成交订单")


def run_grid(
    currency_pair: str,
    reference_price,
    margin_amount_per_order,
    leverage="2",
    grid_tag: str = "gdbgrid01",
    buy_client_order_prefix: str = "db",
    sell_client_order_prefix: str = "dt",
    buy_grid_count: int = 10,
    take_profit_count: int = 10,
    take_profit_grid_count: int = 20,
    grid_step_pct="0.003",
    take_profit_pct="0.003",
    auto_borrow: bool = False,
    auto_repay: bool = True,
    startup_mode: str = "resume",
    database_path: Path = DEFAULT_DATABASE,
    poll_interval: float = 2.5,
    max_cycles: Optional[int] = None,
) -> None:
    pair = str(currency_pair).strip().upper().replace("-", "_")
    startup_mode = str(startup_mode).strip().lower()
    if startup_mode not in {"resume", "reset"}:
        raise ValueError("startup_mode 只能是 resume 或 reset")
    if buy_grid_count <= 0 or take_profit_count <= 0 or take_profit_grid_count <= 0:
        raise ValueError("买格、止盈单数量和止盈网格区间必须大于 0")
    if poll_interval < 0.5:
        raise ValueError("poll_interval 不能小于 0.5 秒")
    for name, value in (
        ("grid_tag", grid_tag),
        ("buy_client_order_prefix", buy_client_order_prefix),
        ("sell_client_order_prefix", sell_client_order_prefix),
    ):
        if not value or not value.isascii() or not value.isalnum():
            raise ValueError(f"{name} 只能包含 ASCII 字母和数字")
    if len(buy_client_order_prefix) > 4 or len(sell_client_order_prefix) > 4:
        raise ValueError("订单前缀不能超过4字符")

    reference = decimal_value("reference_price", reference_price)
    margin = decimal_value("margin_amount_per_order", margin_amount_per_order)
    leverage_value = decimal_value("leverage", leverage)
    step_pct = decimal_value("grid_step_pct", grid_step_pct)
    tp_pct = decimal_value("take_profit_pct", take_profit_pct)
    if step_pct >= 1 or tp_pct >= 1:
        raise ValueError("网格间距和止盈比例必须小于 1")

    load_env_file()
    host = os.getenv("GATE_HOST", "https://api.gateio.ws/api/v4").strip()
    live = os.getenv("GATE_LIVE_CONFIRM", "") == LIVE_CONFIRM_TEXT
    public_client = ApiClient(make_configuration(host))
    try:
        public_api = SpotApi(public_client)
        spec = get_pair_spec(public_api, pair)
        current = get_last_price(public_api, pair)
        desired = desired_buy_orders(
            reference, current, step_pct, buy_grid_count, margin, leverage_value, spec
        )
        if not live:
            print(
                f"安全预览：{pair} 当前价={current}，数据库={database_path}，"
                f"启动模式={startup_mode}"
            )
            for order in desired:
                print(
                    f"买价={order.price} 数量={order.size} "
                    f"计划止盈={take_profit_price(order.price, tp_pct, spec)}"
                )
            print(f"实盘需 GATE_LIVE_CONFIRM={LIVE_CONFIRM_TEXT}")
            return
    finally:
        public_client.close()

    process_lock = StrategyProcessLock(
        Path(f"{Path(database_path)}.{pair}.lock")
    )
    api_key = required_env("GATE_API_KEY")
    api_secret = required_env("GATE_API_SECRET")
    main_client = ApiClient(make_configuration(host, api_key, api_secret))
    checker_client = ApiClient(make_configuration(host, api_key, api_secret))
    api = SpotApi(main_client)
    checker_api = SpotApi(checker_client)
    margin_api = MarginApi(main_client)
    store = GridStore(Path(database_path))
    code = strategy_code(
        pair, reference, step_pct, tp_pct, margin, leverage_value, grid_tag
    )
    price_state = PriceState(current)
    desired_lock = threading.Lock()
    desired_state = {order.price for order in desired}

    def get_desired_prices() -> set[Decimal]:
        with desired_lock:
            return set(desired_state)

    stop = threading.Event()
    checker = threading.Thread(
        target=order_checker,
        args=(
            stop, store, checker_api, pair, code, price_state,
            get_desired_prices, sell_client_order_prefix,
        ),
        name=f"grid-check-{pair}",
        daemon=True,
    )
    try:
        process_lock.acquire()
    except Exception:
        store.close()
        checker_client.close()
        main_client.close()
        raise
    try:
        configure_margin_settings(
            margin_api, pair, leverage_value, auto_repay
        )
        if startup_mode == "reset":
            reset_pair(api, store, pair)
        else:
            conflicts = store.unfinished_other_strategies(pair, code)
            if conflicts:
                details = ", ".join(
                    f"{row['strategy_code']}({row['cycle_count']}条)"
                    for row in conflicts
                )
                raise RuntimeError(
                    f"数据库中存在 {pair} 的其他未完成策略：{details}。"
                    "当前参数不能安全恢复；确认撤销旧策略后请使用 startup_mode='reset'。"
                )
        checker.start()
        print(
            f"[{now_text()}] 启动数据库网格：{pair}，模式={startup_mode}，"
            f"数据库={store.path}，策略码={code}"
        )
        cycle = 0
        failures = 0
        balance_cooldown_until = 0.0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            try:
                current = get_last_price(api, pair)
                price_state.set(current)
                desired = desired_buy_orders(
                    reference, current, step_pct, buy_grid_count,
                    margin, leverage_value, spec
                )
                desired_prices = {order.price for order in desired}
                with desired_lock:
                    desired_state.clear()
                    desired_state.update(desired_prices)
                update_grid_flags(store, pair, code, desired_prices)
                trim_outside_grid(api, store, pair, code, desired_prices)
                tp_upper = take_profit_window_upper(
                    reference, current, step_pct, take_profit_grid_count, spec
                )
                reconcile_take_profits(
                    api, store, pair, code, current, tp_upper, spec,
                    sell_client_order_prefix, take_profit_count
                )
                if time.monotonic() >= balance_cooldown_until:
                    funded = place_missing_buys(
                        api, store, pair, code, desired, tp_pct, spec,
                        auto_borrow, buy_client_order_prefix
                    )
                    if not funded:
                        balance_cooldown_until = (
                            time.monotonic() + BALANCE_RETRY_INTERVAL
                        )
                rows = store.active(pair, code)
                pending_buys = sum(row["buy_status"] == WAIT_BUY for row in rows)
                open_tps = sum(row["tp_status"] == WAIT_TP for row in rows)
                waiting_tps = sum(row["tp_status"] == TP_NOT_SET for row in rows)
                dust_tps = sum(row["tp_status"] == TP_DUST for row in rows)
                print(
                    f"[{now_text()}] 当前价={current}，"
                    f"买单={pending_buys}/{buy_grid_count}，"
                    f"止盈单={open_tps}/{take_profit_count}，"
                    f"止盈区间=({current},{tp_upper}]，"
                    f"等待止盈={waiting_tps}，止盈碎片={dust_tps}，"
                    f"活动周期={len(rows)}"
                )
                failures = 0
            except Exception as exc:
                failures += 1
                detail = (
                    f"label={exc.label}, message={exc.message}"
                    if isinstance(exc, GateApiException) else str(exc)
                )
                print(f"[{now_text()}] 主循环失败：{type(exc).__name__}: {detail}")
                if failures >= 5:
                    raise RuntimeError("连续 5 轮失败，自动停止数据库网格") from exc
            if max_cycles is None or cycle < max_cycles:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n收到停止信号；数据库已保存，现有挂单不会自动撤销。")
    finally:
        stop.set()
        if checker.is_alive():
            shutdown_timeout = (
                NETWORK_RETRY_ATTEMPTS * NETWORK_TIMEOUT[1]
                + sum(0.5 * (2 ** attempt)
                      for attempt in range(NETWORK_RETRY_ATTEMPTS - 1))
                + 2
            )
            checker.join(timeout=shutdown_timeout)
        if checker.is_alive():
            print(f"[{now_text()}] 查单线程仍在退出中，暂不关闭其客户端")
        else:
            checker_client.close()
            store.close()
            process_lock.release()
        main_client.close()


def run_SOXLG() -> None:
    run_grid(
        currency_pair="SOXLG_USDT",
        reference_price="150",
        margin_amount_per_order="1.6",
        leverage="2",
        buy_grid_count=10,
        take_profit_count=10,
        take_profit_grid_count=20,
        grid_step_pct="0.003",
        take_profit_pct="0.003",
        auto_borrow=True,
        auto_repay=True,
        startup_mode="resume",  # resume=恢复；reset=撤单并清空当前交易对后重启
        database_path=DEFAULT_DATABASE,
        poll_interval=2,
    )


if __name__ == "__main__":
    run_SOXLG()
