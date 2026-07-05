"""Jinja2 rendering: the dashboard page (classed, sharp-corner CSS) and the
plain-HTML per-item block embedded in the feed's content:encoded (readers
sanitize CSS/JS, so that one is semantic HTML with absolute URLs only)."""

from __future__ import annotations

import pathlib
from typing import Any, Dict, List

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def _fmt_ts(iso: Any) -> str:
    if not iso:
        return ""
    return str(iso)[:16].replace("T", " ")


_env.filters["ts"] = _fmt_ts


def render_dashboard(ctx: Dict[str, Any]) -> str:
    return _env.get_template("dashboard.html").render(**ctx)


def render_feed_item_html(item: Dict[str, Any]) -> str:
    """Plain-HTML block for content:encoded."""
    return _env.get_template("feed_item.html").render(item=item)


def render_feed_items(items: List[Dict[str, Any]]) -> Dict[int, str]:
    return {it["id"]: render_feed_item_html(it) for it in items}
