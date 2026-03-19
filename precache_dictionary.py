"""
precache_dictionary.py — Pre-generate and cache dictionary data for the Oxe Protocol.

Processes words by frequency_rank (most common first), generating all 7 dictionary
tabs via GPT-4o and storing results in the dictionary_cache table.

Usage:
    python3 precache_dictionary.py --count 100                    # cache top 100 words
    python3 precache_dictionary.py --count 500 --start-rank 101   # cache ranks 101-600
    python3 precache_dictionary.py --count 1000 --delay 1.0       # slower, safer
    python3 precache_dictionary.py --status                        # show cache stats
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dictionary_engine import (
    get_definition,
    get_examples,
    get_pronunciation_data,
    get_expressions,
    get_conjugation,
    get_synonyms,
    get_word_chunks,
    get_all_tabs,
)
from srs_engine import DB_PATH, get_connection


# ── Tab definitions ───────────────────────────────────────────────────────────

TAB_FUNCTIONS = {
    "definition": lambda word: get_definition(word),
    "examples": lambda word: get_examples(word),
    "pronunciation": lambda word: get_pronunciation_data(word),
    "expressions": lambda word: get_expressions(word),
    "conjugation": lambda word: get_conjugation(word),
    "synonyms": lambda word: get_synonyms(word),
    "chunks": lambda word: get_word_chunks(word),
}

TAB_NAMES = list(TAB_FUNCTIONS.keys())


# ── Ensure cache table exists ─────────────────────────────────────────────────

def ensure_cache_table(db_path=DB_PATH):
    """Create dictionary_cache table if it doesn't exist."""
    conn = get_connection(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dictionary_cache (
            word_id   INTEGER NOT NULL,
            tab_name  TEXT NOT NULL,
            data_json TEXT NOT NULL,
            cached_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (word_id, tab_name)
        )
    """)
    conn.commit()
    conn.close()


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_words_to_cache(count, start_rank, db_path=DB_PATH):
    """Return words ordered by frequency_rank, skipping already-fully-cached ones."""
    conn = get_connection(db_path)
    # Get words starting from start_rank, ordered by frequency
    rows = conn.execute(
        """SELECT wb.id, wb.word, wb.frequency_rank
           FROM word_bank wb
           WHERE wb.frequency_rank >= ?
           ORDER BY wb.frequency_rank ASC""",
        (start_rank,),
    ).fetchall()
    conn.close()

    # Filter out words that already have all 7 tabs cached
    conn = get_connection(db_path)
    result = []
    for row in rows:
        if len(result) >= count:
            break
        cached_count = conn.execute(
            "SELECT COUNT(*) FROM dictionary_cache WHERE word_id = ?",
            (row["id"],),
        ).fetchone()[0]
        if cached_count < 7:
            result.append(dict(row))
    conn.close()
    return result


def get_cached_tabs(word_id, db_path=DB_PATH):
    """Return set of tab_names already cached for a word."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT tab_name FROM dictionary_cache WHERE word_id = ?",
        (word_id,),
    ).fetchall()
    conn.close()
    return {r["tab_name"] for r in rows}


def insert_cache(word_id, tab_name, data, db_path=DB_PATH):
    """Insert or replace a cache entry."""
    conn = get_connection(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO dictionary_cache (word_id, tab_name, data_json)
           VALUES (?, ?, ?)""",
        (word_id, tab_name, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


# ── Generate a single tab ────────────────────────────────────────────────────

def generate_tab(word, tab_name, tab_delay):
    """Generate data for a single tab. Returns (tab_name, data) or (tab_name, None) on error."""
    try:
        data = TAB_FUNCTIONS[tab_name](word)
        time.sleep(tab_delay)
        return (tab_name, data)
    except Exception as e:
        return (tab_name, None, str(e))


# ── Cache a single word ──────────────────────────────────────────────────────

def cache_word(word_id, word, tab_delay, db_path=DB_PATH):
    """Generate and cache all missing tabs for a word.

    Uses get_all_tabs() for a single GPT call when all 7 tabs are needed,
    falls back to individual calls for partial caching.

    Returns (tabs_cached, tabs_failed, errors).
    """
    cached_tabs = get_cached_tabs(word_id, db_path)
    tabs_to_generate = [t for t in TAB_NAMES if t not in cached_tabs]

    if not tabs_to_generate:
        return (0, 0, [])

    tabs_cached = 0
    tabs_failed = 0
    errors = []

    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {
            executor.submit(generate_tab, word, tab, tab_delay): tab
            for tab in tabs_to_generate
        }
        for future in as_completed(futures):
            tab_name = futures[future]
            try:
                result = future.result()
                if len(result) == 3:
                    tabs_failed += 1
                    errors.append("  [ERROR] {}: {}".format(result[0], result[2]))
                elif result[1] is not None:
                    insert_cache(word_id, result[0], result[1], db_path)
                    tabs_cached += 1
                else:
                    tabs_failed += 1
                    errors.append("  [ERROR] {}: returned None".format(result[0]))
            except Exception as e:
                tabs_failed += 1
                errors.append("  [ERROR] {}: {}".format(tab_name, str(e)))

    total_cached = len(cached_tabs) + tabs_cached
    return (total_cached, tabs_failed, errors)


# ── Status command ────────────────────────────────────────────────────────────

def show_status(db_path=DB_PATH):
    """Print cache statistics."""
    ensure_cache_table(db_path)
    conn = get_connection(db_path)

    total_words = conn.execute("SELECT COUNT(*) FROM word_bank").fetchone()[0]

    # Words with all 7 tabs cached
    fully_cached = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT word_id FROM dictionary_cache
               GROUP BY word_id HAVING COUNT(DISTINCT tab_name) = 7
           )"""
    ).fetchone()[0]

    # Words with at least 1 tab cached
    partially_cached = conn.execute(
        "SELECT COUNT(DISTINCT word_id) FROM dictionary_cache"
    ).fetchone()[0]

    # Total individual tab entries
    total_entries = conn.execute("SELECT COUNT(*) FROM dictionary_cache").fetchone()[0]

    # Top uncached word rank
    top_uncached = conn.execute(
        """SELECT MIN(wb.frequency_rank) as min_rank
           FROM word_bank wb
           WHERE wb.id NOT IN (
               SELECT word_id FROM dictionary_cache
               GROUP BY word_id HAVING COUNT(DISTINCT tab_name) = 7
           )"""
    ).fetchone()
    top_uncached_rank = top_uncached["min_rank"] if top_uncached and top_uncached["min_rank"] else "N/A"

    pct = (fully_cached / total_words * 100) if total_words > 0 else 0

    # Per-tab breakdown
    tab_counts = conn.execute(
        "SELECT tab_name, COUNT(*) as cnt FROM dictionary_cache GROUP BY tab_name ORDER BY tab_name"
    ).fetchall()

    conn.close()

    print("Oxe Protocol — Dictionary Cache Status")
    print("=" * 45)
    print("Total words in bank:     {:>6}".format(total_words))
    print("Fully cached (7/7 tabs): {:>6}  ({:.1f}%)".format(fully_cached, pct))
    print("Partially cached:        {:>6}".format(partially_cached - fully_cached))
    print("Total cache entries:     {:>6}".format(total_entries))
    print("Top uncached word rank:  {:>6}".format(top_uncached_rank))

    if tab_counts:
        print("\nPer-tab breakdown:")
        for row in tab_counts:
            print("  {:<15} {:>6} entries".format(row["tab_name"], row["cnt"]))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pre-generate and cache dictionary data for the Oxe Protocol."
    )
    parser.add_argument(
        "--count", type=int, default=100,
        help="Number of words to process (default: 100)"
    )
    parser.add_argument(
        "--start-rank", type=int, default=1,
        help="Start from this frequency rank (default: 1)"
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="Delay in seconds between words (default: 0.5)"
    )
    parser.add_argument(
        "--tab-delay", type=float, default=0.2,
        help="Delay in seconds between tabs within a word (default: 0.2)"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show cache statistics and exit"
    )

    args = parser.parse_args()

    ensure_cache_table()

    if args.status:
        show_status()
        return

    # Get words to process
    words = get_words_to_cache(args.count, args.start_rank)

    if not words:
        print("No uncached words found starting from rank {}.".format(args.start_rank))
        return

    print("Precaching {} words starting from rank {} (delay: {}s between words, {}s between tabs)".format(
        len(words), args.start_rank, args.delay, args.tab_delay
    ))
    print("=" * 70)

    total_cached = 0
    total_failed = 0
    start_time = time.time()

    # Process words in parallel batches for much higher throughput
    batch_size = 5  # 5 words × 7 tabs = 35 concurrent GPT calls
    for batch_start in range(0, len(words), batch_size):
        batch = words[batch_start:batch_start + batch_size]
        batch_results = {}

        with ThreadPoolExecutor(max_workers=batch_size) as word_executor:
            word_futures = {
                word_executor.submit(
                    cache_word, w["id"], w["word"], args.tab_delay
                ): w
                for w in batch
            }
            for future in as_completed(word_futures):
                w = word_futures[future]
                try:
                    batch_results[w["id"]] = future.result()
                except Exception as e:
                    batch_results[w["id"]] = (0, 7, ["  [ERROR] {}".format(str(e))])

        for j, w in enumerate(batch):
            i = batch_start + j + 1
            tabs_cached, tabs_failed, errors = batch_results.get(w["id"], (0, 7, []))
            total_cached += tabs_cached
            total_failed += tabs_failed

            if i % 50 == 0 or i == 1:
                elapsed_so_far = time.time() - start_time
                rate = i / elapsed_so_far if elapsed_so_far > 0 else 0
                remaining = len(words) - i
                eta_min = remaining / rate / 60 if rate > 0 else 0
                print("Word {}/{}: '{}' (rank {}) — {}/7 tabs | {:.1f} words/min | ETA {:.0f}min{}".format(
                    i, len(words), w["word"], w["frequency_rank"], tabs_cached,
                    rate * 60, eta_min,
                    " | {} failed".format(tabs_failed) if tabs_failed else ""
                ), flush=True)

            for err in errors:
                print(err, flush=True)

        # Small delay between batches
        time.sleep(args.delay)

    elapsed = time.time() - start_time
    print("=" * 70)
    print("Done. {} tabs cached, {} failed. Elapsed: {:.1f}s ({:.1f} words/min)".format(
        total_cached, total_failed, elapsed, len(words) / elapsed * 60 if elapsed > 0 else 0
    ))


if __name__ == "__main__":
    main()
