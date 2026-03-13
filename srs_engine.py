"""
srs_engine.py — FSRS-powered Spaced Repetition engine for the Oxe Protocol.

Manages voca_20k.db: a 20,000-word bank from BR-PT frequency data.
Chunks and carrier sentences are generated dynamically from words at review time.

Usage:
    python3 srs_engine.py init                    Create the database
    python3 srs_engine.py list [--tier N]         List words (optionally by tier)
    python3 srs_engine.py due                     Show words due for review
    python3 srs_engine.py review <id> <1-4> [ms]  Record a review
    python3 srs_engine.py progress                Show tier unlock status
    python3 srs_engine.py next                    Get next due word for The Loop
"""

import sqlite3
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from fsrs import FSRS, Card, Rating

DB_PATH = Path(__file__).parent / "voca_20k.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS word_bank (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    word                   TEXT    NOT NULL UNIQUE,
    frequency_rank         INTEGER NOT NULL,
    frequency_count        INTEGER NOT NULL DEFAULT 0,
    difficulty_tier        INTEGER NOT NULL CHECK(difficulty_tier BETWEEN 1 AND 6),
    srs_stability          REAL    NOT NULL DEFAULT 0.0,
    srs_difficulty         REAL    NOT NULL DEFAULT 0.0,
    last_retrieval_latency REAL,
    mastery_level          INTEGER NOT NULL DEFAULT 0,
    srs_state              TEXT    NOT NULL,
    created_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

TIER_LABELS = {
    1: "Survival",
    2: "Daily Core",
    3: "Conversational",
    4: "Fluency",
    5: "Nuanced",
    6: "Near-Native",
}

TIER_RANGES = {
    1: (1, 1000),
    2: (1001, 3000),
    3: (3001, 7000),
    4: (7001, 12000),
    5: (12001, 17000),
    6: (17001, 20000),
}

UNLOCK_THRESHOLD = 0.80  # 80% of tier must reach mastery >= 3
LATENCY_THRESHOLD_MS = 1500  # 1.5-second automaticity rule


def _serialize_card(card):
    return json.dumps(card.to_dict(), default=str)


def _deserialize_card(json_str):
    return Card.from_dict(json.loads(json_str))


def get_tier(rank):
    for tier, (lo, hi) in TIER_RANGES.items():
        if lo <= rank <= hi:
            return tier
    return 6


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path=DB_PATH):
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


def add_word(word, rank, freq_count=0, db_path=DB_PATH):
    card = Card()
    tier = get_tier(rank)
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO word_bank
               (word, frequency_rank, frequency_count, difficulty_tier,
                srs_stability, srs_difficulty, mastery_level, srs_state)
               VALUES (?, ?, ?, ?, 0.0, 0.0, 0, ?)""",
            (word, rank, freq_count, tier, _serialize_card(card)),
        )
        row_id = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        row_id = None  # duplicate
    conn.close()
    return row_id


def get_unlocked_tier(db_path=DB_PATH):
    """Returns the highest tier the learner currently has access to."""
    conn = get_connection(db_path)
    for tier in range(1, 7):
        total = conn.execute(
            "SELECT COUNT(*) FROM word_bank WHERE difficulty_tier = ?", (tier,)
        ).fetchone()[0]
        if total == 0:
            conn.close()
            return tier
        mastered = conn.execute(
            "SELECT COUNT(*) FROM word_bank WHERE difficulty_tier = ? AND mastery_level >= 3",
            (tier,),
        ).fetchone()[0]
        if mastered / total < UNLOCK_THRESHOLD:
            conn.close()
            return tier
    conn.close()
    return 6


def get_due_words(db_path=DB_PATH):
    max_tier = get_unlocked_tier(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT * FROM word_bank
           WHERE difficulty_tier <= ?
             AND json_extract(srs_state, '$.due') <= ?
           ORDER BY json_extract(srs_state, '$.due') ASC""",
        (max_tier, now),
    ).fetchall()
    conn.close()
    return rows


def get_next_word(db_path=DB_PATH):
    max_tier = get_unlocked_tier(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    row = conn.execute(
        """SELECT * FROM word_bank
           WHERE difficulty_tier <= ?
             AND json_extract(srs_state, '$.due') <= ?
           ORDER BY json_extract(srs_state, '$.due') ASC
           LIMIT 1""",
        (max_tier, now),
    ).fetchone()
    conn.close()
    return row


def record_review(word_id, rating, latency_ms=None, db_path=DB_PATH):
    """Record a review. If latency > 1.5s, auto-downgrade rating to Hard."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM word_bank WHERE id = ?", (word_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Word {word_id} not found")

    if latency_ms is not None and latency_ms > LATENCY_THRESHOLD_MS:
        if rating.value > Rating.Hard.value:
            print(f"  Latency {latency_ms}ms > {LATENCY_THRESHOLD_MS}ms — downgrading to Hard.")
            rating = Rating.Hard

    card = _deserialize_card(row["srs_state"])
    f = FSRS()
    scheduling = f.repeat(card)
    new_card = scheduling[rating].card

    mastery = min(new_card.reps, 5)
    if rating == Rating.Again:
        mastery = max(dict(row)["mastery_level"] - 1, 0)

    conn.execute(
        """UPDATE word_bank
           SET srs_state = ?, srs_stability = ?, srs_difficulty = ?,
               mastery_level = ?, last_retrieval_latency = ?
           WHERE id = ?""",
        (
            _serialize_card(new_card),
            new_card.stability,
            new_card.difficulty,
            mastery,
            latency_ms,
            word_id,
        ),
    )
    conn.commit()
    conn.close()
    return new_card, mastery


def list_words(tier=None, db_path=DB_PATH):
    conn = get_connection(db_path)
    query = "SELECT * FROM word_bank WHERE 1=1"
    params = []
    if tier is not None:
        query += " AND difficulty_tier = ?"
        params.append(tier)
    query += " ORDER BY frequency_rank"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def tier_progress(db_path=DB_PATH):
    conn = get_connection(db_path)
    results = []
    for tier in range(1, 7):
        total = conn.execute(
            "SELECT COUNT(*) FROM word_bank WHERE difficulty_tier = ?", (tier,)
        ).fetchone()[0]
        mastered = conn.execute(
            "SELECT COUNT(*) FROM word_bank WHERE difficulty_tier = ? AND mastery_level >= 3",
            (tier,),
        ).fetchone()[0]
        pct = (mastered / total * 100) if total > 0 else 0
        results.append((tier, TIER_LABELS[tier], mastered, total, pct))
    conn.close()
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_list():
    tier_filter = None
    if "--tier" in sys.argv:
        idx = sys.argv.index("--tier")
        if idx + 1 < len(sys.argv):
            tier_filter = int(sys.argv[idx + 1])

    rows = list_words(tier=tier_filter)
    if not rows:
        print("No words found.")
        return
    print(f"{'ID':>5}  {'Rank':>5}  {'T':>1}  {'M':>1}  {'Latency':>8}  Word")
    print("-" * 50)
    for r in rows:
        lat = f"{r['last_retrieval_latency']}ms" if r["last_retrieval_latency"] else "—"
        print(f"{r['id']:>5}  {r['frequency_rank']:>5}  {r['difficulty_tier']:>1}  {r['mastery_level']:>1}  {lat:>8}  {r['word']}")
    print(f"\n{len(rows)} word(s).")


def _cli_due():
    rows = get_due_words()
    if not rows:
        print("No words due for review.")
        return
    max_tier = get_unlocked_tier()
    print(f"Current tier: {max_tier} ({TIER_LABELS[max_tier]})\n")
    print(f"{'ID':>5}  {'Rank':>5}  {'T':>1}  {'M':>1}  Word")
    print("-" * 40)
    for r in rows:
        print(f"{r['id']:>5}  {r['frequency_rank']:>5}  {r['difficulty_tier']:>1}  {r['mastery_level']:>1}  {r['word']}")
    print(f"\n{len(rows)} word(s) due.")


def _cli_next():
    row = get_next_word()
    if not row:
        print("No words due.")
        return
    print(f"Next word: {row['word']} (id={row['id']}, tier={row['difficulty_tier']}, rank={row['frequency_rank']})")


def _cli_review(word_id_str, rating_str, latency_str=None):
    word_id = int(word_id_str)
    rating_map = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}
    rating_int = int(rating_str)
    if rating_int not in rating_map:
        print("Rating must be 1 (Again), 2 (Hard), 3 (Good), or 4 (Easy).")
        sys.exit(1)
    latency_ms = int(latency_str) if latency_str else None
    card, mastery = record_review(word_id, rating_map[rating_int], latency_ms)
    print(f"Reviewed word {word_id}: mastery={mastery}, next due={card.due}")


def _cli_progress():
    progress = tier_progress()
    max_tier = get_unlocked_tier()
    print("Oxe Protocol — Tier Progress\n")
    for tier, label, mastered, total, pct in progress:
        bar_filled = int(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        if total == 0:
            status = "EMPTY"
        elif tier < max_tier:
            status = "✓ UNLOCKED"
        elif tier == max_tier:
            status = "  CURRENT"
        else:
            status = "  LOCKED"
        print(f"Tier {tier} ({label:<14}): {bar} {pct:>5.1f}% [{mastered}/{total}] {status}")


def main():
    commands = {
        "init": lambda: init_db() or print("Database initialized."),
        "list": _cli_list,
        "due": _cli_due,
        "next": _cli_next,
        "progress": _cli_progress,
    }

    if len(sys.argv) < 2 or (sys.argv[1] not in commands and sys.argv[1] != "review"):
        print(__doc__.strip())
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "review":
        if len(sys.argv) < 4:
            print("Usage: python3 srs_engine.py review <id> <rating 1-4> [latency_ms]")
            sys.exit(1)
        _cli_review(*sys.argv[2:5])
    else:
        commands[cmd]()


if __name__ == "__main__":
    main()
