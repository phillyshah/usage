"""Guards for the browser code, which the rest of the suite cannot see.

These exist because of a real shipped bug: the step-5 download button — the
entire deliverable of that feature — rendered as a styled anchor with no href,
so clicking it did nothing. Every Python test passed, because the failure was
three layers past where they stop.
"""
import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "app.js"
API_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "api.js"


def test_every_anchor_sets_its_href_through_attrs():
    """``el()`` only reads class / text / html / attrs, so an ``href`` passed at
    the top level is silently dropped and the link looks right but goes nowhere.

    That is exactly how the priced-workbook download shipped broken.
    """
    src = APP_JS.read_text()
    offenders = []
    # Each el("a", { ... }) options object, up to its closing brace.
    for m in re.finditer(r'el\(\s*"a"\s*,\s*\{(.*?)\}\s*\)', src, re.S):
        opts = m.group(1)
        line = src[:m.start()].count("\n") + 1
        if re.search(r'(^|[\s,{])href\s*:', opts) and "attrs" not in opts:
            offenders.append(f"app.js:{line} passes href outside attrs")
        elif "attrs" not in opts:
            offenders.append(f"app.js:{line} builds an <a> with no attrs/href")
    assert not offenders, "\n".join(offenders)


def test_every_api_helper_is_actually_called():
    """A helper nobody calls is a feature nobody can reach. ``pricingLatest``
    sat unused, which is why a finished workbook became unreachable the moment
    the page was reloaded."""
    api_src, app_src = API_JS.read_text(), APP_JS.read_text()
    helpers = set(re.findall(r'^\s{2}(\w+)\s*\(', api_src, re.M))
    # Control flow at the same indentation reads like a method to a regex.
    helpers -= {"request", "constructor", "if", "for", "while", "switch",
                "catch", "return", "function"}
    unused = sorted(h for h in helpers if f"api.{h}(" not in app_src)
    assert not unused, f"defined in api.js but never called: {', '.join(unused)}"


@pytest.mark.parametrize("element_id", [
    "pricing-form", "pricing-input", "pricing-submit", "pricing-result",
    "notify-form", "notify-recipients", "notify-enabled", "notify-test",
])
def test_ids_the_js_binds_to_exist_in_the_html(element_id):
    """app.js does $("#id").addEventListener(...) at module scope; a missing id
    throws on load and silently kills every handler defined after it."""
    html = (Path(__file__).resolve().parents[1]
            / "app" / "static" / "index.html").read_text()
    assert f'id="{element_id}"' in html
