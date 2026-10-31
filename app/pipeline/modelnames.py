"""Turning "sonnet" into the model id that is current today.

Anthropic publishes no `-latest` alias. `claude-sonnet-5-5` looks like a
floating name but it is a pin: when the next Sonnet ships, a config that says
`claude-sonnet-5-5` keeps calling the old one until somebody edits it, and
nothing in the app would say so. That is the thing this module removes.

A setting may therefore name a FAMILY — `sonnet`, `opus`, `haiku` — and the
newest released model in it is looked up from the Models API. Anything else is
passed through untouched, so an exact id is still a deliberate pin and the
OpenRouter ids (`vendor/model`) are unaffected.

Three rules this follows, each from something that has already gone wrong here:

  * **It never fails a batch.** The API is reachable or it is not; on any
    problem the pinned fallback below is used and the reason is logged. A
    reader that could not start because a *name lookup* failed would be a new
    way to lose a day's work.
  * **It requires image support.** The newest model in a family is not
    necessarily one that can look at a photo. Picking a text-only model would
    fail every ticket in the batch, so `image_input` is checked rather than
    assumed.
  * **The answer is cached, with a TTL.** One lookup per family per day, not
    one per ticket; and a container that stays up for a month still notices a
    new model.
"""
import logging
import time

from app.config import settings

log = logging.getLogger("usage.models")

# The newest model in each family as of this writing. Used when the Models API
# cannot be reached, and as the floor the lookup has to beat — a lookup that
# came back with something OLDER than this (a filtered account, a partial
# response) is not an answer worth taking.
PINNED = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

FAMILIES = tuple(PINNED)

_TTL_SECONDS = 24 * 60 * 60
_cache: dict[str, tuple[str, float]] = {}


def is_family(spec: str | None) -> bool:
    return (spec or "").strip().lower() in FAMILIES


def resolve(spec: str | None) -> str:
    """The concrete model id for a setting that may name a family.

    `resolve("sonnet")` -> the current Sonnet. `resolve("claude-opus-5-5")`,
    `resolve("qwen/qwen3-vl-235b-a22b-instruct")` -> unchanged.
    """
    name = (spec or "").strip()
    if not is_family(name):
        return name
    family = name.lower()

    hit = _cache.get(family)
    if hit and (time.monotonic() - hit[1]) < _TTL_SECONDS:
        return hit[0]

    model = _newest(family) or PINNED[family]
    _cache[family] = (model, time.monotonic())
    return model


def _newest(family: str) -> str | None:
    """Ask the Models API which model in this family is current."""
    key = (settings.anthropic_api_key or "").strip()
    if not key:
        # Not an error worth logging loudly: escalation_configuration() already
        # reports a missing key, and the family name still resolves to the pin.
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key, timeout=20.0)
        prefix = f"claude-{family}-"
        candidates = []
        for m in client.models.list():
            if not (m.id or "").startswith(prefix):
                continue
            if not _reads_images(m):
                continue
            candidates.append((str(getattr(m, "created_at", "") or ""), m.id))
        if not candidates:
            log.warning("no vision-capable %s model found; staying on %s",
                        family, PINNED[family])
            return None
        newest = max(candidates)[1]
        if newest != PINNED[family]:
            log.info("%s resolves to %s (pinned fallback is %s)",
                     family, newest, PINNED[family])
        return newest
    except Exception as e:
        # Deliberately not re-raised. See the module docstring: a name lookup
        # is not allowed to be the thing that stops a batch.
        log.warning("could not ask the Models API for the current %s (%s: %s); "
                    "using %s", family, type(e).__name__, e, PINNED[family])
        return None


def _reads_images(model) -> bool:
    """Whether this model accepts an image, per its own capability tree.

    Absent capabilities are treated as "yes": an older API version that does
    not report the tree should not make every model look unusable. A reported
    `supported: false` is taken at its word.
    """
    caps = getattr(model, "capabilities", None)
    if not isinstance(caps, dict):
        return True
    try:
        return bool(caps["image_input"]["supported"])
    except (KeyError, TypeError):
        return True


def forget() -> None:
    """Drop the cache. For tests, and for a reload after a config change."""
    _cache.clear()
