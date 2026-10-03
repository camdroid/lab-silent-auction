"""Add starting flags from a CSV with "Lot ID" and "Comment" columns.

Skips any flag whose exact comment is already open on that lot, so it's safe to run twice.

    python -m app.seed_flags seed/initial-flags.csv
"""

import csv
import sys

from . import config
from .stores import open_store


def main(path: str) -> None:
    config.check()
    store = open_store()
    snap = store.snapshot(force=True)
    lot_ids = {l.id for l in snap["lots"]}
    existing = {(f["lot_id"], f["text"]) for f in snap["flags"] if not f["resolved_at"]}
    added = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            lot_id, text = int(row["Lot ID"]), row["Comment"].strip()
            if lot_id not in lot_ids:
                print(f"  lot {lot_id} isn't in the spreadsheet; skipped")
                skipped += 1
            elif (lot_id, text) in existing:
                skipped += 1
            else:
                store.add_flag(lot_id, text)
                added += 1
    print(f"Added {added} flags, skipped {skipped}.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
