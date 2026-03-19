"""
extract_all_chunks.py — Extract chunks from all stories, rank families, seed chunk_queue.

Processes all stories in story_library, extracts multiword chunks via GPT-4o,
groups into families, ranks by composite score, and seeds the top N into chunk_queue
with GPT-generated Baiano carrier sentences.

Usage:
    python3 extract_all_chunks.py                    # extract + rank + seed top 50
    python3 extract_all_chunks.py --seed-count 100   # seed top 100 into queue
    python3 extract_all_chunks.py --extract-only      # extract only, no seeding
    python3 extract_all_chunks.py --status            # show chunk stats
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from chunk_engine import (
    extract_chunks_from_story,
    rank_chunk_families,
    get_next_chunks_for_srs,
    add_chunks_to_queue,
)
from srs_engine import DB_PATH, get_connection


def show_status(db_path=DB_PATH):
    conn = get_connection(db_path)
    total_stories = conn.execute("SELECT COUNT(*) FROM story_library").fetchone()[0]
    total_families = conn.execute("SELECT COUNT(*) FROM chunk_families").fetchone()[0]
    total_variants = conn.execute("SELECT COUNT(*) FROM chunk_variants").fetchone()[0]
    total_queued = conn.execute("SELECT COUNT(*) FROM chunk_queue").fetchone()[0]

    # Stories with chunks extracted (have variants sourced from stories)
    stories_with_chunks = conn.execute(
        "SELECT COUNT(DISTINCT source_id) FROM chunk_variants WHERE source = 'story'"
    ).fetchone()[0]

    # Top families by rank
    top = conn.execute(
        """SELECT root_form, composite_rank, bahia_relevance, word_count
           FROM chunk_families ORDER BY composite_rank DESC LIMIT 10"""
    ).fetchall()

    # Bahia-specific count
    baiano_count = conn.execute(
        "SELECT COUNT(*) FROM chunk_families WHERE bahia_relevance >= 50"
    ).fetchone()[0]

    conn.close()

    print("Oxe Protocol — Chunk Engine Status")
    print("=" * 50)
    print(f"Stories processed:      {stories_with_chunks:>6} / {total_stories}")
    print(f"Chunk families:         {total_families:>6}")
    print(f"Chunk variants:         {total_variants:>6}")
    print(f"Baiano-specific (>=50): {baiano_count:>6}")
    print(f"Chunks in SRS queue:    {total_queued:>6}")

    if top:
        print("\nTop 10 chunks by composite rank:")
        for r in top:
            print(f"  {r['composite_rank']:.3f}  [{r['word_count']}w] "
                  f"bahia={r['bahia_relevance']:.0f}  {r['root_form']}")


def extract_all_stories(db_path=DB_PATH):
    conn = get_connection(db_path)
    stories = conn.execute("SELECT id, title, level FROM story_library ORDER BY id").fetchall()
    conn.close()

    if not stories:
        print("No stories found.")
        return

    print(f"Extracting chunks from {len(stories)} stories...")
    print("=" * 60)

    total_chunks = 0
    start = time.time()

    # Process stories in parallel batches of 3 (GPT rate limits)
    batch_size = 3
    for batch_start in range(0, len(stories), batch_size):
        batch = stories[batch_start:batch_start + batch_size]

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = {
                executor.submit(extract_chunks_from_story, s["id"], db_path): s
                for s in batch
            }
            for future in as_completed(futures):
                story = futures[future]
                try:
                    count = future.result()
                    total_chunks += count
                    i = batch_start + list(futures.values()).index(story) + 1
                    elapsed = time.time() - start
                    rate = i / elapsed * 60 if elapsed > 0 else 0
                    print(f"  [{i}/{len(stories)}] '{story['title'][:40]}' "
                          f"({story['level']}) → {count} chunks "
                          f"| {rate:.1f} stories/min", flush=True)
                except Exception as e:
                    print(f"  [ERROR] Story {story['id']}: {e}", flush=True)

        time.sleep(0.5)  # Brief pause between batches

    elapsed = time.time() - start
    print("=" * 60)
    print(f"Done. {total_chunks} total chunk variants extracted in {elapsed:.0f}s")
    return total_chunks


def main():
    parser = argparse.ArgumentParser(
        description="Extract chunks from all stories, rank, and seed SRS queue."
    )
    parser.add_argument("--seed-count", type=int, default=50,
                        help="Number of top chunks to seed into SRS queue (default: 50)")
    parser.add_argument("--extract-only", action="store_true",
                        help="Extract and rank only, don't seed queue")
    parser.add_argument("--status", action="store_true",
                        help="Show chunk stats and exit")
    parser.add_argument("--seed-only", action="store_true",
                        help="Skip extraction, just rank and seed queue")

    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if not args.seed_only:
        # Step 1: Extract chunks from all stories
        extract_all_stories()

    # Step 2: Rank all chunk families
    print("\nRanking chunk families...")
    rank_chunk_families()
    print("Done.")

    if args.extract_only:
        show_status()
        return

    # Step 3: Seed top chunks into SRS queue
    print(f"\nSeeding top {args.seed_count} chunks into SRS queue...")
    top_chunks = get_next_chunks_for_srs(limit=args.seed_count)
    if not top_chunks:
        print("No new chunks to seed (all already in queue or no families found).")
        show_status()
        return

    print(f"Generating Baiano carrier sentences for {len(top_chunks)} chunks...")
    added = add_chunks_to_queue(top_chunks)
    print(f"Seeded {added} chunks into SRS queue.")

    show_status()


if __name__ == "__main__":
    main()
