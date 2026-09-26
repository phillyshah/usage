"""Closing the loop: a reviewed step-5 workbook teaches the next extraction.

The payoff is that ``db.price_suggestion`` is consulted during extraction
(``assemble.py``), filling a blank price for the same hospital before step 5
ever runs. The risk is the mirror image: learn an estimate and the tool starts
treating its own arithmetic as evidence, then feeds that back into the next
estimate. These tests pin which side of that line each price falls on.
"""
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.db import db
from app.main import app
from app.pricing.enrich import RUN_ID_PROP, enrich_workbook, stamped_run_id

from tests.test_price_enrichment import (  # reuse the real-shaped builders
    MH_TAB, _mh_list, seed_price_list, seed_usage, usage_prices,
)

client = TestClient(app)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _clean_learning():
    for row in list(db.learning_prices()):
        db.backend.delete_where("learning_price", "part_no", row["part_no"])
    yield


def _priced_workbook(run_id="testrun0001", **row):
    """Run step 5 over a one-row workbook and return (bytes, run dict)."""
    seed_price_list(_mh_list())
    data = seed_usage([{"filename": "MH1.jpg", "entity": "Maxx Health",
                        "hospital": "Blake Hospital", "ref": "MTUUX100-GK", **row}])
    out, summary, cells = enrich_workbook(data, run_id)
    run = {"run_id": run_id, "cells": cells, "status": "succeeded"}
    db.create_pricing_run({**run, "source_filename": "u.xlsx"})
    return out, run, summary


def _send_back(data):
    r = client.post("/corrections/upload",
                    files={"files": ("reviewed.xlsx", data, XLSX)})
    assert r.status_code == 200
    return r.json()["prices"]


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------
def test_a_human_price_is_never_downgraded_by_a_machine_one():
    assert db.learn_price("REF1", "Hosp", 100, source="correction") is True
    assert db.learn_price("REF1", "Hosp", 999, source="price_list") is False
    assert db.price_suggestion("REF1", "Hosp") == 100


def test_a_correction_always_overwrites_a_price_list_value():
    db.learn_price("REF2", "Hosp", 250, source="price_list")
    assert db.learn_price("REF2", "Hosp", 275, source="correction") is True
    assert db.price_suggestion("REF2", "Hosp") == 275


def test_a_new_price_list_clears_only_what_it_taught():
    """learning_price is never purged and the price list is full-replaced, so a
    learned price-list value would otherwise outlive the update meant to
    supersede it."""
    db.learn_price("REF3", "Hosp", 50, source="price_list")
    db.learn_price("REF4", "Hosp", 60, source="correction")
    assert db.clear_price_list_learning() == 1
    assert db.price_suggestion("REF3", "Hosp") is None
    assert db.price_suggestion("REF4", "Hosp") == 60


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------
def test_the_run_id_is_stamped_outside_the_cell_grid():
    """It rides in the workbook's custom properties, so it disturbs no cell and
    the 'only blank Price cells changed' validator never sees it."""
    out, run, _ = _priced_workbook()
    wb = load_workbook(io.BytesIO(out))
    assert stamped_run_id(wb) == run["run_id"]
    assert RUN_ID_PROP in {p.name for p in wb.custom_doc_props.props}


def test_an_untouched_estimate_is_not_learned():
    """The whole point. An estimate nobody corrected is still a guess, and
    learning it would let the tool cite itself."""
    seed_price_list({MH_TAB: {
        "meta": ["Item", "Description"],
        "hospitals": ["Blake Hospital (HCA)", "Other Hospital"],
        "rows": [("MTUUX***-GK", ["TIBIAL BASE PLATE (TITAN)"],
                  {"Other Hospital": 925})]}})
    data = seed_usage([
        {"filename": "MH1.jpg", "entity": "Maxx Health",
         "hospital": "Nowhere Hospital", "ref": "ZZ-REF", "price": 640},
        {"filename": "MH2.jpg", "entity": "Maxx Health",
         "hospital": "Nowhere Hospital", "ref": "ZZ-REF"},
    ])
    out, summary, cells = enrich_workbook(data, "estrun")
    db.create_pricing_run({"run_id": "estrun", "cells": cells, "status": "succeeded"})
    assert summary["estimates"] == 1

    counts = _send_back(out)
    assert counts["estimates_skipped"] == 1 and counts["corrected"] == 0
    # The pair IS in the store — the other row of this file carries a real
    # price that a human entered, and the ordinary step-4 harvest learns that
    # one. What must not happen is the *estimate* being recorded as a
    # price-list fact, which is what the source column pins down.
    assert not [r for r in db.learning_prices() if r.get("source") == "price_list"]


def test_an_untouched_price_list_value_is_learned():
    out, _, summary = _priced_workbook()
    assert summary["direct"] == 1
    counts = _send_back(out)
    assert counts["confirmed"] == 1
    assert db.price_suggestion("MTUUX100-GK", "Blake Hospital") == 925
    row = next(r for r in db.learning_prices() if r["part_no"] == "MTUUX100-GK")
    assert row["source"] == "price_list"


def test_a_corrected_price_is_learned_as_a_human_decision():
    out, run, _ = _priced_workbook()
    wb = load_workbook(io.BytesIO(out))
    ws = wb["Usage"]
    col = [c.value for c in ws[1]].index("Price") + 1
    ws.cell(row=run["cells"][0]["row"], column=col).value = 1234
    buf = io.BytesIO()
    wb.save(buf)

    counts = _send_back(buf.getvalue())
    assert counts["corrected"] == 1 and counts["confirmed"] == 0
    assert db.price_suggestion("MTUUX100-GK", "Blake Hospital") == 1234
    row = next(r for r in db.learning_prices() if r["part_no"] == "MTUUX100-GK")
    assert row["source"] == "correction"


def test_blanking_a_price_out_again_teaches_nothing():
    """Deleting the value is a reviewer rejecting it, not asserting a new one."""
    out, run, _ = _priced_workbook()
    wb = load_workbook(io.BytesIO(out))
    ws = wb["Usage"]
    col = [c.value for c in ws[1]].index("Price") + 1
    ws.cell(row=run["cells"][0]["row"], column=col).value = None
    buf = io.BytesIO()
    wb.save(buf)

    counts = _send_back(buf.getvalue())
    assert counts == {"corrected": 0, "confirmed": 0, "estimates_skipped": 0,
                      "unchanged_kept": 0}
    assert db.price_suggestion("MTUUX100-GK", "Blake Hospital") is None


def test_an_ordinary_corrections_file_harvests_no_prices():
    """A workbook that never went through step 5 carries no run id, so the
    pricing harvest must stay out of the way entirely."""
    seed_price_list(_mh_list())
    data = seed_usage([{"filename": "MH1.jpg", "entity": "Maxx Health",
                        "hospital": "Blake Hospital", "ref": "MTUUX100-GK"}])
    assert stamped_run_id(load_workbook(io.BytesIO(data))) is None
    counts = _send_back(data)
    assert counts == {"corrected": 0, "confirmed": 0, "estimates_skipped": 0,
                      "unchanged_kept": 0}


def test_the_learned_price_is_what_extraction_will_offer():
    """The payoff, asserted through the same call assemble.py makes."""
    out, _, _ = _priced_workbook()
    _send_back(out)
    assert db.price_suggestion("MTUUX100-GK", "Blake Hospital") == 925
