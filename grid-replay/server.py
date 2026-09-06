from __future__ import annotations

import csv
import gzip
import json
import mimetypes
import os
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backtest_engine import run_backtest
from csv_index import CsvIndex
from replay_paths import DEFAULT_CSV_PATH

ROOT = Path(__file__).resolve().parent
CSV_PATH = Path(os.getenv("GRID_REPLAY_CSV", str(DEFAULT_CSV_PATH))).resolve()
SYMBOL = os.getenv("GRID_REPLAY_SYMBOL", CSV_PATH.name.split("-")[0])
DIST_DIR = ROOT / "ui/dist"


INDEX = CsvIndex(CSV_PATH)
BACKTEST_JOBS: dict[str, dict] = {}
BACKTEST_JOBS_LOCK = threading.Lock()


def _update_job(job_id: str, **values) -> None:
    with BACKTEST_JOBS_LOCK:
        job = BACKTEST_JOBS.get(job_id)
        if job is not None:
            job.update(values)


def _run_backtest_job(job_id: str, payload: dict) -> None:
    try:
        def on_progress(progress: dict) -> None:
            _update_job(job_id, **progress)

        result = run_backtest(INDEX, payload, progress_callback=on_progress)
        _update_job(job_id, status="done", progress=100.0, result=result)
    except Exception as exc:
        _update_job(
            job_id,
            status="error",
            error=f"回测失败: {type(exc).__name__}: {exc}",
        )


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def normalize_row(row: list[str]) -> list[float | int]:
    return [
        int(row[0]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = "GridReplay/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json_bytes(payload)
        use_gzip = len(body) >= 64 * 1024 and "gzip" in self.headers.get("Accept-Encoding", "").lower()
        if use_gzip:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if path.suffix in {".js", ".css"}:
            self.send_header("Cache-Control", "public, max-age=3600")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/backtest", "/api/backtest/start"}:
            self._send_json({"error": "接口不存在"}, 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
            if content_length <= 0 or content_length > 1_000_000:
                self._send_json({"error": "请求体为空或过大"}, 400)
                return
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")

            if parsed.path == "/api/backtest":
                result = run_backtest(INDEX, payload)
                self._send_json(result)
                return

            job_id = uuid.uuid4().hex
            with BACKTEST_JOBS_LOCK:
                BACKTEST_JOBS[job_id] = {
                    "status": "running",
                    "progress": 0.0,
                    "processedMinutes": 0,
                    "targetMinutes": 0,
                    "generatedFrames": 0,
                    "frameLimit": int(payload.get("frameLimit", 2000) or 2000),
                    "currentTime": 0,
                }
            threading.Thread(
                target=_run_backtest_job,
                args=(job_id, payload),
                name=f"backtest-{job_id[:8]}",
                daemon=True,
            ).start()
            self._send_json({"jobId": job_id, "status": "running"}, 202)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": f"回测失败: {type(exc).__name__}: {exc}"}, 500)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/backtest/status":
            qs = parse_qs(parsed.query)
            job_id = str(qs.get("jobId", [""])[0])
            with BACKTEST_JOBS_LOCK:
                job = BACKTEST_JOBS.get(job_id)
                if job is None:
                    snapshot = None
                else:
                    snapshot = dict(job)
            if snapshot is None:
                self._send_json({"error": "回测任务不存在"}, 404)
                return
            self._send_json(snapshot)
            return

        if parsed.path == "/api/meta":
            self._send_json({
                "symbol": SYMBOL,
                "interval": "1m",
                "total": INDEX.total_rows,
                "firstTime": INDEX.first_time,
                "lastTime": INDEX.last_time,
                "source": str(CSV_PATH),
            })
            return
        if parsed.path == "/api/seek":
            qs = parse_qs(parsed.query)
            try:
                timestamp = int(qs.get("timestamp", [str(INDEX.first_time)])[0])
            except ValueError:
                self._send_json({"error": "timestamp 必须是毫秒时间戳"}, 400)
                return
            self._send_json({"offset": INDEX.row_for_timestamp(timestamp)})
            return
        if parsed.path == "/api/klines":
            qs = parse_qs(parsed.query)
            try:
                offset = int(qs.get("offset", ["0"])[0])
                limit = int(qs.get("limit", ["5000"])[0])
            except ValueError:
                self._send_json({"error": "offset/limit 必须是整数"}, 400)
                return
            rows = INDEX.rows(offset, limit)
            self._send_json({
                "offset": max(0, offset),
                "nextOffset": max(0, offset) + len(rows),
                "rows": [normalize_row(row) for row in rows],
            })
            return

        if not DIST_DIR.is_dir():
            self._send_json({
                "error": "Vue 前端尚未构建",
                "hint": "进入 grid-replay/ui 执行 npm install && npm run build",
            }, 503)
            return

        rel = parsed.path.lstrip("/") or "index.html"
        candidate = (DIST_DIR / rel).resolve()
        if DIST_DIR.resolve() not in candidate.parents and candidate != DIST_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if candidate.is_file():
            self._send_file(candidate)
            return
        self._send_file(DIST_DIR / "index.html")


def main() -> None:
    host = os.getenv("GRID_REPLAY_HOST", "127.0.0.1")
    port = int(os.getenv("GRID_REPLAY_PORT", "18888"))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"K线: {CSV_PATH}")
    print(f"行数: {INDEX.total_rows:,}")
    print(f"网格回放页面: {url}")
    if os.getenv("GRID_REPLAY_NO_BROWSER", "") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
