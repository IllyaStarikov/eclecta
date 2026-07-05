"""FastAPI server: parameterized RSS + review dashboard. Pure reader —
opens read-only SQLite connections per request and never writes, so the
worker can crash, restart, or write freely without affecting serving."""

from __future__ import annotations

import datetime
import pathlib
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, db as db_mod, feed as feed_mod, render as render_mod

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


def _conn_or_none(cfg) -> Optional[sqlite3.Connection]:
    try:
        return db_mod.connect_ro(cfg.db_path)
    except db_mod.DBError:
        return None


def _health_ctx(conn: Optional[sqlite3.Connection]):
    h = {
        "sources_enabled": 0,
        "sources_verified": 0,
        "clusters": 0,
        "curated": 0,
        "spend_today": 0.0,
        "last_ingest": None,
        "last_curate": None,
        "failing_sources": [],
        "recent": [],
    }
    if conn is None:
        return h
    h["sources_enabled"] = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE enabled=1"
    ).fetchone()[0]
    h["sources_verified"] = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE enabled=1 AND verified_at IS NOT NULL"
    ).fetchone()[0]
    h["clusters"] = conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
    h["curated"] = conn.execute(
        "SELECT COUNT(*) FROM curations WHERE status='done' AND skip=0"
    ).fetchone()[0]
    row = conn.execute(
        "SELECT cli_usd + api_usd AS total FROM spend WHERE day=date('now')"
    ).fetchone()
    h["spend_today"] = float(row["total"]) if row and row["total"] else 0.0
    for job, key in (("ingest", "last_ingest"), ("curate", "last_curate")):
        r = conn.execute(
            "SELECT ts FROM health WHERE job=? ORDER BY id DESC LIMIT 1", (job,)
        ).fetchone()
        h[key] = r["ts"] if r else None
    h["failing_sources"] = [
        dict(r)
        for r in conn.execute(
            "SELECT slug, error_count, last_error FROM sources "
            "WHERE error_count >= 3 ORDER BY error_count DESC LIMIT 12"
        ).fetchall()
    ]
    h["recent"] = [
        dict(r)
        for r in conn.execute(
            "SELECT ts, job, level, message FROM health ORDER BY id DESC LIMIT 8"
        ).fetchall()
    ]
    return h


def create_app(cfg) -> FastAPI:
    app = FastAPI(title="Signal", version=__version__, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def feed_response(request: Request, params: dict) -> Response:
        conn = _conn_or_none(cfg)
        if conn is None:
            raise HTTPException(503, "database not initialized; run ingest first")
        try:
            items = feed_mod.query_items(conn, cfg, params)
            html = render_mod.render_feed_items(items)
            xml = feed_mod.render_rss(items, html, cfg, str(request.url))
            return Response(
                content=xml, media_type="application/rss+xml; charset=utf-8"
            )
        finally:
            conn.close()

    @app.get("/feed.xml")
    def feed_xml(
        request: Request,
        channel: Optional[str] = None,
        topic: Optional[str] = None,
        min_score: Optional[float] = Query(None, ge=0, le=10),
        min_relevance: Optional[int] = Query(None, ge=1, le=10),
        since: Optional[str] = None,
        limit: Optional[int] = Query(None, ge=1),
        sources: Optional[str] = None,
    ):
        return feed_response(
            request,
            {
                "channel": channel,
                "topic": topic,
                "min_score": min_score,
                "min_relevance": min_relevance,
                "since": since,
                "limit": limit,
                "sources": sources,
            },
        )

    @app.get("/feed/{channel}.xml")
    def feed_channel(request: Request, channel: str):
        if channel not in cfg.channels:
            raise HTTPException(404, "unknown channel %r" % channel)
        return feed_response(request, {"channel": channel})

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        channel: Optional[str] = None,
        min_score: Optional[float] = None,
        limit: int = Query(60, ge=1, le=200),
    ):
        conn = _conn_or_none(cfg)
        items = []
        digest_row = None
        try:
            if conn is not None:
                items = feed_mod.query_items(
                    conn, cfg,
                    {"channel": channel, "min_score": min_score, "limit": limit},
                )
                digest_row = conn.execute(
                    "SELECT kind, period_key, body_html, staged_path, "
                    "promoted FROM digests "
                    "ORDER BY generated_at DESC LIMIT 1"
                ).fetchone()
            html = render_mod.render_dashboard(
                {
                    "items": items,
                    "channels": cfg.channels,
                    "active_channel": channel,
                    "health": _health_ctx(conn),
                    "digest": dict(digest_row) if digest_row else None,
                    "auto_refresh": int(cfg.server.get("auto_refresh_sec", 120)),
                    "version": __version__,
                }
            )
            return HTMLResponse(html)
        finally:
            if conn is not None:
                conn.close()

    @app.get("/opml")
    def opml():
        path = cfg.sources_opml
        if not path.exists():
            raise HTTPException(404, "sources.opml not generated yet")
        return FileResponse(
            str(path), media_type="text/x-opml; charset=utf-8",
            filename="signal-sources.opml",
        )

    @app.get("/healthz")
    def healthz():
        conn = _conn_or_none(cfg)
        try:
            ok = conn is not None
            body = {
                "ok": ok,
                "version": __version__,
                "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            if ok:
                body.update(_health_ctx(conn))
                # JSONResponse can't serialize Row remnants; ensure plain types
                body["failing_sources"] = body["failing_sources"][:5]
                body["recent"] = body["recent"][:5]
            return JSONResponse(body, status_code=200 if ok else 503)
        finally:
            if conn is not None:
                conn.close()

    return app


def run(cfg, host: Optional[str] = None, port: Optional[int] = None) -> int:
    import uvicorn

    uvicorn.run(
        create_app(cfg),
        host=host or cfg.server.get("host", "127.0.0.1"),
        port=int(port or cfg.server.get("port", 8765)),
        log_level="info",
    )
    return 0
