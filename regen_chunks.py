"""
regen_chunks.py — Regenerate all cached chunks with high-frequency prompt.

Deletes existing chunks cache entries and regenerates them via GPT-4o
using the updated prompt that enforces high-frequency collocations only.

Usage:
    source ~/.profile && python3 regen_chunks.py
    source ~/.profile && python3 regen_chunks.py --batch 100  # process 100 at a time
    source ~/.profile && python3 regen_chunks.py --status      # check progress
"""

import argparse
import json
import sqlite3
import sys
import time

from dictionary_engine import get_word_chunks, DB_PATH, get_connection


def get_status():
    conn = get_connection(DB_PATH)
    total = conn.execute(
        "SELECT COUNT(DISTINCT word_id) FROM dictionary_cache WHERE tab_name = 'chunks'"
    ).fetchone()[0]
    # Check how many have been regenerated (flagged via a marker)
    regen = conn.execute(
        "SELECT COUNT(*) FROM dictionary_cache WHERE tab_name = 'chunks' AND data_json LIKE '%alta%'"
    ).fetchone()[0]
    conn.close()
    return total, regen


def regenerate(batch_size=0, delay=0.5):
    conn = get_connection(DB_PATH)

    # Get all word_ids with cached chunks
    rows = conn.execute(
        """SELECT dc.word_id, wb.word
           FROM dictionary_cache dc
           JOIN word_bank wb ON wb.id = dc.word_id
           WHERE dc.tab_name = 'chunks'
           ORDER BY wb.frequency_rank ASC"""
    ).fetchall()
    conn.close()

    total = len(rows)
    if batch_size > 0:
        rows = rows[:batch_size]

    print(f"Regenerating chunks for {len(rows)}/{total} words...")

    success = 0
    errors = 0

    for i, row in enumerate(rows):
        word_id = row[0]
        word = row[1]

        try:
            # Generate new chunks with updated high-frequency prompt
            new_data = get_word_chunks(word)

            # Update the cache entry
            conn2 = get_connection(DB_PATH)
            conn2.execute(
                "UPDATE dictionary_cache SET data_json = ?, created_at = datetime('now') "
                "WHERE word_id = ? AND tab_name = 'chunks'",
                (json.dumps(new_data, ensure_ascii=False), word_id),
            )
            conn2.commit()
            conn2.close()

            success += 1
            gen_count = len(new_data.get("chunks_generated", []))
            db_count = len(new_data.get("chunks_from_db", []))

            if (i + 1) % 10 == 0 or i == 0:
                print(f"  [{i+1}/{len(rows)}] {word}: {gen_count} generated, {db_count} from DB")

        except Exception as e:
            errors += 1
            print(f"  [{i+1}/{len(rows)}] {word}: ERROR - {e}")

        if delay > 0:
            time.sleep(delay)

    print(f"\nDone: {success} regenerated, {errors} errors")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--batch", type=int, default=0, help="Process N words (0=all)")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between GPT calls")
    args = parser.parse_args()

    if args.status:
        total, regen = get_status()
        print(f"Total chunks cached: {total}")
        print(f"With 'alta' frequency: {regen}")
    else:
        regenerate(batch_size=args.batch, delay=args.delay)
