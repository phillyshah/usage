"""app/pricing/suggest.py — what price to offer for a line we couldn't read.

Pure and db-free, so these are cheap and exact.

The bug worth naming: the old lookup compared hospital names with ``==``, so a
ticket reading 'Blake Hospital' missed a price learned under 'Blake Hospital
(HCA)' — while step 5, for the same concept, matched that pair through four
tiers. Two matching policies for one idea is one too many.
"""
from app.pricing.suggest import suggest_price


def _rows(*pairs):
    return [{"hospital": h, "unit_price": p} for h, p in pairs]


def test_an_exact_hospital_match_is_offered_and_may_confirm_a_read_price():
    s = suggest_price(_rows(("Mercy General", 725)), "Mercy General")
    assert s.value == 725.0 and s.source == "learned_price" and s.may_confirm


def test_a_spelling_variant_of_the_same_account_is_found():
    """This is the miss the old exact-equality lookup produced."""
    s = suggest_price(_rows(("Blake Hospital (HCA)", 900)), "Blake Hospital")
    assert s is not None and s.value == 900.0


def test_a_loose_hospital_match_may_never_confirm_a_read_price():
    """'Boca Raton Hospital' and 'Boca Raton Surg Ctr' collapse under the core
    tier and are plausibly two different accounts with two different prices, so
    agreement with one cannot make a read price confident."""
    s = suggest_price(_rows(("Boca Raton Surgical Center", 400)),
                      "Boca Raton Surgical Institute")
    if s is not None and s.source == "learned_price_fuzzy":
        assert not s.may_confirm
        assert "loosely" in s.basis


def test_another_hospitals_price_is_not_offered_as_this_hospitals():
    s = suggest_price(_rows(("Mercy General", 725)), "Somewhere Else")
    assert s is None, "one observation is not a cross-hospital typical price"


def test_three_agreeing_hospitals_become_a_cross_hospital_suggestion():
    s = suggest_price(_rows(("A", 100), ("B", 100), ("C", 110)), "Unknown General")
    assert s.value == 100.0 and s.source == "learned_price_cross"
    assert not s.may_confirm and "other hospitals" in s.basis


def test_two_observations_are_not_enough():
    assert suggest_price(_rows(("A", 100), ("B", 100)), "Unknown General") is None


def test_witnesses_that_disagree_widely_are_not_evidence():
    """A 9x spread isn't describing one component's price."""
    assert suggest_price(_rows(("A", 100), ("B", 500), ("C", 900)),
                         "Unknown General") is None


def test_no_hospital_on_the_ticket_can_still_use_the_cross_hospital_rung():
    s = suggest_price(_rows(("A", 100), ("B", 100), ("C", 110)), None)
    assert s is not None and s.source == "learned_price_cross"


def test_nothing_learned_means_nothing_offered():
    assert suggest_price([], "Mercy General") is None


def test_zero_and_junk_prices_are_ignored():
    assert suggest_price(_rows(("Mercy General", 0)), "Mercy General") is None
    assert suggest_price(_rows(("Mercy General", "n/a")), "Mercy General") is None


def test_two_different_learned_prices_for_one_hospital_offer_neither():
    """Contradictory history is not a suggestion."""
    rows = [{"hospital": "Mercy General", "unit_price": 700},
            {"hospital": "Mercy General", "unit_price": 900}]
    s = suggest_price(rows, "Mercy General")
    assert s is None or s.source == "learned_price_cross"
