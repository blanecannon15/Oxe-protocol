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
    1: "Sobrevivência",
    2: "Cotidiano",
    3: "Conversação",
    4: "Fluência",
    5: "Nuance",
    6: "Quase Nativo",
}

TIER_RANGES = {
    1: (1, 1000),
    2: (1001, 5000),
    3: (5001, 15000),
    4: (15001, 35000),
    5: (35001, 65000),
    6: (65001, 100000),
}

UNLOCK_THRESHOLD = 0.80  # 80% of tier must reach mastery >= 3
LATENCY_THRESHOLD_MS = 1000  # 1-second automaticity rule


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
    """Record a review. If latency > 1s, auto-downgrade rating to Hard."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM word_bank WHERE id = ?", (word_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Word {word_id} not found")

    latency_downgraded = False
    if latency_ms is not None and latency_ms > LATENCY_THRESHOLD_MS:
        if rating.value > Rating.Hard.value:
            rating = Rating.Hard
            latency_downgraded = True

    old_mastery = dict(row)["mastery_level"]

    card = _deserialize_card(row["srs_state"])
    f = FSRS()
    scheduling = f.repeat(card)
    new_card = scheduling[rating].card

    mastery = min(new_card.reps, 5)
    if rating == Rating.Again:
        mastery = max(old_mastery - 1, 0)

    times_failed_clause = ""
    params = [
        _serialize_card(new_card),
        new_card.stability,
        new_card.difficulty,
        mastery,
        latency_ms,
    ]
    if rating == Rating.Again:
        times_failed_clause = ", times_failed = times_failed + 1"

    conn.execute(
        f"""UPDATE word_bank
           SET srs_state = ?, srs_stability = ?, srs_difficulty = ?,
               mastery_level = ?, last_retrieval_latency = ?{times_failed_clause}
           WHERE id = ?""",
        (*params, word_id),
    )
    conn.commit()
    conn.close()

    was_mastered = mastery >= 3 and old_mastery < 3
    record_daily_activity(was_mastered=was_mastered, db_path=db_path)

    return new_card, mastery, latency_downgraded


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


def migrate_db(db_path=DB_PATH):
    """Add new columns and tables for daily stats / weak word tracking."""
    conn = get_connection(db_path)
    # Add times_failed to word_bank
    try:
        conn.execute("ALTER TABLE word_bank ADD COLUMN times_failed INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Add mastered_speed to story_library
    try:
        conn.execute("ALTER TABLE story_library ADD COLUMN mastered_speed REAL NOT NULL DEFAULT 1.0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Create daily_stats table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            words_reviewed INTEGER NOT NULL DEFAULT 0,
            words_mastered INTEGER NOT NULL DEFAULT 0,
            minutes REAL NOT NULL DEFAULT 0.0,
            session_start TEXT,
            session_end TEXT
        )
    """)
    # Create chunk_queue table — the active review layer
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunk_queue (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id                INTEGER REFERENCES word_bank(id),
            target_chunk           TEXT NOT NULL,
            carrier_sentence       TEXT NOT NULL,
            source                 TEXT NOT NULL CHECK(source IN ('dictionary','story','podcast','corpus','manual')),
            current_pass           INTEGER NOT NULL DEFAULT 1 CHECK(current_pass BETWEEN 1 AND 5),
            srs_state              TEXT NOT NULL,
            mastery_level          INTEGER NOT NULL DEFAULT 0,
            times_failed           INTEGER NOT NULL DEFAULT 0,
            last_retrieval_latency REAL,
            biometric_score        REAL,
            golden_audio_path      TEXT,
            native_audio_path      TEXT,
            image_path             TEXT,
            created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            last_reviewed          TEXT,
            UNIQUE(word_id, target_chunk)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunk_queue_due
            ON chunk_queue(json_extract(srs_state, '$.due'))
    """)
    # Voice clone registry for Neural Mapping (ElevenLabs STS)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_clone (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            voice_id     TEXT NOT NULL,
            name         TEXT NOT NULL,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
    # Search history for Dictionary Engine
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            query      TEXT NOT NULL,
            word_id    INTEGER,
            chunk_id   INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
    # Podcast library
    conn.execute("""
        CREATE TABLE IF NOT EXISTS podcast_library (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            title                 TEXT NOT NULL,
            difficulty            INTEGER NOT NULL DEFAULT 80,
            total_segments        INTEGER NOT NULL DEFAULT 12,
            body                  TEXT NOT NULL,
            focus_words           TEXT NOT NULL DEFAULT '[]',
            word_count            INTEGER,
            audio_segments        TEXT,
            times_played          INTEGER NOT NULL DEFAULT 0,
            last_played           TEXT,
            comprehension_results TEXT NOT NULL DEFAULT '[]',
            created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
    conn.commit()
    conn.close()


def record_daily_activity(was_mastered=False, db_path=DB_PATH):
    """Upsert today's row in daily_stats."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM daily_stats WHERE date = ?", (today,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO daily_stats (date, words_reviewed, words_mastered, session_start, session_end) VALUES (?, 1, ?, ?, ?)",
            (today, 1 if was_mastered else 0, now, now),
        )
    else:
        mastered_inc = 1 if was_mastered else 0
        conn.execute(
            """UPDATE daily_stats
               SET words_reviewed = words_reviewed + 1,
                   words_mastered = words_mastered + ?,
                   session_end = ?,
                   session_start = COALESCE(session_start, ?)
               WHERE date = ?""",
            (mastered_inc, now, now, today),
        )
    conn.commit()
    conn.close()


def get_daily_stats(db_path=DB_PATH):
    """Return today's stats dict."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM daily_stats WHERE date = ?", (today,)).fetchone()
    conn.close()
    if row is None:
        return {"date": today, "words_reviewed": 0, "words_mastered": 0, "minutes": 0.0, "session_start": None, "session_end": None}
    return dict(row)


def get_streak(db_path=DB_PATH):
    """Walk backwards from today counting consecutive days with words_reviewed > 0."""
    from datetime import timedelta
    conn = get_connection(db_path)
    streak = 0
    day = datetime.now(timezone.utc).date()
    while True:
        day_str = day.strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT words_reviewed FROM daily_stats WHERE date = ?", (day_str,)
        ).fetchone()
        if row and row["words_reviewed"] > 0:
            streak += 1
            day -= timedelta(days=1)
        else:
            break
    conn.close()
    return streak


def get_weak_words(db_path=DB_PATH):
    """Return words with mastery_level=0 and times_failed >= 2, limited to unlocked tiers."""
    max_tier = get_unlocked_tier(db_path)
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT * FROM word_bank
           WHERE mastery_level = 0 AND times_failed >= 2 AND difficulty_tier <= ?
           ORDER BY times_failed DESC""",
        (max_tier,),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Chunk Queue — active review layer (5-Pass Shadowing)
# ---------------------------------------------------------------------------


def add_chunk(word_id, target_chunk, carrier_sentence, source, db_path=DB_PATH):
    """Insert a chunk into the review queue. Returns chunk_id or None if duplicate."""
    card = Card()
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO chunk_queue
               (word_id, target_chunk, carrier_sentence, source, srs_state)
               VALUES (?, ?, ?, ?, ?)""",
            (word_id, target_chunk, carrier_sentence, source, _serialize_card(card)),
        )
        chunk_id = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        chunk_id = None  # duplicate word_id + target_chunk
    conn.close()
    return chunk_id


def get_next_chunk(db_path=DB_PATH):
    """Return the next due chunk from chunk_queue, respecting tier unlocks."""
    max_tier = get_unlocked_tier(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    row = conn.execute(
        """SELECT cq.*, wb.word, wb.frequency_rank, wb.difficulty_tier
           FROM chunk_queue cq
           JOIN word_bank wb ON cq.word_id = wb.id
           WHERE wb.difficulty_tier <= ?
             AND json_extract(cq.srs_state, '$.due') <= ?
           ORDER BY json_extract(cq.srs_state, '$.due') ASC
           LIMIT 1""",
        (max_tier, now),
    ).fetchone()
    conn.close()
    return row


def get_due_chunks(db_path=DB_PATH):
    """Return all due chunks from chunk_queue, respecting tier unlocks."""
    max_tier = get_unlocked_tier(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT cq.*, wb.word, wb.frequency_rank, wb.difficulty_tier
           FROM chunk_queue cq
           JOIN word_bank wb ON cq.word_id = wb.id
           WHERE wb.difficulty_tier <= ?
             AND json_extract(cq.srs_state, '$.due') <= ?
           ORDER BY json_extract(cq.srs_state, '$.due') ASC""",
        (max_tier, now),
    ).fetchall()
    conn.close()
    return rows


def update_chunk_pass(chunk_id, new_pass, db_path=DB_PATH):
    """Advance a chunk's shadowing pass (1→5). No FSRS update until pass 5 completion."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE chunk_queue SET current_pass = ? WHERE id = ?",
        (new_pass, chunk_id),
    )
    conn.commit()
    conn.close()


def record_chunk_review(chunk_id, rating, latency_ms=None, biometric_score=None, db_path=DB_PATH):
    """Record a completed review (pass 5 done). Updates FSRS and propagates mastery to word_bank."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM chunk_queue WHERE id = ?", (chunk_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Chunk {chunk_id} not found")

    latency_downgraded = False
    if latency_ms is not None and latency_ms > LATENCY_THRESHOLD_MS:
        if rating.value > Rating.Hard.value:
            rating = Rating.Hard
            latency_downgraded = True

    old_mastery = row["mastery_level"]
    card = _deserialize_card(row["srs_state"])
    f = FSRS()
    scheduling = f.repeat(card)
    new_card = scheduling[rating].card

    mastery = min(new_card.reps, 5)
    if rating == Rating.Again:
        mastery = max(old_mastery - 1, 0)

    now = datetime.now(timezone.utc).isoformat()
    times_failed_inc = ", times_failed = times_failed + 1" if rating == Rating.Again else ""

    conn.execute(
        f"""UPDATE chunk_queue
           SET srs_state = ?, mastery_level = ?, last_retrieval_latency = ?,
               biometric_score = ?, current_pass = 1, last_reviewed = ?{times_failed_inc}
           WHERE id = ?""",
        (_serialize_card(new_card), mastery, latency_ms, biometric_score, now, chunk_id),
    )

    # Propagate mastery to parent word in word_bank
    word_id = row["word_id"]
    if word_id is not None:
        conn.execute(
            "UPDATE word_bank SET mastery_level = MAX(mastery_level, ?) WHERE id = ?",
            (mastery, word_id),
        )

    conn.commit()
    conn.close()

    was_mastered = mastery >= 3 and old_mastery < 3
    record_daily_activity(was_mastered=was_mastered, db_path=db_path)

    return new_card, mastery, latency_downgraded


def get_chunk_by_id(chunk_id, db_path=DB_PATH):
    """Return a single chunk by ID, joined with word_bank."""
    conn = get_connection(db_path)
    row = conn.execute(
        """SELECT cq.*, wb.word, wb.frequency_rank, wb.difficulty_tier
           FROM chunk_queue cq
           JOIN word_bank wb ON cq.word_id = wb.id
           WHERE cq.id = ?""",
        (chunk_id,),
    ).fetchone()
    conn.close()
    return row


def get_review_feed(db_path=DB_PATH):
    """Return chunk_queue items from dictionary/story/podcast sources, due for review."""
    max_tier = get_unlocked_tier(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT cq.*, wb.word, wb.frequency_rank, wb.difficulty_tier
           FROM chunk_queue cq
           JOIN word_bank wb ON cq.word_id = wb.id
           WHERE wb.difficulty_tier <= ?
             AND cq.source IN ('dictionary', 'story', 'podcast')
             AND json_extract(cq.srs_state, '$.due') <= ?
           ORDER BY json_extract(cq.srs_state, '$.due') ASC""",
        (max_tier, now),
    ).fetchall()
    conn.close()
    return rows


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
    card, mastery, downgraded = record_review(word_id, rating_map[rating_int], latency_ms)
    msg = f"Reviewed word {word_id}: mastery={mastery}, next due={card.due}"
    if downgraded:
        msg += f" (latency downgraded to Hard)"
    print(msg)


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
