"""The app reports what it weighs.

Written after two wrong diagnoses in two days. On 2 Nov the kernel killed
uvicorn five times at ~5.3 GB and the logs said nothing — an OOM kill leaves no
traceback, the process just stops. Both fixes shipped on inference from reading
code, and both times the failure came back.

The gap was never the fixing. It was that nobody could watch memory move.
These tests pin the two properties that make the instrument trustworthy: it is
always on, and it can never be the thing that breaks a run.
"""
import logging

from app import memlog


def test_it_reports_a_real_number():
    assert memlog.rss_mb() > 0
    assert memlog.peak_mb() >= memlog.rss_mb() * 0.5


def test_growth_shows_up_as_a_delta():
    memlog.reset()
    memlog.mark("before")
    block = bytearray(120 * 1024 * 1024)
    try:
        assert memlog.mark("after") - 100 > 0
    finally:
        del block


def test_a_mark_names_its_stage_and_the_change(caplog):
    """The last line before a restart is what names the stage that was running
    when the kernel fired, so the stage and the delta both have to be in it."""
    memlog.reset()
    with caplog.at_level(logging.INFO, logger="usage.mem"):
        memlog.mark("first")
        memlog.mark("second", ticket="abc123")
    text = caplog.text
    assert "stage=first" in text
    assert "stage=second" in text and "ticket=abc123" in text
    assert "delta=" in text and "peak=" in text


def test_the_first_mark_of_a_thread_has_no_delta(caplog):
    """A delta against another thread's last value would attribute one
    ticket's growth to a different one — the batch runs three at a time."""
    memlog.reset()
    with caplog.at_level(logging.INFO, logger="usage.mem"):
        memlog.mark("fresh")
    assert "delta=" not in caplog.text


def test_each_thread_keeps_its_own_previous_value():
    import threading

    memlog.mark("main-thread-mark")           # sets a value on THIS thread
    seen = []

    def worker():
        memlog.reset()
        seen.append(getattr(memlog._last, "value", None))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen == [None], "a worker must not inherit another thread's mark"


def test_it_never_raises(monkeypatch):
    """An instrument that can crash a run is worse than no instrument. If
    /proc is unreadable it reports 0 and the pipeline carries on."""
    def boom(*a, **k):
        raise OSError("no /proc here")

    monkeypatch.setattr("builtins.open", boom)
    assert memlog.rss_mb() == 0.0


def test_the_heavy_stages_are_instrumented():
    """Named explicitly: the point is that the LAST line before a kill
    identifies the stage, which only works if every expensive one marks."""
    import inspect

    from app.pipeline import pdf, run

    src = inspect.getsource(run)
    for stage in ("ingest.decoded", "process.image_loaded",
                  "process.barcodes_done", "process.vision_done",
                  "batch.tickets_done", "batch.workbook_built"):
        assert stage in src, f"{stage} is not marked"
    assert "pdf.page_rendered" in inspect.getsource(pdf)


def test_diag_reports_memory_against_the_container_limit():
    from fastapi.testclient import TestClient

    from app.main import app

    mem = TestClient(app).get("/diag").json()["memory"]
    assert mem["rss_mb"] > 0
    assert mem["limit_mb"] == 3072, "must match docker-compose mem_limit"


# ---------------------------------------------------------------------------
# The trace says how long, not just what
# ---------------------------------------------------------------------------
def test_each_trace_step_records_how_long_it_took():
    """A debug trace that took two minutes could say what it did and not one
    word about where the time went, so "is that normal?" had to come to me."""
    import time

    from app.pipeline import tracer

    steps = tracer.start()
    try:
        time.sleep(0.02)
        tracer.record("a", "First", "ok", "did a thing")
        tracer.record("b", "Second", "ok", "did another")
    finally:
        tracer.stop()
    assert steps[0]["ms"] >= 15
    assert all(s["ms"] is not None for s in steps)


def test_a_step_recorded_outside_a_trace_is_still_a_no_op():
    from app.pipeline import tracer

    tracer.stop()
    tracer.record("x", "x", "ok", "x")          # must not raise
