#!/usr/bin/env python3
"""Pull new Claude Code / Cursor usage data into the local SQLite DB.

    python ingest.py               # incremental (default)
    python ingest.py --full        # rebuild everything from scratch
    python ingest.py --claude      # only Claude Code
    python ingest.py --cursor      # only Cursor
"""
import argparse
import sys
import time

from aidash import claude_ingest, config, cursor_ingest, db


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="ignore saved offsets and re-read everything")
    ap.add_argument("--claude", action="store_true", help="only ingest Claude Code")
    ap.add_argument("--cursor", action="store_true", help="only ingest Cursor")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    do_claude = args.claude or not args.cursor
    do_cursor = args.cursor or not args.claude
    verbose = not args.quiet

    con = db.connect()
    db.init(con)

    if args.full:
        if do_claude:
            db.reset(con, "cc_")
        if do_cursor:
            db.reset(con, "cur_")
        if verbose:
            print("full rebuild: cleared existing rows")

    if do_claude:
        t0 = time.time()
        print(f"Claude Code  <- {config.claude_projects_dir()}")
        res = claude_ingest.run(con, full=args.full, verbose=verbose)
        if res.get("ok"):
            print(f"  {res['files_read']}/{res['files_seen']} transcripts read, "
                  f"{res['rows']} rows in {time.time() - t0:.1f}s")
            guessed = {m: c for m, c in res.get("pricing_confidence", {}).items() if c != "official"}
            if guessed:
                print(f"  ! cost is estimated for: {', '.join(sorted(guessed))}"
                      f"  (edit aidash/pricing.json)")
        else:
            print(f"  skipped: {res.get('error')}")

    if do_cursor:
        t0 = time.time()
        gs = config.cursor_global_storage()
        print(f"Cursor       <- {gs or 'not found'}")
        res = cursor_ingest.run(con, full=args.full, verbose=verbose)
        if res.get("ok"):
            print(f"  {res['sessions']} sessions, {res['bubbles']} new messages, "
                  f"{res['ai_lines']} ai-lines, {res['conversations']} conversations "
                  f"in {time.time() - t0:.1f}s")
        else:
            print(f"  skipped: {res.get('error')}")

    size = config.DB_PATH.stat().st_size / 1e6 if config.DB_PATH.exists() else 0
    print(f"\nDB: {config.DB_PATH}  ({size:.1f} MB)")
    print("Next: python serve.py")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
