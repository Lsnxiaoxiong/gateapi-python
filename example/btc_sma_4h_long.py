"""Gate BTC_USDT 永续合约，4 小时 SMA20/100 仅做多。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import websocket
from gate_api import ApiClient, FuturesApi, FuturesOrder, SpotApi
from gate_api.exceptions import ApiException, GateApiException
from urllib3.exceptions import HTTPError

if __package__:
    from . import auto_grid_futures_generic as core
else:
    import auto_grid_futures_generic as core


LIVE_TRADING = False  # 改为 True 后使用 .env 中的 Gate API 凭证
ENTRY_CONTRACTS = "1"  # 开仓张数。杠杆和保证金模式使用 Gate 账户当前设置

CONTRACT = "BTC_USDT"
SETTLE = "usdt"
INTERVAL = "4h"
BAR_SECONDS = 4 * 60 * 60
FAST_PERIOD = 20
SLOW_PERIOD = 100
REST_URL = "https://api.gateio.ws/api/v4"
WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
STATE_PATH = Path(__file__).with_suffix(".sqlite3").resolve()
POLL_SECONDS = 30
CANDLE_DELAY_SECONDS = 5
ORDER_EXPIRY_MS = 30_000
RECOVERY_GRACE_MS = 120_000
LOG = logging.getLogger(__name__)


class WaitForData(RuntimeError):
    """本轮数据不足以安全推进，等待下一轮再检查。"""


@dataclass(frozen=True)
class Signal:
    bar: int
    fast: Decimal
    slow: Decimal
    server_ms: int

    @property
    def long(self) -> bool:
        return self.fast > self.slow


def closed_bar(server_ms: int) -> int:
    return (server_ms // 1000 // BAR_SECONDS - 1) * BAR_SECONDS


def calculate_signal(candles, server_ms: int) -> Signal:
    bar = closed_bar(server_ms)
    if server_ms // 1000 % BAR_SECONDS < CANDLE_DELAY_SECONDS:
        raise WaitForData("等待 Gate 完成最新 4h K 线")
    by_time = {int(candle.t): candle.c for candle in candles}
    times = range(bar - (SLOW_PERIOD - 1) * BAR_SECONDS, bar + 1, BAR_SECONDS)
    if any(timestamp not in by_time for timestamp in times):
        raise WaitForData("缺少最新的连续 100 根已收盘 4h K 线")
    closes = [core.decimal_value("K 线收盘价", by_time[timestamp]) for timestamp in times]
    fast = sum(closes[-FAST_PERIOD:]) / FAST_PERIOD
    slow = sum(closes) / SLOW_PERIOD
    return Signal(bar, fast, slow, server_ms)


@dataclass(frozen=True)
class PendingOrder:
    text: str
    size: Decimal
    submitted_ms: int
    expires_ms: int
    order_id: str | None = None


class State:
    """按 Gate 账户保存策略净多仓和最多一笔待确认委托。"""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.execute("PRAGMA synchronous = FULL")
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS strategy (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    host TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    position TEXT NOT NULL DEFAULT '0',
                    pending_text TEXT,
                    pending_size TEXT,
                    submitted_ms INTEGER,
                    expires_ms INTEGER,
                    order_id TEXT
                )
            """)

    def bind(self, host: str, user_id: int) -> None:
        account = self.db.execute(
            "SELECT host, user_id FROM strategy WHERE id = 1"
        ).fetchone()
        if account is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO strategy (id, host, user_id) VALUES (1, ?, ?)",
                    (host, user_id),
                )
        elif account != (host, user_id):
            raise WaitForData("状态库绑定了另一个 Gate 账户或 API 地址")

    def read(self) -> tuple[Decimal, PendingOrder | None]:
        position, text, size, submitted, expires, order_id = self.db.execute("""
            SELECT position, pending_text, pending_size, submitted_ms, expires_ms, order_id
            FROM strategy WHERE id = 1
        """).fetchone()
        pending = PendingOrder(text, Decimal(size), submitted, expires, order_id) if text else None
        return Decimal(position), pending

    def reserve(self, pending: PendingOrder) -> None:
        with self.db:
            self.db.execute("""
                UPDATE strategy SET pending_text = ?, pending_size = ?, submitted_ms = ?,
                    expires_ms = ? WHERE id = 1
            """, (pending.text, core.decimal_text(pending.size), pending.submitted_ms,
                  pending.expires_ms))

    def remember_order_id(self, order_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE strategy SET order_id = ? WHERE id = 1", (str(order_id),))

    def finish(self, position: Decimal) -> None:
        with self.db:
            self.db.execute("""
                UPDATE strategy SET position = ?, pending_text = NULL, pending_size = NULL,
                    submitted_ms = NULL, expires_ms = NULL, order_id = NULL WHERE id = 1
            """, (core.decimal_text(position),))


class Strategy:
    def __init__(self, api: FuturesApi, spot: SpotApi, host: str,
                 entry_size: Decimal, state: State | None):
        self.api = api
        self.spot = spot
        self.host = host
        self.entry_size = entry_size
        self.state = state
        self.last_signal_bar = None

    def step(self) -> None:
        server_ms = self.spot.get_system_time(_request_timeout=core.NETWORK_TIMEOUT).server_time
        if self.state is None and self.last_signal_bar == closed_bar(server_ms):
            return
        candles = self.api.list_futures_candlesticks(
            SETTLE, CONTRACT, interval=INTERVAL, timezone="utc0",
            limit=SLOW_PERIOD + 5, _request_timeout=core.NETWORK_TIMEOUT,
        )
        signal = calculate_signal(candles, server_ms)
        if signal.bar != self.last_signal_bar:
            close_time = datetime.fromtimestamp(signal.bar + BAR_SECONDS, timezone.utc)
            LOG.info("4h 收盘 %s | SMA20=%.4f SMA100=%.4f | 目标=%s",
                     close_time.isoformat(), signal.fast, signal.slow,
                     "持多" if signal.long else "空仓")
            self.last_signal_bar = signal.bar
        if self.state is not None:
            self.trade(signal)

    def trade(self, signal: Signal) -> None:
        position = self.api.get_position(SETTLE, CONTRACT, _request_timeout=core.NETWORK_TIMEOUT)
        if position.mode != "single" or position.contract != CONTRACT or not position.user:
            raise WaitForData("需使用单向持仓模式，并取得 BTC_USDT 的账户持仓信息")
        actual = Decimal(position.size)
        if not actual.is_finite() or actual < 0:
            raise WaitForData("Gate 返回空头仓位或无效仓位，暂停下单")
        self.state.bind(self.host, position.user)
        owned, pending = self.state.read()
        if pending is not None:
            self.reconcile(pending, owned, actual, signal.server_ms)
            return
        if actual != owned:
            raise WaitForData(f"BTC_USDT 实际持仓 {actual} 张，策略持仓 {owned} 张，暂停下单")
        if signal.long == (owned > 0):
            return

        self.require_no_open_orders()
        size = self.entry_size if signal.long else -owned
        next_close_ms = (signal.bar + 2 * BAR_SECONDS) * 1000
        expires_ms = min(signal.server_ms + ORDER_EXPIRY_MS, next_close_ms - 1)
        pending = PendingOrder(f"t-sma4h-{uuid4().hex[:20]}", size, signal.server_ms, expires_ms)
        order = FuturesOrder(
            contract=CONTRACT, size=core.decimal_text(size), price="0", tif="ioc",
            reduce_only=size < 0, text=pending.text,
        )
        self.state.reserve(pending)
        LOG.info("提交%s %s 张 | 委托标识 %s", "开多" if size > 0 else "平多", abs(size), pending.text)
        result = self.api.create_futures_order(
            SETTLE, order, x_gate_exptime=str(expires_ms), _request_timeout=core.NETWORK_TIMEOUT,
        )
        self.record_result(pending, owned, result)

    def require_no_open_orders(self) -> None:
        options = {"contract": CONTRACT, "limit": 1, "_request_timeout": core.NETWORK_TIMEOUT}
        if (self.api.list_futures_orders(SETTLE, "open", **options)
                or self.api.list_price_triggered_orders(SETTLE, "open", **options)):
            raise WaitForData("BTC_USDT 有其他普通挂单或触发单，暂停下单")

    def reconcile(self, pending: PendingOrder, owned: Decimal, actual: Decimal, server_ms: int) -> None:
        try:
            order = self.api.get_futures_order(
                SETTLE, pending.order_id or pending.text, _request_timeout=core.NETWORK_TIMEOUT,
            )
        except GateApiException as exc:
            if exc.label != "ORDER_NOT_FOUND":
                raise
        else:
            self.record_result(pending, owned, order)
            return

        # Gate 的自定义标识可能查不到较早的零成交订单，过期后再查原委托窗口。
        if server_ms < pending.expires_ms + RECOVERY_GRACE_MS:
            raise WaitForData(f"等待 Gate 确认委托 {pending.text}")
        history = self.api.get_orders_with_time_range(
            SETTLE, contract=CONTRACT, _from=pending.submitted_ms // 1000 - 1,
            to=pending.expires_ms // 1000 + 1, limit=100, _request_timeout=core.NETWORK_TIMEOUT,
        )
        matches = [order for order in history if order.text == pending.text]
        if len(history) >= 100 or len(matches) > 1:
            raise WaitForData(f"委托 {pending.text} 的历史查询结果不明确，暂停下单")
        if matches:
            self.record_result(pending, owned, matches[0])
            return
        if actual != owned:
            raise WaitForData(f"委托 {pending.text} 未查到且仓位已变化，暂停下单")
        self.require_no_open_orders()
        self.state.finish(owned)
        LOG.warning("委托 %s 已过期，历史无记录且仓位未变，本轮关闭恢复记录", pending.text)

    def record_result(self, pending: PendingOrder, owned: Decimal, order: FuturesOrder) -> None:
        if (order.contract != CONTRACT or order.text != pending.text
                or Decimal(order.size) != pending.size
                or not order.id or (pending.order_id and str(order.id) != pending.order_id)):
            raise WaitForData(f"Gate 返回的委托与本地记录不一致 {pending.text}")
        self.state.remember_order_id(order.id)
        if order.status != "finished":
            raise WaitForData(f"等待 Gate 委托 {order.id} 完成")
        remaining = abs(Decimal(order.left))
        if not remaining.is_finite() or remaining > abs(pending.size):
            raise WaitForData(f"Gate 委托 {order.id} 返回无效未成交张数")
        filled = abs(pending.size) - remaining
        new_position = owned + (filled if pending.size > 0 else -filled)
        self.state.finish(new_position)
        LOG.info("委托 %s 已结束 | 成交 %s 张 | 策略持仓 %s 张", order.id, filled, new_position)


class CandleNotifications:
    """WebSocket 只负责唤醒；K 线收盘价和连续性由 REST 确认。"""

    def __init__(self):
        self.url = os.getenv("GATE_FUTURES_WS_URL", "").strip() or WS_URL
        self.proxy_options = {}
        proxy = urlsplit(os.getenv("GATE_PROXY", "").strip())
        if proxy.hostname:
            self.proxy_options = {
                "http_proxy_host": proxy.hostname,
                "http_proxy_port": proxy.port,
                "proxy_type": proxy.scheme,
            }
            if proxy.username is not None:
                self.proxy_options["http_proxy_auth"] = (
                    unquote(proxy.username), unquote(proxy.password or ""),
                )
        self.socket = None
        self.next_connect = 0.0
        self.last_message = 0.0
        self.latest_bar = -1

    def close(self) -> None:
        if self.socket is not None:
            self.socket.shutdown()
            self.socket = None

    def wait(self, seconds: float) -> bool:
        now = time.monotonic()
        if self.socket is None and now < self.next_connect:
            time.sleep(min(seconds, self.next_connect - now))
            return False
        try:
            if self.socket is None:
                self.socket = websocket.create_connection(
                    self.url, timeout=min(seconds, core.NETWORK_TIMEOUT[1]), **self.proxy_options,
                )
                self.socket.send(json.dumps({
                    "time": int(time.time()), "channel": "futures.candlesticks",
                    "event": "subscribe", "payload": [INTERVAL, CONTRACT],
                }))
                self.last_message = time.monotonic()
            self.socket.settimeout(min(seconds, 5))
            payload = self.socket.recv()
            if not payload:
                raise ConnectionError("WebSocket 已关闭")
            message = json.loads(payload)
            if message.get("error"):
                raise ConnectionError(f"订阅出错 {message['error']}")
            self.last_message = time.monotonic()
            if message.get("channel") != "futures.candlesticks":
                return False
            if message.get("event") == "subscribe" and message["result"]["status"] == "success":
                LOG.info("已订阅 Gate WebSocket %s %s K 线", CONTRACT, INTERVAL)
            if message.get("event") == "update":
                bar = max((int(row["t"]) for row in message["result"]), default=-1)
                if bar > self.latest_bar:
                    self.latest_bar = bar
                    return True
        except websocket.WebSocketTimeoutException:
            if self.socket is not None and time.monotonic() - self.last_message < 60:
                return False
            self.reconnect("WebSocket 行情超时")
        except (OSError, websocket.WebSocketException, ValueError, KeyError, TypeError) as exc:
            self.reconnect(str(exc))
        return False

    def reconnect(self, reason: str) -> None:
        self.close()
        self.next_connect = time.monotonic() + POLL_SECONDS
        LOG.warning("%s | 等待重连，期间使用 REST", reason)


def run(strategy: Strategy) -> None:
    notifications = CandleNotifications()
    next_check = 0.0
    try:
        while True:
            remaining = next_check - time.monotonic()
            if remaining > 0:
                if notifications.wait(remaining):
                    next_check = min(next_check, time.monotonic() + CANDLE_DELAY_SECONDS)
                continue
            try:
                strategy.step()
            except WaitForData as exc:
                LOG.warning("%s", exc)
            except (ApiException, HTTPError, OSError) as exc:
                LOG.warning("Gate 请求未完成，本轮结束 | %s",
                            getattr(exc, "label", None) or getattr(exc, "status", None) or exc)
            next_check = time.monotonic() + POLL_SECONDS
    finally:
        notifications.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    core.load_env_file()
    host = os.getenv("GATE_HOST", REST_URL).strip().rstrip("/")
    size = core.decimal_value("ENTRY_CONTRACTS", ENTRY_CONTRACTS)
    key = core.required_env("GATE_API_KEY") if LIVE_TRADING else None
    secret = core.required_env("GATE_API_SECRET") if LIVE_TRADING else None
    with ApiClient(core.make_configuration(host, key, secret)) as client:
        client.set_default_header("X-Gate-Size-Decimal", "1")
        strategy = Strategy(FuturesApi(client), SpotApi(client), host, size, None)
        LOG.info("%s %s SMA20/100 | %s | 开多 %s 张",
                 CONTRACT, INTERVAL, "实盘" if LIVE_TRADING else "仅预览行情与信号", size)
        if not LIVE_TRADING:
            run(strategy)
            return

        lock = core.StrategyProcessLock(STATE_PATH.with_suffix(".sqlite3.lock"))
        lock.acquire()
        try:
            spec = strategy.api.get_futures_contract(
                SETTLE, CONTRACT, _request_timeout=core.NETWORK_TIMEOUT,
            )
            minimum = core.decimal_value("order_size_min", spec.order_size_min)
            step = minimum if spec.enable_decimal else Decimal(1)
            maximum = Decimal(spec.market_order_size_max or spec.order_size_max)
            if spec.status != "trading" or not minimum <= size <= maximum or size % step:
                raise ValueError("ENTRY_CONTRACTS 不符合 BTC_USDT 当前市价委托张数规则")
            with closing(sqlite3.connect(str(STATE_PATH))) as db:
                strategy.state = State(db)
                run(strategy)
        finally:
            lock.release()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOG.info("策略已停止")
