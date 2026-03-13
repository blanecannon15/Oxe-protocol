"""
build_corpus.py — Download BR-PT frequency corpus and seed voca_20k.db
with 20,000 words as a word bank. Chunks and sentences are generated
dynamically at review time, not stored.

Usage:
    python3 build_corpus.py --download    Download pt_br_50k.txt
    python3 build_corpus.py --seed        Parse top 20K words and insert into DB
    python3 build_corpus.py --all         Download + seed
"""

import sys
import urllib.request
from pathlib import Path

from srs_engine import init_db, add_word, get_connection, DB_PATH

DATA_DIR = Path(__file__).parent / "data"
FREQ_FILE = DATA_DIR / "pt_br_50k.txt"
FREQ_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/pt_br/pt_br_50k.txt"

MAX_WORDS = 20000


def download_frequency_list():
    """Download BR-PT frequency list from hermitdave/FrequencyWords."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if FREQ_FILE.exists():
        print(f"Frequency file already exists at {FREQ_FILE}")
        return

    print(f"Downloading from {FREQ_URL}...")
    urllib.request.urlretrieve(FREQ_URL, str(FREQ_FILE))
    print(f"Saved to {FREQ_FILE}")

    with open(FREQ_FILE, encoding="utf-8") as f:
        count = sum(1 for _ in f)
    print(f"Total entries: {count}")


def parse_frequency_list():
    """Parse the frequency file into (word, frequency, rank) tuples."""
    words = []
    with open(FREQ_FILE, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if i > MAX_WORDS:
                break
            parts = line.strip().split()
            if len(parts) >= 2:
                word = parts[0]
                freq = int(parts[1])
                words.append((word, freq, i))
    return words


def seed_word_bank():
    """Insert 20K words into the database."""
    if not FREQ_FILE.exists():
        print("Frequency file not found. Run with --download first.")
        sys.exit(1)

    init_db()
    words = parse_frequency_list()
    print(f"Seeding {len(words)} words into voca_20k.db...")

    inserted = 0
    skipped = 0
    for word, freq, rank in words:
        row_id = add_word(word, rank, freq)
        if row_id is not None:
            inserted += 1
        else:
            skipped += 1

    print(f"Done: {inserted} inserted, {skipped} duplicates skipped.")

    # Show tier distribution
    conn = get_connection()
    for tier in range(1, 7):
        count = conn.execute(
            "SELECT COUNT(*) FROM word_bank WHERE difficulty_tier = ?", (tier,)
        ).fetchone()[0]
        print(f"  Tier {tier}: {count} words")
    conn.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    arg = sys.argv[1]
    if arg == "--download":
        download_frequency_list()
    elif arg == "--seed":
        seed_word_bank()
    elif arg == "--all":
        download_frequency_list()
        seed_word_bank()
    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
