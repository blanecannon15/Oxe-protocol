#!/usr/bin/env python3
"""Backfill support chunks for all SRS words that only have 1 chunk.

Calls auto_link_word_to_chunks() for each word, which:
  - Generates 3 additional chunks via GPT-4o-mini
  - Sets item_role = 'primary' for first, 'support' for rest
  - Creates support_links entries

Usage:
    source ~/.profile && python3 backfill_support_chunks.py
    source ~/.profile && python3 backfill_support_chunks.py --limit 50   # do 50 at a time
    source ~/.profile && python3 backfill_support_chunks.py --dry-run    # just count
"""

import argparse
import sqlite3
import sys
import time

from srs_engine import DB_PATH, get_connection
from chunk_engine import auto_link_word_to_chunks


def get_words_needing_backfill(db_path=DB_PATH):
    """Find words with only 1 chunk in chunk_queue (no supports)."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT cq.word_id, wb.word, COUNT(*) as cnt
        FROM chunk_queue cq
        JOIN word_bank wb ON wb.id = cq.word_id
        WHERE cq.word_id IS NOT NULL
        GROUP BY cq.word_id
        HAVING cnt = 1
        ORDER BY cq.id
    """).fetchall()
    conn.close()
    return [(r["word_id"], r["word"]) for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Max words to backfill (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Just count, don't generate")
    args = parser.parse_args()

    words = get_words_needing_backfill()
    total = len(words)
    print(f"Found {total} words needing support chunk backfill")

    if args.dry_run:
        for wid, w in words[:20]:
            print(f"  {wid}: {w}")
        if total > 20:
            print(f"  ... and {total - 20} more")
        return

    if args.limit > 0:
        words = words[:args.limit]
        print(f"Processing {len(words)} of {total}")

    success = 0
    errors = 0
    for i, (wid, w) in enumerate(words):
        try:
            result = auto_link_word_to_chunks(wid, w, count=4)
            sup_count = len(result.get("support_chunk_ids", []))
            print(f"[{i+1}/{len(words)}] {w} (id={wid}): primary={result.get('primary_chunk_id')}, supports={sup_count}")
            success += 1
            # Small delay to avoid rate limiting
            time.sleep(0.5)
        except Exception as e:
            print(f"[{i+1}/{len(words)}] ERROR {w} (id={wid}): {e}")
            errors += 1

    print(f"\nDone: {success} success, {errors} errors out of {len(words)} words")

    # Verify
    conn = get_connection()
    links = conn.execute("SELECT COUNT(*) as c FROM support_links").fetchone()["c"]
    supports = conn.execute("SELECT COUNT(*) as c FROM chunk_queue WHERE item_role='support'").fetchone()["c"]
    conn.close()
    print(f"Total support_links: {links}, support-role chunks: {supports}")


if __name__ == "__main__":
    main()
