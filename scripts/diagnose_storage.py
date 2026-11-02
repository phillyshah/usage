"""Why does a Supabase Storage upload hang? Three questions, one run.

Written after four wrong theories about one failure — HTTP/2 flow control,
a stale keep-alive pool, path MTU, and payload size — each of which sounded
right and cost a round trip to disprove. The questions below are the ones that
actually partition the problem, so one run narrows it instead of one guess.

    docker exec usage-labels-api-1 python scripts/diagnose_storage.py

It writes small objects named probe-* into the ticket-image bucket and reads
nothing back. Safe to run on production; delete the probe-* objects afterwards
if you care about tidiness.
"""
import sys
import time

sys.path.insert(0, "/app")

import httpx

from app.config import settings

BUCKET = "redacted-images"
BASE = settings.supabase_url.rstrip("/")
AUTH = {"Authorization": f"Bearer {settings.supabase_service_key}"}


def _timed(fn):
    started = time.time()
    try:
        return fn(), None, time.time() - started
    except Exception as e:
        return None, f"{type(e).__name__}", time.time() - started


def q1_does_storage_answer_at_all():
    """A SMALL request to the same service.

    If this hangs too, Storage itself is unreachable or wedged and the size of
    an upload is beside the point. If it answers instantly, the service is up
    and something about sending a BODY is the problem.
    """
    print("\n--- 1. Does Storage answer a small request? ---")
    with httpx.Client(timeout=15) as c:
        r, err, secs = _timed(lambda: c.get(f"{BASE}/storage/v1/bucket", headers=AUTH))
        if err:
            print(f"    list buckets: FAIL {err} in {secs:.1f}s")
            return False
        print(f"    list buckets: HTTP {r.status_code} in {secs:.1f}s")
        if r.status_code < 300:
            names = [b.get("name") for b in r.json()] if r.text.startswith("[") else []
            print(f"    buckets: {names}")
            print(f"    '{BUCKET}' present: {BUCKET in names}")
        else:
            print(f"    body: {r.text[:200]}")
        return r.status_code < 300


def q2_where_is_the_size_cliff():
    """Upload 1 KB, 16 KB, 64 KB, 128 KB, 293 KB and find where it stops.

    A clean cliff names the mechanism. Right at 64 KB would have meant HTTP/2
    flow control (it did not — HTTP/1.1 failed identically). A cliff somewhere
    else, or no cliff at all, means something different again, and the number
    is the clue either way.
    """
    print("\n--- 2. Where is the size cliff? ---")
    sizes = [1_000, 16_000, 64_000, 128_000, 293_000]
    last_ok = None
    for n in sizes:
        url = f"{BASE}/storage/v1/object/{BUCKET}/probe-{n}.jpg"
        hdr = {**AUTH, "Content-Type": "image/jpeg", "x-upsert": "true",
               # curl adds Expect: 100-continue for big bodies and so gets a
               # fast rejection without ever sending one. Make sure we DO send.
               "Expect": ""}
        with httpx.Client(http2=False, timeout=20) as c:
            r, err, secs = _timed(lambda: c.post(url, headers=hdr, content=b"x" * n))
        if err:
            print(f"    {n:>7,} bytes: FAIL {err} in {secs:.1f}s")
        else:
            print(f"    {n:>7,} bytes: HTTP {r.status_code} in {secs:.1f}s")
            if r.status_code < 300:
                last_ok = n
    print(f"    largest that succeeded: {last_ok if last_ok else 'none'}")
    return last_ok


def q3_is_it_this_box_or_supabase():
    """The same sized POST to somewhere that is not Supabase.

    If a 293 KB POST stalls to every host, it is this machine's egress and no
    change to this app can fix it. If it only stalls to Supabase, it is theirs
    or ours.
    """
    print("\n--- 3. Is it this box, or Supabase? ---")
    for host in ("https://postman-echo.com/post", "https://httpbin.org/post"):
        with httpx.Client(http2=False, timeout=20, follow_redirects=True) as c:
            r, err, secs = _timed(
                lambda: c.post(host, content=b"x" * 293_000,
                               headers={"Content-Type": "application/octet-stream",
                                        "Expect": ""}))
        if err:
            print(f"    {host}: FAIL {err} in {secs:.1f}s")
        else:
            print(f"    {host}: HTTP {r.status_code} in {secs:.1f}s")


def main():
    print(f"Supabase: {BASE}")
    print(f"key: ...{settings.supabase_service_key[-6:]} "
          f"({len(settings.supabase_service_key)} chars)")
    storage_up = q1_does_storage_answer_at_all()
    cliff = q2_where_is_the_size_cliff()
    q3_is_it_this_box_or_supabase()

    print("\n--- What this means ---")
    if not storage_up:
        print("    Storage does not answer even a small request. The uploads are")
        print("    a symptom; the service or the credential is the problem.")
    elif cliff is None:
        print("    Storage answers reads but accepts no upload at any size,")
        print("    including 1 KB. That is not about size — look at the bucket,")
        print("    the key's permissions, and question 3's result.")
    elif cliff >= 293_000:
        print("    Every size went through. The failure is intermittent rather")
        print("    than absolute — re-run during a failing period.")
    else:
        print(f"    Uploads succeed to {cliff:,} bytes and stop above it.")
        print("    Compare with question 3: if other hosts take 293 KB fine,")
        print("    the limit is on the Supabase side of the path.")


if __name__ == "__main__":
    main()
