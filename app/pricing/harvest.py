"""Learn from a reviewed step-5 workbook.

Step 5 fills blank prices; this is what happens when the reviewed file comes
back through step 4. The point is that a price learned here is offered at
*extraction* time on the next batch (``db.price_suggestion``, used by
``assemble.py``), so a blank that was filled once need never be blank again.

What makes that safe is knowing which numbers a person stood behind. Step 5
records everything it wrote (``pricing_runs.cells``) and stamps the run id into
the workbook's custom properties, so the returned file can be diffed against it:

  * **changed** — the reviewer typed something else. A human decision, learned
    as ``correction``, and nothing may later overwrite it.
  * **unchanged and green** — taken verbatim from the hospital price list and
    left alone. Learned as ``price_list``, and dropped again the next time a
    price list is uploaded, because that upload is a full replace and this
    store is never purged.
  * **unchanged and rose** — an estimate nobody touched. Still a guess. Learning
    it would let the tool treat its own arithmetic as evidence and then feed
    that back into the next estimate, so it is deliberately skipped.
"""
from __future__ import annotations

import logging

from app.db import db

log = logging.getLogger("pricing.harvest")


def _money(value):
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    return None


def harvest_prices(wb, run: dict) -> dict:
    """Diff a returned workbook's Price column against what the run wrote.

    ``wb`` is an already-loaded workbook; ``run`` is the ``pricing_runs`` row.
    """
    counts = {"corrected": 0, "confirmed": 0, "estimates_skipped": 0,
              "unchanged_kept": 0}
    cells = run.get("cells") or []
    if isinstance(cells, str):           # some backends hand jsonb back as text
        import json
        try:
            cells = json.loads(cells)
        except ValueError:
            cells = []
    if not cells or "Usage" not in wb.sheetnames:
        return counts

    ws = wb["Usage"]
    headers = {str(c.value).strip(): i for i, c in enumerate(ws[1], start=1)
               if c.value is not None}
    price_col = headers.get("Price")
    hosp_col = headers.get("Hospital")
    if not price_col or not hosp_col:
        return counts

    for rec in cells:
        row = rec.get("row")
        ref = rec.get("ref")
        if not row or not ref:
            continue
        # The hospital as it is written on the sheet now — the reviewer may have
        # corrected that too, and the price belongs to whatever it says today.
        hosp = ws.cell(row=row, column=hosp_col).value
        hospital = str(hosp).strip() if hosp else (rec.get("hospital") or "")
        if not hospital:
            continue

        current = _money(ws.cell(row=row, column=price_col).value)
        if current is None:
            continue                      # blanked out again; nothing to learn
        written = _money(rec.get("value"))

        if written is not None and abs(current - written) < 0.005:
            if rec.get("kind") == "direct":
                if db.learn_price(ref, hospital, current, source="price_list"):
                    counts["confirmed"] += 1
                else:
                    counts["unchanged_kept"] += 1
            else:
                counts["estimates_skipped"] += 1
            continue

        db.learn_price(ref, hospital, current, source="correction")
        counts["corrected"] += 1

    log.info("pricing harvest for run %s: %s", run.get("run_id"), counts)
    return counts
