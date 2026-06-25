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


# ---------------------------------------------------------------------------
# Learning health — a passive safeguard that the learning stores stay intact.
#
# The learning tables are append/upsert-only; nothing in the codebase deletes
# from them. So a per-store "high-water mark" (the largest row count ever seen,
# persisted in app_settings) gives us a data-loss tripwire: if the current count
# ever drops below the mark, something erased learned facts and we flag it red.
# ---------------------------------------------------------------------------
_HWM_PREFIX = "learning_hwm_"

# How many days without a single new/refreshed fact before we call it "idle".
STALE_DAYS = 30


def _store_stats() -> list[dict]:
    """Per-store {table, key, count, last_learned} via one table_stats call each
    (count=exact + newest stamp), reusing the same specs as list_learning_counts."""
    out = []
    for table, stamp, key in _LEARNING_TABLES:
        s = db.backend.table_stats(table, stamp_col=stamp)
        out.append({"table": table, "key": key,
                    "count": s.get("rows", 0) or 0,
                    "last_learned": s.get("updated_at")})
    return out


def bump_learning_watermarks() -> None:
    """Raise each store's high-water mark to its current count (never lower it).

    Called at startup, on the daily scheduler tick, and after a corrections
    upload — every legitimate moment the stores can grow.
    """
    for s in _store_stats():
        prev = _read_hwm(s["table"])
        if prev is None or s["count"] > prev:
            db.set_app_setting(_HWM_PREFIX + s["table"], str(s["count"]))


def _read_hwm(table: str) -> int | None:
    """Stored high-water mark for a store, or None if never recorded yet."""
    raw = db.get_app_setting(_HWM_PREFIX + table)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def learning_health() -> dict:
    """Pure read: compare each store's current count to its high-water mark.

    status: 'empty' (nothing learned yet) | 'at_risk' (a store shrank below its
    mark — possible data loss) | 'stale' (intact but no new facts in STALE_DAYS)
    | 'ok' (intact and recently growing). A missing mark defaults to the current
    count (first-run baseline), so a fresh deploy is never falsely flagged.
    """
    tables = []
    total = 0
    any_shrunk = False
    newest: str | None = None
    for s in _store_stats():
        count = s["count"]
        total += count
        # Missing mark -> treat current as the baseline (never a shrink).
        mark = _read_hwm(s["table"])
        hwm = count if mark is None else mark
        shrunk = count < hwm
        any_shrunk = any_shrunk or shrunk
        last = s["last_learned"]
        if last and (newest is None or last > newest):
            newest = last
        tables.append({"key": s["key"], "count": count, "hwm": hwm,
                       "shrunk": shrunk, "last_learned": last})

    # A shrink is the loudest signal and wins even if the store is now empty
    # (a wipe-to-zero is still data loss, not a never-used store).
    if any_shrunk:
        status = "at_risk"
    elif total == 0:
        status = "empty"
    elif _is_stale(newest):
        status = "stale"
    else:
        status = "ok"

    return {"status": status, "total": total, "last_learned": newest,
            "tables": tables}


def _is_stale(last_learned: str | None) -> bool:
    if not last_learned:
        return True
    dt = _parse_dt(last_learned)
    if not dt:
        return True
    return dt < datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
