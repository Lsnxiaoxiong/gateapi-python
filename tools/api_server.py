# coding: utf-8
"""FastAPI service exposing historical K-line and contract gain-ranking tools.

Endpoints
---------
- ``GET /api/valid_intervals``  -> list of supported candle intervals.
- ``GET /api/candles``          -> OHLCV candles for one market/pair/interval.
- ``GET /api/rank``             -> yearly-low-gain ranking of active perpetuals.
- ``GET /api/contracts``        -> list active perpetual contracts (name + price).

The service imports the synchronous ``tools`` helpers and runs them in a worker
thread (``run_in_threadpool``) so the ASGI event loop is never blocked. No API
keys are required — all queries use Gate's public market-data endpoints.

Run with::

    python -m tools.api_server                  # default 127.0.0.1:8787
    python tools/api_server.py --host 0.0.0.0 --port 8787

The static Vue3 frontend is served from ``tools/web/dist`` (if present) at ``/``.
"""
from __future__ import absolute_import

import argparse
import copy
import logging
import os
import threading
import time
import uuid

_logger = logging.getLogger(__name__)
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from tools import (
    get_candlesticks,
    rank_futures_by_yearly_low_gain,
    VALID_INTERVALS,
    INTERVAL_SECONDS,
    MAX_LOOKBACK_POINTS,
)

__all__ = ["app", "create_app"]


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _web_dist_dir():
    # The frontend ships as plain static files (Vue3 via CDN/local ESM, no
    # build step) directly under tools/web.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def create_app() -> FastAPI:
    """Build the FastAPI application (factory)."""
    app = FastAPI(
        title="GateAPI K-Line & Ranking Service",
        description="Historical OHLCV candlestick and futures gain-ranking API.",
        version="1.0.0",
    )

    # Background ranking jobs keep the long-running market scan out of the
    # request/response lifecycle. The frontend polls these small snapshots so
    # it can show live progress and is not limited by a single HTTP timeout.
    rank_jobs = {}
    rank_jobs_lock = threading.Lock()

    def _rank_job_snapshot(job_id):
        with rank_jobs_lock:
            job = rank_jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    def _run_rank_job(job_id, settle, max_workers, end_time, limit,
                      min_market_cap_musd, max_market_cap_musd,
                      min_order_value, max_order_value):
        started_at = time.time()
        with rank_jobs_lock:
            job = rank_jobs[job_id]
            job["status"] = "running"
            job["started_at"] = started_at
            if min_market_cap_musd is not None or max_market_cap_musd is not None:
                job["progress"]["phase"] = "filtering"
                job["progress"]["current_contract"] = "正在获取 Gate 合约列表与市值"

        def _partial_result(result):
            with rank_jobs_lock:
                job = rank_jobs.get(job_id)
                if job is None:
                    return
                job["partial_results"].append(copy.deepcopy(result))
                job["partial_results"].sort(
                    key=lambda item: (-(float(item.get("gain_pct") or 0)),
                                      item.get("contract") or ""))

        def _progress(completed, total, contract, error):
            with rank_jobs_lock:
                job = rank_jobs.get(job_id)
                if job is None:
                    return
                progress = job["progress"]
                progress["phase"] = "scanning"
                progress["completed"] = completed
                progress["total"] = total
                progress["current_contract"] = contract
                if error:
                    progress["failed"] += 1
                progress["successful"] = max(0, completed - progress["failed"])
                progress["percent"] = round((completed / total) * 100, 1) if total else 100.0

        try:
            rank_kwargs = dict(
                settle=settle,
                end_time=end_time,
                max_workers=max_workers,
                limit=limit,
                progress_callback=_progress,
                result_callback=_partial_result,
            )
            optional_filters = {
                "min_market_cap_musd": min_market_cap_musd,
                "max_market_cap_musd": max_market_cap_musd,
                "min_order_value": min_order_value,
                "max_order_value": max_order_value,
            }
            rank_kwargs.update((key, value) for key, value in optional_filters.items()
                               if value is not None)
            report = rank_futures_by_yearly_low_gain(**rank_kwargs)
            elapsed = time.time() - started_at
            report.setdefault("stats", {})["elapsed_seconds"] = elapsed
            with rank_jobs_lock:
                job = rank_jobs[job_id]
                stats = report.get("stats", {})
                requested = stats.get("requested_contracts", job["progress"]["total"])
                job["progress"].update({
                    "completed": requested,
                    "total": requested,
                    "successful": stats.get("successful_contracts", 0),
                    "failed": stats.get("failed_contracts", 0),
                    "percent": 100.0,
                    "current_contract": None,
                    "phase": "completed",
                })
                job["status"] = "completed"
                job["finished_at"] = time.time()
                job["report"] = report
        except Exception as exc:  # noqa: BLE001
            _logger.exception("rank job failed job_id=%s", job_id)
            with rank_jobs_lock:
                job = rank_jobs[job_id]
                job["status"] = "failed"
                job["progress"]["phase"] = "failed"
                job["finished_at"] = time.time()
                job["error"] = str(exc)

    # Permissive CORS so the dev Vite server (port 5173) can call the API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/valid_intervals")
    async def valid_intervals():
        """Return the list of supported intervals and their span info."""
        now = int(time.time())
        out = []
        for iv in VALID_INTERVALS:
            sec = INTERVAL_SECONDS[iv]
            max_days = MAX_LOOKBACK_POINTS * sec // 86400
            out.append({
                "interval": iv,
                "seconds": sec,
                "max_lookback_days": max_days,
                "default_span_days": min(365, max_days) if max_days else 365,
            })
        return {"intervals": out, "max_lookback_points": MAX_LOOKBACK_POINTS}

    @app.get("/api/candles")
    async def candles(
        market_type: str = Query("futures", description="spot or futures"),
        trading_pair: str = Query(..., description="e.g. BTC_USDT"),
        interval: str = Query("1h", description="candle interval"),
        span_days: int = Query(365, ge=1, le=36500, description="look-back days"),
        settle: str = Query("usdt", description="futures settlement currency"),
        end_time: int = Query(None, description="end time Unix seconds (default now)"),
    ):
        """Fetch OHLCV candles (run in a worker thread)."""
        try:
            data = await run_in_threadpool(
                get_candlesticks,
                market_type=market_type,
                trading_pair=trading_pair,
                interval=interval,
                span_days=span_days,
                settle=settle,
                end_time=end_time,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc))
        return {
            "market_type": market_type,
            "trading_pair": trading_pair.upper().replace("-", "_"),
            "interval": interval,
            "span_days": span_days,
            "settle": settle,
            "count": len(data),
            "candles": data,
        }

    @app.get("/api/rank")
    async def rank(
        settle: str = Query("usdt"),
        max_workers: int = Query(4, ge=1, le=16),
        end_time: int = Query(None),
        limit: int = Query(None, ge=1, le=10000, description="Scan only the top-N most traded contracts for a fast preview"),
        min_market_cap_musd: float = Query(None, ge=0, description="Minimum coin market cap in millions of USD"),
        max_market_cap_musd: float = Query(None, ge=0, description="Maximum coin market cap in millions of USD"),
        min_order_value: float = Query(None, ge=0, description="Minimum order notional lower bound in settlement currency"),
        max_order_value: float = Query(None, ge=0, description="Minimum order notional upper bound in settlement currency"),
    ):
        """Rank active perpetual contracts by yearly low-gain (worker thread).

        By default this scans every active contract (~900+) and may take
        minutes. Pass ``limit`` (e.g. ``limit=100``) to scan only the most
        actively traded contracts and get a fast interactive preview.
        """
        t0 = time.time()
        _logger.info("rank start settle=%s max_workers=%d limit=%s", settle, max_workers, limit)

        def _progress(completed, total, contract, error):
            if error or (completed % 10 == 0) or completed == total:
                if error:
                    _logger.warning("rank progress %d/%d contract=%s error=%s", completed, total, contract, error)
                else:
                    _logger.info("rank progress %d/%d contract=%s", completed, total, contract)

        try:
            rank_kwargs = dict(
                settle=settle,
                end_time=end_time,
                max_workers=max_workers,
                limit=limit,
                progress_callback=_progress,
            )
            optional_filters = {
                "min_market_cap_musd": min_market_cap_musd,
                "max_market_cap_musd": max_market_cap_musd,
                "min_order_value": min_order_value,
                "max_order_value": max_order_value,
            }
            rank_kwargs.update((key, value) for key, value in optional_filters.items()
                               if value is not None)
            report = await run_in_threadpool(rank_futures_by_yearly_low_gain, **rank_kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            _logger.exception("rank failed")
            raise HTTPException(status_code=500, detail=str(exc))
        report["stats"]["elapsed_seconds"] = time.time() - t0
        _logger.info("rank done results=%d errors=%d elapsed=%.2fs",
                     len(report.get("results", [])),
                     len(report.get("errors", [])),
                     report["stats"]["elapsed_seconds"])
        return report

    @app.post("/api/rank/jobs", status_code=202)
    async def start_rank_job(
        settle: str = Query("usdt"),
        max_workers: int = Query(4, ge=1, le=16),
        end_time: int = Query(None),
        limit: int = Query(None, ge=1, le=10000),
        min_market_cap_musd: float = Query(None, ge=0),
        max_market_cap_musd: float = Query(None, ge=0),
        min_order_value: float = Query(None, ge=0),
        max_order_value: float = Query(None, ge=0),
    ):
        """Start a ranking scan and return a pollable job snapshot immediately."""
        job_id = uuid.uuid4().hex
        created_at = time.time()
        initial_total = int(limit) if limit else 0
        with rank_jobs_lock:
            # Keep memory bounded; completed jobs are cheap but need not live forever.
            if len(rank_jobs) >= 20:
                finished = sorted(
                    (item for item in rank_jobs.items()
                     if item[1].get("status") in ("completed", "failed")),
                    key=lambda item: item[1].get("finished_at", item[1].get("created_at", 0)),
                )
                for old_id, _ in finished[:max(1, len(rank_jobs) - 19)]:
                    rank_jobs.pop(old_id, None)
            rank_jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "created_at": created_at,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "progress": {
                    "completed": 0,
                    "total": initial_total,
                    "successful": 0,
                    "failed": 0,
                    "percent": 0.0,
                    "current_contract": None,
                    "phase": "queued",
                },
                "report": None,
                "partial_results": [],
            }
        worker = threading.Thread(
            target=_run_rank_job,
            args=(job_id, settle, max_workers, end_time, limit,
                  min_market_cap_musd, max_market_cap_musd,
                  min_order_value, max_order_value),
            name="rank-job-%s" % job_id[:8],
            daemon=True,
        )
        worker.start()
        return _rank_job_snapshot(job_id)

    @app.get("/api/rank/jobs/{job_id}")
    async def get_rank_job(job_id: str):
        """Return current progress/result for a background ranking scan."""
        job = _rank_job_snapshot(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Ranking job not found")
        return job

    @app.get("/api/contracts")
    async def contracts(settle: str = Query("usdt")):
        """List active perpetual contracts (name + last_price)."""
        from tools.market_candlesticks import _default_futures_api
        api = _default_futures_api(None)
        try:
            raw = await run_in_threadpool(api.list_futures_contracts, settle)
        finally:
            client = getattr(api, "api_client", None)
            close = getattr(client, "close", None) if client else None
            if callable(close):
                close()
        out = []
        for c in raw:
            if getattr(c, "in_delisting", False):
                continue
            if getattr(c, "status", "") != "trading":
                continue
            out.append({
                "name": getattr(c, "name", ""),
                "last_price": getattr(c, "last_price", None),
            })
        return {"count": len(out), "contracts": out}

    # Serve the static Vue3 frontend (no build step required) at /.
    dist = _web_dist_dir()
    if os.path.isdir(dist):
        # Static asset files served directly at their path (styles.css, js…).
        app.mount("/static", StaticFiles(directory=dist), name="static")

        @app.get("/")
        async def index():
            return FileResponse(
                os.path.join(dist, "index.html"),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            # SPA fallback: any non-API, non-/static path returns index.html.
            if full_path.startswith("api/") or full_path.startswith("static/"):
                raise HTTPException(status_code=404, detail="Not found")
            candidate = os.path.join(dist, full_path)
            if os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(
                os.path.join(dist, "index.html"),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
    else:
        @app.get("/")
        async def index_no_dist():
            return JSONResponse({
                "message": "Frontend not found under tools/web.",
                "api_docs": "/docs",
                "endpoints": ["/api/valid_intervals", "/api/candles",
                              "/api/rank", "/api/contracts"],
            })

    return app


app = create_app()


def main():
    parser = argparse.ArgumentParser(description="GateAPI K-Line & Ranking API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true", help="auto-reload on change")
    args = parser.parse_args()
    import uvicorn
    uvicorn.run("tools.api_server:app", host=args.host, port=args.port,
                reload=args.reload)


if __name__ == "__main__":
    _root = _project_root()
    import sys
    if _root not in sys.path:
        sys.path.insert(0, _root)
    main()
