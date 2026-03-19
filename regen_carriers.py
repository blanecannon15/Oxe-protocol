"""
regen_carriers.py — Regenerate carrier sentences for all chunk_queue entries
that have the fallback "Oxe, X — tá ligado?" pattern.
"""

import sys
import time

from chunk_engine import _generate_carrier_sentences
from srs_engine import get_connection


def main():
    conn = get_connection()

    # Find all chunks with fallback carrier
    rows = conn.execute(
        "SELECT id, target_chunk, carrier_sentence FROM chunk_queue WHERE carrier_sentence LIKE '%tá ligado?'"
    ).fetchall()

    if not rows:
        print("No fallback carriers found. All chunks have proper sentences.")
        return

    print(f"Regenerating carrier sentences for {len(rows)} chunks...")

    # Collect chunk strings
    chunk_strings = [r["target_chunk"] for r in rows]
    chunk_ids = {r["target_chunk"]: r["id"] for r in rows}

    # Generate in batch
    carriers = _generate_carrier_sentences(chunk_strings, batch_size=20)

    updated = 0
    for chunk_str, carrier in carriers.items():
        if "tá ligado?" not in carrier:  # Only update if we got a real sentence
            cid = chunk_ids.get(chunk_str)
            if cid:
                conn.execute(
                    "UPDATE chunk_queue SET carrier_sentence = ? WHERE id = ?",
                    (carrier, cid),
                )
                updated += 1

    conn.commit()
    conn.close()

    print(f"Updated {updated}/{len(rows)} carrier sentences.")

    # Show samples
    conn2 = get_connection()
    samples = conn2.execute(
        "SELECT target_chunk, carrier_sentence FROM chunk_queue WHERE carrier_sentence NOT LIKE '%tá ligado?' ORDER BY RANDOM() LIMIT 5"
    ).fetchall()
    conn2.close()

    if samples:
        print("\nSamples:")
        for s in samples:
            print(f"  [{s['target_chunk']}] → {s['carrier_sentence']}")


if __name__ == "__main__":
    main()
