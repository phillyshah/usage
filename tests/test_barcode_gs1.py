"""GS1 DataMatrix parsing: separator-free Maxx grammar, (240) REF, check digit."""
from app.pipeline.barcode import decode_single, gtin_check_digit_ok
from tests.fixtures._worked_example import EXPECTED_LINES, LABEL_PAYLOADS


def test_check_digit_validation():
    assert gtin_check_digit_ok("00810008120088") is True
    assert gtin_check_digit_ok("00810008120089") is False   # last digit wrong
    assert gtin_check_digit_ok("123") is False
    assert gtin_check_digit_ok(None) is False


def test_maxx_payloads_parse_fully():
    by_ref = {e["ref"]: e for e in EXPECTED_LINES}
    for payload in LABEL_PAYLOADS:
        f = decode_single(payload)
        assert f["ref"] in by_ref, f["ref"]
        exp = by_ref[f["ref"]]
        assert f["lot"] == exp["lot"]
        assert f["expiry"] == exp["expiry"]
        assert gtin_check_digit_ok(f["gtin"])
        assert f["mfg"] is not None                # (11) captured


def test_all_digit_lot_is_not_swallowed():
    """A numeric lot (no letters) must still delimit against the date AIs."""
    f = decode_single("01008100081211081070119757471125060117300531240MO-HDAI-36/40-")
    assert f["lot"] == "7011975747"
    assert f["expiry"] == "2030-05-31"
    assert f["ref"] == "MO-HDAI-36/40-"


def test_separated_gs1_still_parses_via_biip():
    """A properly FNC1-separated payload (different AI order) parses through biip."""
    f = decode_single("01036141400012331727113010LOT12345")
    assert f["lot"] == "LOT12345"
    assert f["expiry"] == "2027-11-30"
