#!/usr/bin/env python3
"""Delete stored redacted ticket images.

Why this exists: images already in the bucket were masked by an older, smaller
patient region that did not always cover the whole sticker, so some of them
carry visible patient details. Re-masking cannot help — the stored copy IS the
masked one, so the original framing is gone and a second pass could only paint a
bigger black box over a guess. Deleting is the only thing that actually removes
them.

The cost is bounded: the images exist so a reviewer can re-check a ticket, and
retention drops them after RETENTION_DAYS anyway. Extraction results, line items
and every learned fact live in the database and are untouched.

    python scripts/purge_redacted_images.py            # show what would go
    python scripts/purge_redacted_images.py --delete   # actually delete
    python scripts/purge_redacted_images.py --delete --before 2026-09-29

Run it on the VPS, where the Supabase credentials are:

    docker compose exec labels-api python scripts/purge_redacted_images.py
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delete", action="store_true",
                    help="actually delete; without it nothing is removed")
    ap.add_argument("--before", metavar="YYYY-MM-DD",
                    help="only tickets created before this date "
                         "(default: every stored image)")
    args = ap.parse_args()

    from app.db import db
    from app.storage import delete_object, split_ref

    tickets = [t for t in db.backend.select("tickets") if t.get("source_image_path")]
    if args.before:
        tickets = [t for t in tickets
                   if str(t.get("created_at") or "")[:10] < args.before]

    if not tickets:
        print("No stored images match.")
        return 0

    print(f"{len(tickets)} stored image(s) match"
          + (f" (created before {args.before})" if args.before else ""))
    if not args.delete:
        for t in tickets[:10]:
            print(f"  {t['ticket_id']}  {t.get('created_at', '')[:19]}  "
                  f"{t['source_image_path']}")
        if len(tickets) > 10:
            print(f"  ... and {len(tickets) - 10} more")
        print("\nNothing deleted. Re-run with --delete to remove them.")
        return 0

    removed = failed = 0
    for t in tickets:
        try:
            bucket, path = split_ref(t["source_image_path"])
            delete_object(bucket, path)
            # Clear the pointer too, so the UI doesn't offer a dead link.
            db.update_ticket(t["ticket_id"], {"source_image_path": None})
            removed += 1
        except Exception as exc:
            failed += 1
            print(f"  could not delete {t['ticket_id']}: {exc}", file=sys.stderr)

    print(f"Deleted {removed} image(s)" + (f", {failed} failed" if failed else ""))
    print("Extraction results, line items and learned facts are untouched.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
