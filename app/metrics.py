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


# ---------------------------------------------------------------------------
# Daily views (History tab) — real dates, computed on the fly, no migration.
# ---------------------------------------------------------------------------
def _parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _day(s) -> str | None:
    dt = _parse_dt(s)
    return dt.astimezone(timezone.utc).date().isoformat() if dt else None


def _recent_tickets(days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for t in db.backend.select("tickets"):
        dt = _parse_dt(t.get("created_at"))
        if dt and dt >= cutoff:
            out.append(t)
    return out


def auto_resolve_by_day(days: int = 14) -> list[dict]:
    """Share of fields read confidently, bucketed by calendar day (UTC).

    Reads each recent ticket's field_extractions via the predicate-pushing
    find_all (not the capped select()), so today's activity always shows.
    Days with no fields are skipped. Returns real ISO dates.
    """
    total: dict[str, int] = defaultdict(int)
    confident: dict[str, int] = defaultdict(int)
    for t in _recent_tickets(days):
        day = _day(t.get("created_at"))
        if not day:
            continue
        for fe in db.backend.find_all("field_extractions", "ticket_id", t["ticket_id"]):
            total[day] += 1
            if (fe.get("confidence") or "low").lower() == "high":
                confident[day] += 1
    out = []
    for day in sorted(total.keys()):
        n = total[day]
        out.append({
            "date": day,
            "pct_confident": round(100.0 * confident[day] / n, 1) if n else 0.0,
            "fields": n,
            "confident": confident[day],
        })
    return out


def corrections_by_day(days: int = 14) -> dict[str, dict]:
    """Per-day corrections the human made: total, blanks filled, guesses fixed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_day: dict[str, dict] = defaultdict(
        lambda: {"corrections_made": 0, "blanks_filled": 0, "low_conf_fixed": 0})
    for r in db.backend.select("corrections_audit"):
        dt = _parse_dt(r.get("corrected_at"))
        if not dt or dt < cutoff:
            continue
        day = dt.astimezone(timezone.utc).date().isoformat()
        by_day[day]["corrections_made"] += 1
        if r.get("was_blank"):
            by_day[day]["blanks_filled"] += 1
        if r.get("was_low_conf"):
            by_day[day]["low_conf_fixed"] += 1
    return by_day


# (table, timestamp column, output key) for the learning stores.
_LEARNING_TABLES = [
    ("learning_price", "last_seen", "prices"),
    ("learning_part_desc", "updated_at", "part_descriptions"),
    ("learning_rep_map", "updated_at", "reps"),
    ("learning_gtin_xref", "updated_at", "gtin_links"),
]


def facts_learned_by_day(days: int = 14) -> dict[str, dict]:
    """Per-day count of facts the tool learned/refreshed (by last-touched stamp)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_day: dict[str, dict] = defaultdict(
        lambda: {k: 0 for _, _, k in _LEARNING_TABLES})
    for table, stamp, key in _LEARNING_TABLES:
        for r in db.backend.select(table):
            dt = _parse_dt(r.get(stamp))
            if not dt or dt < cutoff:
                continue
            day = dt.astimezone(timezone.utc).date().isoformat()
            by_day[day][key] += 1
    return by_day


def learning_totals() -> dict[str, int]:
    """Cumulative size of each learning store — the headline 'what it knows'."""
    counts = db.list_learning_counts()
    return {key: counts.get(table, 0) for table, _, key in _LEARNING_TABLES}


def learning_timeline(days: int = 14) -> dict:
    """Combined History payload: cumulative totals + per-day impact."""
    corr = corrections_by_day(days)
    facts = facts_learned_by_day(days)
    days_set = sorted(set(corr) | set(facts), reverse=True)
    daily = []
    for day in days_set:
        c = corr.get(day, {"corrections_made": 0, "blanks_filled": 0, "low_conf_fixed": 0})
        f = facts.get(day, {k: 0 for _, _, k in _LEARNING_TABLES})
        daily.append({"date": day, **c, "facts_learned": f})
    return {"cumulative": learning_totals(), "daily": daily}
