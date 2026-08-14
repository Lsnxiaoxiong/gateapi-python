"""Gate 逐仓杠杆现货、SQLite 持久化网格。

一条数据库记录代表一个完整周期：买单 -> 止盈卖单。主循环只补现价下方最近
的 ``buy_grid_count`` 个买格；``GridChecker`` 维护买卖窗口并撤销区间外挂单；
``OrderListener`` 订阅 ``spot.orders``，实时推进成交状态并为成交买单挂止盈。
买单尚未成交时只保存计划止盈价，避免卖单自动借入基础币形成意外空头。

启动模式：
- ``resume``：从当前交易对已有数据库状态恢复。
- ``reset``：撤销数据库记录的当前交易对未成交订单，清空其记录并重新初始化。

实盘必须设置 ``GATE_LIVE_CONFIRM=RUN_REAL_MARGIN_DB_GRID``。
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
from types import SimpleNamespace
from typing import Callable, Optional, TypeVar
from urllib.parse import urlparse

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
GRID_CHECK_INTERVAL = 1.0
ORDER_STATE_CHECK_INTERVAL = 1.0
WEBSOCKET_RECONNECT_INTERVAL = 2.0
TP_SUBMISSION_STALE_SECONDS = 60.0
ORDER_SNAPSHOT_LIMIT = 100
ORPHAN_ORDER_GRACE_SECONDS = 15
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = SCRIPT_DIR / "grid_orders.sqlite3"
T = TypeVar("T")

BUY_OPEN = "已挂单、未成交"
WAIT_BUY = BUY_OPEN
BUY_FILLED = "已成交"
BUY_CANCELLED = "买单已取消"
POSITION_NO_TP = "未设置止盈单"
POSITION_WAIT_TP_ORDER = "等待挂止盈单"
POSITION_WAIT_TP = "已设置止盈、等待止盈"
POSITION_TP_FILLED = "止盈已成交"
TP_NOT_SET = "未设置止盈单"
TP_PLANNED = TP_NOT_SET
TP_WAIT_PLACE = "等待挂单"
TP_SUBMITTING = "正在挂单"
TP_OPEN = "止盈单已挂单"
WAIT_TP = TP_OPEN
TP_FILLED = "止盈已成交"
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
    return [
        PlannedOrder(price, calculate_order_size(margin, leverage, price, spec))
        for price in desired_buy_prices(
            reference, current, step_pct, count, spec.price_step
        )
    ]


def desired_buy_prices(
    reference: Decimal,
    current: Decimal,
    step_pct: Decimal,
    count: int,
    price_step: Decimal,
) -> list[Decimal]:
    """独立计算现价下方最近的锚定网格价格。"""
    start = first_buy_index(reference, current, step_pct, price_step)
    result: list[Decimal] = []
    index = start
    while len(result) < count:
        price = buy_grid_price(reference, step_pct, index, price_step)
        index += 1
        if price <= 0:
            raise RuntimeError("网格价格小于等于 0")
        if result and result[-1] == price:
            continue
        result.append(price)
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
    max_unclosed_per_grid: int = 2,
) -> str:
    exposure_suffix = (
        "" if max_unclosed_per_grid == 2
        else f"|max-unclosed={max_unclosed_per_grid}"
    )
    raw = (
        f"{grid_tag}|db-margin|{currency_pair}|{decimal_text(reference)}|"
        f"{decimal_text(step_pct)}|{decimal_text(take_profit_pct)}|"
        f"{decimal_text(margin)}|{decimal_text(leverage)}{exposure_suffix}"
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
                    position_status TEXT NOT NULL DEFAULT '未设置止盈单',
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
                CREATE INDEX IF NOT EXISTS idx_grid_order_ids
                ON grid_cycles(buy_order_id, tp_order_id);
                CREATE INDEX IF NOT EXISTS idx_grid_pending
                ON grid_cycles(
                    currency_pair, strategy_code, completed, buy_status, tp_status
                );
                """
            )
            columns = {
                str(row[1]) for row in self.db.execute("PRAGMA table_info(grid_cycles)")
            }
            if "position_status" not in columns:
                self.db.execute(
                    "ALTER TABLE grid_cycles ADD COLUMN position_status TEXT "
                    "NOT NULL DEFAULT '未设置止盈单'"
                )
            # 兼容旧版数据库中的状态文本；订单 ID 和持仓数量保持不变。
            self.db.execute(
                "UPDATE grid_cycles SET buy_status=? WHERE buy_status=?",
                (BUY_OPEN, "等待买单成交"),
            )
            self.db.execute(
                "UPDATE grid_cycles SET buy_status=? WHERE buy_status=?",
                (BUY_FILLED, "买单成交"),
            )
            self.db.execute(
                "UPDATE grid_cycles SET position_status=?,tp_status=? "
                "WHERE tp_order_id IS NOT NULL AND tp_status=?",
                (POSITION_WAIT_TP, TP_OPEN, "等待止盈"),
            )
            self.db.execute(
                "UPDATE grid_cycles SET position_status=?,tp_status=? "
                "WHERE completed=1 AND tp_status=?",
                (POSITION_TP_FILLED, TP_FILLED, "止盈单成交"),
            )
            self.db.execute(
                "UPDATE grid_cycles SET position_status=?,tp_status=? "
                "WHERE buy_status=? AND tp_order_id IS NULL AND completed=0 "
                "AND tp_status=?",
                (POSITION_NO_TP, TP_NOT_SET, BUY_OPEN, "已设置止盈单"),
            )
            self.db.execute(
                "UPDATE grid_cycles SET position_status=?,tp_status=? "
                "WHERE buy_status=? AND tp_order_id IS NULL AND completed=0 "
                "AND tp_status IN (?,?,?)",
                (
                    POSITION_WAIT_TP_ORDER, TP_WAIT_PLACE, BUY_FILLED,
                    "未设置止盈、等待设置止盈", "已设置止盈单",
                    TP_CANCELLED,
                ),
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

    def one(self, row_id: int) -> Optional[sqlite3.Row]:
        rows = self.rows("SELECT * FROM grid_cycles WHERE id=?", (row_id,))
        return rows[0] if rows else None

    def find_order(
        self, pair: str, code: str, order_id: str, side: str
    ) -> Optional[sqlite3.Row]:
        column = "buy_order_id" if side == "buy" else "tp_order_id"
        rows = self.rows(
            f"SELECT * FROM grid_cycles WHERE currency_pair=? AND strategy_code=? "
            f"AND {column}=? ORDER BY id DESC LIMIT 1",
            (pair, code, str(order_id)),
        )
        return rows[0] if rows else None

    def find_order_text(
        self, pair: str, code: str, order_text: str, side: str
    ) -> Optional[sqlite3.Row]:
        column = "buy_order_text" if side == "buy" else "tp_order_text"
        rows = self.rows(
            f"SELECT * FROM grid_cycles WHERE currency_pair=? AND strategy_code=? "
            f"AND {column}=? ORDER BY id DESC LIMIT 1",
            (pair, code, str(order_text)),
        )
        return rows[0] if rows else None

    def claim_take_profit(self, row_id: int) -> bool:
        now = time.time()
        with self.lock:
            cursor = self.db.execute(
                "UPDATE grid_cycles SET tp_status=?,position_status=?,updated_at=? "
                "WHERE id=? AND completed=0 AND buy_status=? AND tp_order_id IS NULL "
                "AND tp_status IN (?,?,?,?)",
                (
                    TP_SUBMITTING, POSITION_WAIT_TP_ORDER, now, row_id, BUY_FILLED,
                    TP_WAIT_PLACE, TP_NOT_SET, TP_CANCELLED, TP_PLANNED,
                ),
            )
            self.db.commit()
            return cursor.rowcount == 1

    def release_take_profit_claim(self, row_id: int) -> None:
        self.execute(
            "UPDATE grid_cycles SET tp_status=?,position_status=?,updated_at=? "
            "WHERE id=? AND tp_status=?",
            (
                TP_WAIT_PLACE, POSITION_WAIT_TP_ORDER, time.time(), row_id,
                TP_SUBMITTING,
            ),
        )

    def recover_stale_take_profit_claims(
        self, pair: str, code: str, stale_seconds: float
    ) -> int:
        with self.lock:
            cursor = self.db.execute(
                "UPDATE grid_cycles SET tp_status=?,position_status=?,updated_at=? "
                "WHERE currency_pair=? AND strategy_code=? AND completed=0 "
                "AND tp_status=? AND tp_order_id IS NULL AND updated_at<=?",
                (
                    TP_WAIT_PLACE, POSITION_WAIT_TP_ORDER, time.time(), pair, code,
                    TP_SUBMITTING, time.time() - max(0.0, stale_seconds),
                ),
            )
            self.db.commit()
            return cursor.rowcount

    def set_take_profit_open(self, row_id: int, order) -> None:
        with self.lock:
            cursor = self.db.execute(
                "UPDATE grid_cycles SET tp_order_id=?,tp_order_text=?,tp_status=?,"
                "position_status=?,updated_at=? WHERE id=? AND completed=0 "
                "AND tp_status=?",
                (
                    str(order.id), str(order.text or ""), TP_OPEN,
                    POSITION_WAIT_TP, time.time(), row_id, TP_SUBMITTING,
                ),
            )
            self.db.commit()
            if cursor.rowcount != 1:
                raise RuntimeError(f"止盈挂单写库抢占已失效：记录={row_id}")

    def attach_missing_take_profit(
        self,
        pair: str,
        code: str,
        order_id: str,
        order_text: str,
        price: Decimal,
        size: Decimal,
    ) -> Optional[sqlite3.Row]:
        """恢复已在 Gate 挂出、但进程崩溃前尚未来得及写库的止盈单。"""
        with self.lock:
            existing = self.db.execute(
                "SELECT * FROM grid_cycles WHERE currency_pair=? "
                "AND strategy_code=? AND tp_order_id=? LIMIT 1",
                (pair, code, str(order_id)),
            ).fetchone()
            if existing:
                return existing
            candidate = self.db.execute(
                "SELECT * FROM grid_cycles WHERE currency_pair=? "
                "AND strategy_code=? AND completed=0 AND buy_status=? "
                "AND tp_order_id IS NULL AND tp_price=? AND remaining_size=? "
                "AND tp_status IN (?,?,?,?) ORDER BY updated_at,id LIMIT 1",
                (
                    pair, code, BUY_FILLED, decimal_text(price),
                    decimal_text(size), TP_SUBMITTING, TP_WAIT_PLACE,
                    TP_NOT_SET, TP_CANCELLED,
                ),
            ).fetchone()
            if candidate is None:
                return None
            cursor = self.db.execute(
                "UPDATE grid_cycles SET tp_order_id=?,tp_order_text=?,"
                "tp_status=?,position_status=?,updated_at=? WHERE id=? "
                "AND completed=0 AND tp_order_id IS NULL",
                (
                    str(order_id), str(order_text or ""), TP_OPEN,
                    POSITION_WAIT_TP, time.time(), candidate["id"],
                ),
            )
            self.db.commit()
            if cursor.rowcount != 1:
                return None
            return self.db.execute(
                "SELECT * FROM grid_cycles WHERE id=?", (candidate["id"],)
            ).fetchone()

    def insert_buy(
        self, pair: str, code: str, order, planned: PlannedOrder, tp: Decimal
    ) -> int:
        now = time.time()
        with self.lock:
            existing = self.db.execute(
                "SELECT id FROM grid_cycles WHERE currency_pair=? "
                "AND strategy_code=? AND buy_order_id=? ORDER BY id DESC LIMIT 1",
                (pair, code, str(order.id)),
            ).fetchone()
            if existing:
                return int(existing[0])
            cursor = self.db.execute(
                "INSERT INTO grid_cycles(currency_pair,strategy_code,buy_price,"
                "buy_size,remaining_size,buy_order_id,buy_order_text,buy_status,"
                "position_status,tp_price,tp_status,in_grid,completed,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
                (
                    pair, code, decimal_text(planned.price),
                    decimal_text(planned.size), decimal_text(planned.size),
                    str(order.id), str(order.text or ""), BUY_OPEN,
                    POSITION_NO_TP, decimal_text(tp), TP_NOT_SET, now, now,
                ),
            )
            self.db.commit()
            return int(cursor.lastrowid)

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


def cancel_api_order(
    api: SpotApi,
    pair: str,
    order_id: Optional[str],
    ignore_absent: bool = False,
):
    """撤销订单；重置流程可把已关闭或不存在的订单视为已经撤销。"""
    if not order_id:
        return None
    try:
        return api.cancel_order(
            order_id, pair, account="margin", _request_timeout=NETWORK_TIMEOUT
        )
    except GateApiException as exc:
        if exc.label not in {"ORDER_NOT_FOUND", "ORDER_CLOSED", "ORDER_CANCELLED"}:
            raise
        if ignore_absent:
            print(
                f"[{now_text()}] 重置跳过已关闭或不存在的订单："
                f"id={order_id}，label={exc.label}"
            )
            return None
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


def order_filled_amount(result) -> Decimal:
    raw = getattr(result, "filled_amount", None)
    if raw is not None:
        return Decimal(str(raw or "0"))
    amount = Decimal(str(getattr(result, "amount", None) or "0"))
    left = Decimal(str(getattr(result, "left", None) or "0"))
    return max(Decimal("0"), amount - left)


def mark_buy_result(store: GridStore, row, result) -> Optional[sqlite3.Row]:
    current = store.one(int(row["id"]))
    if current is None:
        return None
    if str(current["buy_order_id"] or "") != str(row["buy_order_id"] or ""):
        return current
    row = current
    filled = order_filled_amount(result)
    finished = result.status in {"closed", "cancelled"}
    if finished and filled > 0:
        if row["buy_status"] == BUY_FILLED:
            store.execute(
                "UPDATE grid_cycles SET last_checked_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            return store.one(row["id"])
        size = filled
        store.execute(
            "UPDATE grid_cycles SET buy_status=?,buy_size=?,remaining_size=?,"
            "position_status=?,tp_status=?,last_checked_at=?,updated_at=? WHERE id=?",
            (
                BUY_FILLED, decimal_text(size), decimal_text(size),
                POSITION_WAIT_TP_ORDER, TP_WAIT_PLACE, time.time(), time.time(),
                row["id"],
            ),
        )
        print(f"[{now_text()}] 买单成交：id={row['buy_order_id']}，数量={size}")
    elif finished:
        store.execute("DELETE FROM grid_cycles WHERE id=?", (row["id"],))
        return None
    else:
        store.execute(
            "UPDATE grid_cycles SET last_checked_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
    return store.one(row["id"])


def mark_tp_result(store: GridStore, row, result) -> Optional[sqlite3.Row]:
    current = store.one(int(row["id"]))
    if current is None or current["completed"]:
        return current
    if str(current["tp_order_id"] or "") != str(row["tp_order_id"] or ""):
        return current
    row = current
    filled = order_filled_amount(result)
    remaining = Decimal(row["remaining_size"])
    finished = result.status in {"closed", "cancelled"}
    if finished and filled >= remaining:
        store.execute(
            "UPDATE grid_cycles SET remaining_size='0',tp_status=?,"
            "position_status=?,completed=1,last_checked_at=?,updated_at=? WHERE id=?",
            (
                TP_FILLED, POSITION_TP_FILLED, time.time(), time.time(), row["id"],
            ),
        )
        print(f"[{now_text()}] 止盈成交：id={row['tp_order_id']}，周期完成")
    elif finished:
        remaining = max(Decimal("0"), remaining - filled)
        store.execute(
            "UPDATE grid_cycles SET remaining_size=?,tp_order_id=NULL,"
            "tp_order_text=NULL,tp_status=?,position_status=?,last_checked_at=?,"
            "updated_at=? WHERE id=?",
            (
                decimal_text(remaining), TP_WAIT_PLACE, POSITION_WAIT_TP_ORDER,
                time.time(), time.time(), row["id"],
            ),
        )
    else:
        store.execute(
            "UPDATE grid_cycles SET last_checked_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
    return store.one(row["id"])


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


class OrderListener:
    """监听 ``spot.orders``，实时推进买入与止盈状态，REST 仅作断线校验。"""

    channel_name = "spot.orders"
    account_name = "margin"

    def __init__(
        self,
        stop: threading.Event,
        store: GridStore,
        api: SpotApi,
        pair: str,
        code: str,
        spec: PairSpec,
        take_profit_pct: Decimal,
        buy_prefix: str,
        sell_prefix: str,
        ws_url: str,
    ):
        self.stop = stop
        self.store = store
        self.api = api
        self.pair = pair
        self.code = code
        self.spec = spec
        self.take_profit_pct = take_profit_pct
        self.buy_prefix = buy_prefix
        self.sell_prefix = sell_prefix
        self.ws_url = ws_url
        self.pending_events: dict[str, tuple[dict, float]] = {}

    def _strategy_text(self, side: str) -> str:
        prefix = self.buy_prefix if side == "buy" else self.sell_prefix
        return f"t-{prefix}{self.code}"

    def _belongs_to_strategy(self, event: dict) -> bool:
        side = str(event.get("side", ""))
        return (
            str(event.get("currency_pair", "")) == self.pair
            and side in {"buy", "sell"}
            and str(event.get("text", "")).startswith(self._strategy_text(side))
        )

    def _event_from_order(self, order) -> dict:
        return {
            "id": str(order.id),
            "text": str(getattr(order, "text", "") or ""),
            "currency_pair": str(
                getattr(order, "currency_pair", self.pair) or self.pair
            ),
            "account": str(
                getattr(order, "account", self.account_name) or self.account_name
            ),
            "side": str(getattr(order, "side", "")),
            "status": str(getattr(order, "status", "")),
            "filled_amount": decimal_text(order_filled_amount(order)),
            "amount": str(getattr(order, "amount", "0") or "0"),
            "left": str(getattr(order, "left", "0") or "0"),
            "price": str(getattr(order, "price", "0") or "0"),
        }

    def _insert_missing_buy(self, event: dict) -> bool:
        try:
            price = decimal_value("WebSocket买单价格", event.get("price"))
            size = decimal_value("WebSocket买单数量", event.get("amount"))
        except ValueError as exc:
            print(f"[{now_text()}] OrderListener 无法补建买单记录：{exc}")
            return False
        order = SimpleNamespace(
            id=str(event.get("id", "")), text=str(event.get("text", "") or "")
        )
        row_id = self.store.insert_buy(
            self.pair, self.code, order, PlannedOrder(price, size),
            take_profit_price(price, self.take_profit_pct, self.spec),
        )
        print(
            f"[{now_text()}] OrderListener 补建买单记录："
            f"价格={price}，数量={size}，id={order.id}，记录={row_id}"
        )
        return True

    def _attach_missing_take_profit(self, event: dict) -> bool:
        if str(event.get("side", "")) != "sell":
            return False
        try:
            price = decimal_value("止盈卖单价格", event.get("price"))
            size = decimal_value("止盈卖单数量", event.get("amount"))
        except ValueError:
            return False
        row = self.store.attach_missing_take_profit(
            self.pair, self.code, str(event.get("id", "")),
            str(event.get("text", "") or ""), price, size,
        )
        if row is None:
            return False
        print(
            f"[{now_text()}] OrderStateChecker 恢复止盈卖单关联："
            f"记录={row['id']}，卖价={price}，数量={size}，"
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
        side = str(event.get("side", ""))
        if side not in {"buy", "sell"}:
            return False
        order_id = str(event.get("id", ""))
        order_text = str(event.get("text", "") or "")
        row = self.store.find_order(self.pair, self.code, order_id, side)
        if row is None and order_text:
            row = self.store.find_order_text(
                self.pair, self.code, order_text, side
            )
        if row is None:
            return False
        result = SimpleNamespace(**event)
        if side == "buy":
            updated = mark_buy_result(self.store, row, result)
            if (
                updated is not None
                and updated["buy_status"] == BUY_FILLED
                and not updated["tp_order_id"]
            ):
                submit_take_profit(
                    self.api, self.store, updated, self.pair, self.code,
                    self.spec, self.sell_prefix,
                )
        else:
            mark_tp_result(self.store, row, result)
        return True

    def _receive_event(self, event: dict) -> None:
        if self.handle_order_update(event):
            return
        if (
            self._belongs_to_strategy(event)
            and self._attach_missing_take_profit(event)
            and self.handle_order_update(event)
        ):
            return
        if (
            self._belongs_to_strategy(event)
            and str(event.get("side", "")) == "buy"
            and self._insert_missing_buy(event)
            and self.handle_order_update(event)
        ):
            return
        if self._belongs_to_strategy(event):
            self.pending_events[str(event.get("id", ""))] = (
                event, time.monotonic() + ORPHAN_ORDER_GRACE_SECONDS
            )

    def _flush_pending_events(self) -> None:
        now = time.monotonic()
        for order_id, (event, deadline) in list(self.pending_events.items()):
            if self.handle_order_update(event) or now >= deadline:
                self.pending_events.pop(order_id, None)

    def reconcile_rest(self, exact_missing: bool = False) -> None:
        opened, finished = list_order_snapshot(self.api, self.pair)
        # 活动买单可能在进程崩溃前已提交、但尚未来得及写库；按订单 ID 幂等补建。
        for order in opened:
            event = self._event_from_order(order)
            if self._belongs_to_strategy(event):
                self._receive_event(event)
        # 历史完成订单只更新已有记录，避免 reset 后重新导入旧策略历史。
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
                    result = api_order(self.api, self.pair, order_id)
                except Exception as exc:
                    print(
                        f"[{now_text()}] OrderListener 补查订单 {order_id} 失败：{exc}"
                    )
                    continue
            if result is None:
                continue
            event = self._event_from_order(result)
            if not event["side"]:
                event["side"] = kind if kind == "buy" else "sell"
            self.handle_order_update(event)
        cancel_orphan_take_profits(
            self.api, self.store, self.pair, self.code, self.sell_prefix, opened
        )
        recovered = self.store.recover_stale_take_profit_claims(
            self.pair, self.code, TP_SUBMISSION_STALE_SECONDS
        )
        if recovered:
            print(
                f"[{now_text()}] OrderListener 恢复超时止盈挂单状态："
                f"{recovered} 条"
            )

    def _subscription(self, api_key: str, api_secret: str) -> str:
        timestamp = int(time.time())
        channel = "spot.orders"
        event = "subscribe"
        payload = f"channel={channel}&event={event}&time={timestamp}"
        signature = hmac.new(
            api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512
        ).hexdigest()
        return json.dumps({
            "time": timestamp,
            "channel": channel,
            "event": event,
            "payload": [self.pair],
            "auth": {"method": "api_key", "KEY": api_key, "SIGN": signature},
        })

    @staticmethod
    def _proxy_options() -> dict:
        proxy = os.getenv("GATE_PROXY", "").strip()
        if not proxy:
            return {}
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
                f"[{now_text()}] OrderListener 无法启动：缺少 websocket-client；"
                "请执行 pip install websocket-client"
            )
            self.stop.set()
            return
        while not self.stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(
                    self.ws_url,
                    timeout=NETWORK_TIMEOUT[1],
                    **self._proxy_options(),
                )
                ws.send(self._subscription(api_key, api_secret))
                ws.settimeout(1)
                last_ping = time.monotonic()
                print(
                    f"[{now_text()}] OrderListener 已订阅 "
                    f"{self.pair} {self.channel_name}"
                )
                while not self.stop.is_set():
                    try:
                        raw = ws.recv()
                        if not raw:
                            raise ConnectionError("WebSocket 连接已关闭")
                        message = json.loads(raw)
                        if message.get("error"):
                            raise RuntimeError(f"WebSocket 返回错误：{message['error']}")
                        if message.get("event") == "update":
                            result = message.get("result") or []
                            for event in result if isinstance(result, list) else [result]:
                                if isinstance(event, dict):
                                    self._receive_event(event)
                    except websocket.WebSocketTimeoutException:
                        pass
                    self._flush_pending_events()
                    now = time.monotonic()
                    if now - last_ping >= 20:
                        ws.ping()
                        last_ping = now
            except Exception as exc:
                if not self.stop.is_set():
                    print(f"[{now_text()}] OrderListener 断线：{exc}；准备重连")
                    self.stop.wait(WEBSOCKET_RECONNECT_INTERVAL)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass


class OrderStateChecker:
    """唯一的 REST 订单对账线程，每秒把 Gate 实际状态同步到数据库。"""

    def __init__(
        self,
        stop: threading.Event,
        reconciler: OrderListener,
        interval: float = ORDER_STATE_CHECK_INTERVAL,
    ):
        if interval <= 0:
            raise ValueError("订单状态检查间隔必须大于 0")
        self.stop = stop
        self.reconciler = reconciler
        self.interval = interval

    def run(self) -> None:
        print(
            f"[{now_text()}] OrderStateChecker 已启动："
            f"每 {self.interval:g} 秒同步一次订单状态"
        )
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                # 先批量查询 open/finished；数据库未完成但不在快照中的订单按 ID 补查。
                self.reconciler.reconcile_rest(exact_missing=True)
            except Exception as exc:
                detail = (
                    f"label={exc.label}, message={exc.message}"
                    if isinstance(exc, GateApiException) else str(exc)
                )
                print(f"[{now_text()}] OrderStateChecker 对账失败：{detail}")
            elapsed = time.monotonic() - started
            self.stop.wait(max(0, self.interval - elapsed))


class GridChecker:
    """维护现价上下方网格窗口，不承担订单成交状态轮询。"""

    def __init__(
        self,
        stop: threading.Event,
        store: GridStore,
        api: SpotApi,
        pair: str,
        code: str,
        price_state: PriceState,
        reference: Decimal,
        step_pct: Decimal,
        buy_grid_count: int,
        take_profit_grid_count: int,
        take_profit_count: int,
        spec: PairSpec,
        sell_prefix: str,
        max_unclosed_per_grid: int = 2,
    ):
        self.stop = stop
        self.store = store
        self.api = api
        self.pair = pair
        self.code = code
        self.price_state = price_state
        self.reference = reference
        self.step_pct = step_pct
        self.buy_grid_count = buy_grid_count
        self.take_profit_grid_count = take_profit_grid_count
        self.take_profit_count = take_profit_count
        self.spec = spec
        self.sell_prefix = sell_prefix
        self.max_unclosed_per_grid = max_unclosed_per_grid

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                current = self.price_state.get()
                desired_prices = set(desired_buy_prices(
                    self.reference, current, self.step_pct,
                    self.buy_grid_count, self.spec.price_step,
                ))
                update_grid_flags(
                    self.store, self.pair, self.code, desired_prices
                )
                trim_outside_grid(
                    self.api, self.store, self.pair, self.code, desired_prices,
                    self.max_unclosed_per_grid,
                )
                upper = take_profit_window_upper(
                    self.reference, current, self.step_pct,
                    self.take_profit_grid_count, self.spec,
                )
                reconcile_take_profits(
                    self.api, self.store, self.pair, self.code, current, upper,
                    self.spec, self.sell_prefix, self.take_profit_count,
                )
            except Exception as exc:
                detail = (
                    f"label={exc.label}, message={exc.message}"
                    if isinstance(exc, GateApiException) else str(exc)
                )
                print(f"[{now_text()}] GridChecker 本轮失败：{detail}")
            self.stop.wait(GRID_CHECK_INTERVAL)


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
    max_unclosed_per_grid: int = 2,
) -> bool:
    rows = store.active(pair, code)
    pending_prices = {
        Decimal(row["buy_price"]) for row in rows if row["buy_status"] == WAIT_BUY
    }
    for planned in desired:
        if (
            store.exposure_count(pair, code, planned.price)
            >= max_unclosed_per_grid
        ):
            print(
                f"[{now_text()}] {planned.price} 已有"
                f"{max_unclosed_per_grid}组未止盈持仓，跳过新买单"
            )
            continue
        if planned.price in pending_prices:
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


def submit_take_profit(
    api: SpotApi,
    store: GridStore,
    row,
    pair: str,
    code: str,
    spec: PairSpec,
    prefix: str,
) -> bool:
    """为一个已成交买单挂止盈；数据库抢占确保监听器和检查器不会重复下单。"""
    refreshed = store.one(int(row["id"]))
    if (
        refreshed is None
        or refreshed["completed"]
        or refreshed["buy_status"] != BUY_FILLED
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
            (TP_DUST, POSITION_WAIT_TP_ORDER, time.time(), refreshed["id"]),
        )
        print(
            f"[{now_text()}] 止盈剩余转为碎片：记录={refreshed['id']}，"
            f"数量={size}，金额={decimal_text(notional)} USDT"
        )
        return False
    if not store.claim_take_profit(int(refreshed["id"])):
        return False
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
                "UPDATE grid_cycles SET tp_status=?,position_status=?,updated_at=? "
                "WHERE id=?",
                (TP_DUST, POSITION_WAIT_TP_ORDER, time.time(), refreshed["id"]),
            )
            print(
                f"[{now_text()}] Gate拒绝过小止盈，已转为碎片："
                f"记录={refreshed['id']}，数量={size}，金额="
                f"{decimal_text(notional)} USDT"
            )
            return False
        store.release_take_profit_claim(int(refreshed["id"]))
        if exc.label == "BALANCE_NOT_ENOUGH":
            print(f"[{now_text()}] 基础币余额不足，暂不能设置止盈：记录{refreshed['id']}")
            return False
        raise
    except Exception:
        store.release_take_profit_claim(int(refreshed["id"]))
        raise
    try:
        store.set_take_profit_open(int(refreshed["id"]), order)
    except Exception:
        cancel_api_order(api, pair, str(order.id))
        store.release_take_profit_claim(int(refreshed["id"]))
        raise
    print(
        f"[{now_text()}] 设置止盈：买价={refreshed['buy_price']}，"
        f"卖价={price}，id={order.id}"
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
            or row["tp_status"] in {TP_OPEN, TP_SUBMITTING}
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
                "tp_status=?,position_status=?,updated_at=? WHERE id=?",
                (
                    TP_DUST, POSITION_WAIT_TP_ORDER, time.time(), row["id"],
                ),
            )
            print(
                f"[{now_text()}] 止盈剩余转为碎片：记录={row['id']}，"
                f"数量={size}，金额={decimal_text(notional)} USDT，"
                f"最低金额={decimal_text(spec.min_quote_amount)} USDT"
            )

    eligible = [
        row for row in store.active(pair, code)
        if row["buy_status"] == BUY_FILLED
        and row["tp_status"] in {
            TP_NOT_SET, TP_WAIT_PLACE, TP_CANCELLED, TP_PLANNED
        }
        and current < Decimal(row["tp_price"]) <= upper_price
        and Decimal(row["remaining_size"]) > 0
    ]
    # 已挂止盈也参与排序，使有限名额始终留给最接近现价的止盈价。
    eligible.extend(
        row for row in store.active(pair, code)
        if row["buy_status"] == BUY_FILLED
        and row["tp_status"] == TP_OPEN
        and current < Decimal(row["tp_price"]) <= upper_price
        and Decimal(row["remaining_size"]) > 0
    )
    eligible.sort(key=lambda row: Decimal(row["tp_price"]) - current)
    selected_ids = {row["id"] for row in eligible[:limit]}

    for row in store.active(pair, code):
        if row["tp_status"] != TP_OPEN or row["id"] in selected_ids:
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
        if row["tp_status"] in {TP_OPEN, TP_SUBMITTING} or row["completed"]:
            continue
        submit_take_profit(api, store, row, pair, code, spec, prefix)


def trim_outside_grid(
    api: SpotApi,
    store: GridStore,
    pair: str,
    code: str,
    desired_prices: set[Decimal],
    max_unclosed_per_grid: int = 2,
) -> None:
    outside = [
        row for row in store.active(pair, code)
        if row["buy_status"] == BUY_OPEN
        and (
            Decimal(row["buy_price"]) not in desired_prices
            or store.exposure_count(
                pair, code, Decimal(row["buy_price"])
            ) >= max_unclosed_per_grid
        )
    ]
    # 先撤销距离当前窗口最远的挂单，数据库与交易所逐条保持一致。
    window_center = (min(desired_prices) + max(desired_prices)) / Decimal("2")
    outside.sort(
        key=lambda row: abs(Decimal(row["buy_price"]) - window_center),
        reverse=True,
    )
    for row in outside:
        buy_price = Decimal(row["buy_price"])
        blocked_by_exposure = (
            store.exposure_count(pair, code, buy_price)
            >= max_unclosed_per_grid
        )
        result = cancel_api_order(api, pair, row["buy_order_id"])
        updated = mark_buy_result(store, row, result)
        if updated is not None and updated["buy_status"] == BUY_FILLED:
            filled = Decimal(updated["buy_size"])
            store.execute(
                "UPDATE grid_cycles SET in_grid=0,updated_at=? WHERE id=?",
                (time.time(), updated["id"]),
            )
            print(
                f"[{now_text()}] 区间外买单部分成交 {filled}，"
                f"保留持仓并等待止盈：{buy_price}"
            )
        else:
            reason = (
                f"同价位已有{max_unclosed_per_grid}组未止盈持仓"
                if blocked_by_exposure else "超出买格区间"
            )
            print(f"[{now_text()}] 移除未成交买单并删库：{buy_price}，原因={reason}")


def reset_pair(api: SpotApi, store: GridStore, pair: str) -> None:
    rows = store.rows(
        "SELECT * FROM grid_cycles WHERE currency_pair=? AND completed=0", (pair,)
    )
    for row in rows:
        if row["buy_status"] == WAIT_BUY:
            cancel_api_order(
                api, pair, row["buy_order_id"], ignore_absent=True
            )
        if row["tp_status"] == WAIT_TP:
            cancel_api_order(
                api, pair, row["tp_order_id"], ignore_absent=True
            )
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
    take_profit_grid_count: int = 10,
    max_unclosed_per_grid: int = 2,
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
    if max_unclosed_per_grid <= 0:
        raise ValueError("max_unclosed_per_grid 必须大于 0")
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
    ws_url = os.getenv("GATE_WS_URL", "wss://api.gateio.ws/ws/v4/").strip()
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
    listener_client = ApiClient(make_configuration(host, api_key, api_secret))
    state_client = ApiClient(make_configuration(host, api_key, api_secret))
    api = SpotApi(main_client)
    checker_api = SpotApi(checker_client)
    listener_api = SpotApi(listener_client)
    state_api = SpotApi(state_client)
    margin_api = MarginApi(main_client)
    try:
        process_lock.acquire()
    except Exception:
        state_client.close()
        listener_client.close()
        checker_client.close()
        main_client.close()
        raise
    try:
        store = GridStore(Path(database_path))
    except Exception:
        process_lock.release()
        state_client.close()
        listener_client.close()
        checker_client.close()
        main_client.close()
        raise
    code = strategy_code(
        pair, reference, step_pct, tp_pct, margin, leverage_value, grid_tag,
        max_unclosed_per_grid,
    )
    price_state = PriceState(current)

    stop = threading.Event()
    grid_checker = GridChecker(
        stop, store, checker_api, pair, code, price_state, reference, step_pct,
        buy_grid_count, take_profit_grid_count, take_profit_count, spec,
        sell_client_order_prefix, max_unclosed_per_grid,
    )
    order_listener = OrderListener(
        stop, store, listener_api, pair, code, spec, tp_pct,
        buy_client_order_prefix, sell_client_order_prefix, ws_url,
    )
    state_reconciler = OrderListener(
        stop, store, state_api, pair, code, spec, tp_pct,
        buy_client_order_prefix, sell_client_order_prefix, ws_url,
    )
    order_state_checker = OrderStateChecker(stop, state_reconciler)
    checker = threading.Thread(
        target=grid_checker.run,
        name=f"grid-check-{pair}",
        daemon=True,
    )
    listener = threading.Thread(
        target=order_listener.run,
        args=(api_key, api_secret),
        name=f"order-listener-{pair}",
        daemon=True,
    )
    state_sync = threading.Thread(
        target=order_state_checker.run,
        name=f"order-state-check-{pair}",
        daemon=True,
    )
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
        state_sync.start()
        listener.start()
        checker.start()
        print(
            f"[{now_text()}] 启动数据库网格：{pair}，模式={startup_mode}，"
            f"数据库={store.path}，策略码={code}"
        )
        cycle = 0
        failures = 0
        balance_cooldown_until = 0.0
        while not stop.is_set() and (max_cycles is None or cycle < max_cycles):
            cycle += 1
            try:
                current = get_last_price(api, pair)
                price_state.set(current)
                desired = desired_buy_orders(
                    reference, current, step_pct, buy_grid_count,
                    margin, leverage_value, spec
                )
                tp_upper = take_profit_window_upper(
                    reference, current, step_pct, take_profit_grid_count, spec
                )
                if time.monotonic() >= balance_cooldown_until:
                    funded = place_missing_buys(
                        api, store, pair, code, desired, tp_pct, spec,
                        auto_borrow, buy_client_order_prefix,
                        max_unclosed_per_grid,
                    )
                    if not funded:
                        balance_cooldown_until = (
                            time.monotonic() + BALANCE_RETRY_INTERVAL
                        )
                rows = store.active(pair, code)
                pending_buys = sum(row["buy_status"] == WAIT_BUY for row in rows)
                open_tps = sum(row["tp_status"] == WAIT_TP for row in rows)
                waiting_tps = sum(
                    row["tp_status"] in {
                        TP_NOT_SET, TP_WAIT_PLACE, TP_SUBMITTING, TP_CANCELLED
                    }
                    for row in rows
                )
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
                stop.wait(poll_interval)
    except KeyboardInterrupt:
        print("\n收到停止信号；数据库已保存，现有挂单不会自动撤销。")
    finally:
        stop.set()
        if checker.is_alive() or listener.is_alive() or state_sync.is_alive():
            shutdown_timeout = (
                NETWORK_RETRY_ATTEMPTS * NETWORK_TIMEOUT[1]
                + sum(0.5 * (2 ** attempt)
                      for attempt in range(NETWORK_RETRY_ATTEMPTS - 1))
                + 2
            )
            checker.join(timeout=shutdown_timeout)
            listener.join(timeout=shutdown_timeout)
            state_sync.join(timeout=shutdown_timeout)
        if checker.is_alive() or listener.is_alive() or state_sync.is_alive():
            print(f"[{now_text()}] 异步线程仍在退出中，暂不关闭其客户端")
        else:
            state_client.close()
            listener_client.close()
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
        take_profit_grid_count=10,
        max_unclosed_per_grid=2,  # 单个网格价位最多保留的已成交未止盈周期数
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
