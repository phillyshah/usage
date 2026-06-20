# Test fixtures

Per `DEVELOPER_HANDOFF.md` §10, drop the real sample data here:

- `ticket_01.jpg` … `ticket_04.jpg` — the 4 sample ticket photos
- `Expiry_Log.xlsx` — the reference log (loads to ~1,682 parts / ~51k lots)

Expected behaviour once the real fixtures are present:

- The Expiry Log loads to ~1,682 parts / ~51,000 lots.
- Blank slots ("Place Implant Label") produce no line.
- Some REFs resolve from the log and some don't — the misses land amber/flagged,
  never silent.
- Patient stickers are masked in every stored image.

Until the real photos are added, `_synthetic.py` generates a ticket image with a
real GS1 DataMatrix barcode and a small Expiry Log so `test_end_to_end.py`
exercises the whole path offline (no Supabase, no Anthropic, no network).
