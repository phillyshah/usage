"""Auto-resolve metric: % of cells that came back confident, per week.

The 'getting better' curve from the spec. Computed from the per-field snapshots
present in the DB (recent weeks, within retention) — high-confidence cells over
total cells, grouped by the ISO week of the ticket's creation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.db import db


def auto_resolve_by_week(weeks: int = 8) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    # ticket_id -> created_at week label
    ticket_week: dict[str, str] = {}
    for t in db.backend.select("tickets"):
        ca = t.get("created_at")
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            continue
        iso = dt.isocalendar()
        ticket_week[t["ticket_id"]] = f"{iso[0]}-W{iso[1]:02d}"

    total: dict[str, int] = defaultdict(int)
    confident: dict[str, int] = defaultdict(int)
    for fe in db.backend.select("field_extractions"):
        week = ticket_week.get(fe.get("ticket_id"))
        if not week:
            continue
        total[week] += 1
        if (fe.get("confidence") or "low").lower() == "high":
            confident[week] += 1

    out = []
    for week in sorted(total.keys()):
        n = total[week]
        pct = round(100.0 * confident[week] / n, 1) if n else 0.0
        out.append({"week": week, "pct_confident": pct})
    return out
