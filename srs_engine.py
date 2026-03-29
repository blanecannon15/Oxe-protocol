"""
srs_engine.py — FSRS-6 powered Spaced Repetition engine for the Oxe Protocol.

Manages voca_20k.db: a 20,000-word bank from BR-PT frequency data.
Chunks and carrier sentences are generated dynamically from words at review time.

Uses FSRS-6 with 21 scientifically optimized parameters and personalized
forgetting curve decay (w[20]).

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
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from fsrs import FSRS, Card, Rating

# ---------------------------------------------------------------------------
# FSRS-6: 21-parameter defaults with personalized forgetting curve decay
# ---------------------------------------------------------------------------

FSRS6_WEIGHTS = (
    0.2120,   # w[0]  Initial stability: Again (~5 hours)
    1.2931,   # w[1]  Initial stability: Hard (~1.3 days)
    2.3065,   # w[2]  Initial stability: Good (~2.3 days)
    8.2956,   # w[3]  Initial stability: Easy (~8.3 days)
    6.4133,   # w[4]  Base initial difficulty
    0.8334,   # w[5]  Difficulty sensitivity to first grade
    3.0194,   # w[6]  Difficulty change rate per grade
    0.0010,   # w[7]  Mean reversion strength
    1.8722,   # w[8]  Recall stability increase multiplier
    0.1666,   # w[9]  Stability saturation exponent
    0.7960,   # w[10] Retrievability sensitivity on recall
    1.4835,   # w[11] Post-lapse stability scaling
    0.0614,   # w[12] Difficulty exponent in lapse formula
    0.2629,   # w[13] Stability exponent in lapse formula
    1.6483,   # w[14] Retrievability factor in lapse formula
    0.6014,   # w[15] Hard rating modifier
    1.8729,   # w[16] Easy rating modifier
    0.5425,   # w[17] Same-day review stability coefficient
    0.0912,   # w[18] Same-day grade offset
    0.0658,   # w[19] Same-day stability decay
    0.1542,   # w[20] Forgetting curve decay exponent (FSRS-6)
)

FSRS6_DESIRED_RETENTION = 0.9
FSRS6_MAXIMUM_INTERVAL = 365  # cap at 1 year for active language learning


class FSRS6(FSRS):
    """FSRS-6 scheduler with 21 parameters and personalized forgetting curve.

    Extends the base FSRS class to support:
      - w[19]: same-day stability decay factor
      - w[20]: personalized forgetting curve DECAY exponent
      - FACTOR recalculated from the personalized DECAY
    """

    def __init__(self, w=None, request_retention=None, maximum_interval=None):
        w = w if w is not None else FSRS6_WEIGHTS
        request_retention = request_retention if request_retention is not None else FSRS6_DESIRED_RETENTION
        maximum_interval = maximum_interval if maximum_interval is not None else FSRS6_MAXIMUM_INTERVAL

        # Store the full 21-weight tuple before calling super
        self._w_full = w

        # Base class accepts the full tuple; it only indexes up to w[18]
        super().__init__(
            w=w,
            request_retention=request_retention,
            maximum_interval=maximum_interval,
        )

        # Override DECAY with personalized value from w[20]
        if len(w) >= 21:
            self.DECAY = -w[20]  # negative because the formula uses power decay
        else:
            self.DECAY = -0.5  # fallback to classic FSRS default

        # Recalculate FACTOR from the new DECAY
        self.FACTOR = self.p.request_retention ** (1 / self.DECAY) - 1

    def short_term_stability(self, stability, rating):
        """FSRS-6 short-term stability uses w[17], w[18], and w[19]."""
        w = self._w_full
        if len(w) >= 20:
            return stability * math.exp(
                w[17] * (rating - 3 + w[18]) * math.pow(stability, -w[19])
            )
        # Fallback to base class behavior (FSRS-5 formula)
        return super().short_term_stability(stability, rating)


def _make_fsrs():
    """Factory for creating the configured FSRS-6 scheduler instance."""
    return FSRS6()


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
    f = _make_fsrs()
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
    conn.execute(
        "INSERT INTO review_history (item_type, item_id, rating, latency_ms) VALUES (?, ?, ?, ?)",
        ('word', word_id, rating, latency_ms),
    )
    conn.commit()
    conn.close()

    was_mastered = mastery >= 3 and old_mastery < 3
    record_daily_activity(was_mastered=was_mastered, db_path=db_path)

    # Update acquisition state and backfill review_history
    try:
        from acquisition_engine import update_state_after_review
        state_result = update_state_after_review('word', word_id, rating, latency_ms, 'clean')
        conn2 = get_connection(db_path)
        conn2.execute(
            """UPDATE review_history SET state_before = ?, state_after = ?
               WHERE item_type = 'word' AND item_id = ? AND state_before IS NULL
               ORDER BY timestamp DESC LIMIT 1""",
            (state_result['old_state'], state_result['new_state'], word_id),
        )
        conn2.commit()
        conn2.close()
    except Exception as e:
        print(f"[SRS] Acquisition state update warning: {e}")

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
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunk_queue_due
                ON chunk_queue(json_extract(srs_state, '$.due'))
        """)
    except sqlite3.OperationalError:
        pass  # skip if JSON is malformed in existing rows
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


def migrate_v2(db_path=DB_PATH):
    """Create tables for the acquisition engine brain (v2 schema)."""
    conn = get_connection(db_path)
    conn.executescript("""
        -- Automaticity state tracking per word/chunk
        CREATE TABLE IF NOT EXISTS automaticity_state (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type             TEXT NOT NULL CHECK(item_type IN ('word','chunk')),
            item_id               INTEGER NOT NULL,
            state                 TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK(state IN (
                    'UNKNOWN','RECOGNIZED','CONTEXT_KNOWN',
                    'EFFORTFUL_AUDIO','AUTOMATIC_CLEAN',
                    'AUTOMATIC_NATIVE','AVAILABLE_OUTPUT'
                )),
            confidence            REAL NOT NULL DEFAULT 0.0,
            exposure_count        INTEGER NOT NULL DEFAULT 0,
            correct_streak        INTEGER NOT NULL DEFAULT 0,
            latency_history       TEXT NOT NULL DEFAULT '[]',
            avg_latency_ms        REAL,
            latency_trend         REAL DEFAULT 0.0,
            clean_audio_success   REAL NOT NULL DEFAULT 0.0,
            native_audio_success  REAL NOT NULL DEFAULT 0.0,
            clean_attempts        INTEGER NOT NULL DEFAULT 0,
            native_attempts       INTEGER NOT NULL DEFAULT 0,
            output_success        REAL NOT NULL DEFAULT 0.0,
            output_attempts       INTEGER NOT NULL DEFAULT 0,
            last_biometric        REAL,
            last_state_change     TEXT,
            last_reviewed         TEXT,
            created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(item_type, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_auto_state ON automaticity_state(state);
        CREATE INDEX IF NOT EXISTS idx_auto_item ON automaticity_state(item_type, item_id);

        -- Fragile item detection
        CREATE TABLE IF NOT EXISTS fragile_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type       TEXT NOT NULL CHECK(item_type IN ('word','chunk')),
            item_id         INTEGER NOT NULL,
            fragility_type  TEXT NOT NULL CHECK(fragility_type IN (
                'familiar_but_fragile','known_but_slow','text_only',
                'clean_audio_only','blocked_by_prosody'
            )),
            fragility_score REAL NOT NULL DEFAULT 0.0,
            detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            resolved_at     TEXT,
            UNIQUE(item_type, item_id, fragility_type)
        );
        CREATE INDEX IF NOT EXISTS idx_fragile_active ON fragile_items(fragility_type) WHERE resolved_at IS NULL;

        -- Chunk families and variants
        CREATE TABLE IF NOT EXISTS chunk_families (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            root_form         TEXT NOT NULL UNIQUE,
            word_count        INTEGER NOT NULL,
            frequency_score   REAL NOT NULL DEFAULT 0.0,
            naturalness_score REAL NOT NULL DEFAULT 0.0,
            bahia_relevance   REAL NOT NULL DEFAULT 0.0,
            composite_rank    REAL NOT NULL DEFAULT 0.0,
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE TABLE IF NOT EXISTS chunk_variants (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id      INTEGER NOT NULL REFERENCES chunk_families(id),
            variant_form   TEXT NOT NULL,
            source         TEXT NOT NULL CHECK(source IN ('story','podcast','conversation','corpus','manual','llm')),
            source_id      INTEGER,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(family_id, variant_form)
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_family_rank ON chunk_families(composite_rank DESC);

        -- Junction: chunk families ↔ component words
        CREATE TABLE IF NOT EXISTS chunk_family_words (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id  INTEGER NOT NULL REFERENCES chunk_families(id),
            word_id    INTEGER NOT NULL REFERENCES word_bank(id),
            UNIQUE(family_id, word_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cfw_word ON chunk_family_words(word_id);
        CREATE INDEX IF NOT EXISTS idx_cfw_family ON chunk_family_words(family_id);

        -- Daily planning and session tracking
        CREATE TABLE IF NOT EXISTS daily_plan (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL UNIQUE,
            plan_json       TEXT NOT NULL,
            generated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            completed_pct   REAL NOT NULL DEFAULT 0.0,
            actual_json     TEXT
        );
        CREATE TABLE IF NOT EXISTS session_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            event_data      TEXT NOT NULL DEFAULT '{}',
            timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE TABLE IF NOT EXISTS fatigue_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            minute_offset   INTEGER NOT NULL,
            accuracy_5min   REAL,
            avg_latency_5min REAL,
            replay_freq     REAL,
            items_per_minute REAL,
            fatigue_score   REAL NOT NULL,
            action_taken    TEXT,
            timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        -- Content metadata for stories, podcasts, conversations
        CREATE TABLE IF NOT EXISTS content_metadata (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type      TEXT NOT NULL CHECK(content_type IN ('story','podcast','conversation')),
            content_id        INTEGER NOT NULL,
            difficulty_level  TEXT NOT NULL,
            accent            TEXT NOT NULL DEFAULT 'baiano',
            clarity           REAL NOT NULL DEFAULT 100.0,
            speech_rate_wpm   REAL,
            lexical_density   REAL NOT NULL DEFAULT 0.0,
            chunk_overlap_pct REAL NOT NULL DEFAULT 0.0,
            compression_pct   REAL,
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(content_type, content_id)
        );

        -- Speech unlock stages and conversation sessions
        CREATE TABLE IF NOT EXISTS speech_unlock (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            stage           INTEGER NOT NULL DEFAULT 1 CHECK(stage BETWEEN 1 AND 6),
            stage_name      TEXT NOT NULL DEFAULT 'echo',
            entered_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            criteria_met    TEXT NOT NULL DEFAULT '{}',
            UNIQUE(stage)
        );
        CREATE TABLE IF NOT EXISTS conversa_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            speech_stage    INTEGER NOT NULL,
            mode            TEXT NOT NULL,
            prompt_type     TEXT,
            prompt_data     TEXT,
            messages        TEXT NOT NULL DEFAULT '[]',
            chunks_used     TEXT NOT NULL DEFAULT '[]',
            chunks_introduced TEXT NOT NULL DEFAULT '[]',
            post_extraction TEXT,
            duration_seconds INTEGER,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        -- Seed stage 1
        INSERT OR IGNORE INTO speech_unlock (stage, stage_name) VALUES (1, 'echo');

        -- Review history (every review event as a discrete row)
        CREATE TABLE IF NOT EXISTS review_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type       TEXT NOT NULL CHECK(item_type IN ('word','chunk')),
            item_id         INTEGER NOT NULL,
            rating          INTEGER NOT NULL,
            latency_ms      REAL,
            biometric_score REAL,
            mode            TEXT,
            audio_type      TEXT,
            state_before    TEXT,
            state_after     TEXT,
            timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_review_hist_item ON review_history(item_type, item_id);
        CREATE INDEX IF NOT EXISTS idx_review_hist_ts ON review_history(timestamp);

        -- Activity time tracking (real clock time per activity per day)
        CREATE TABLE IF NOT EXISTS activity_timer (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            activity        TEXT NOT NULL CHECK(activity IN (
                'srs_drill','listening','shadowing','conversa','assembly','dictionary'
            )),
            started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            ended_at        TEXT,
            seconds_spent   INTEGER NOT NULL DEFAULT 0,
            UNIQUE(date, activity, started_at)
        );
        CREATE INDEX IF NOT EXISTS idx_activity_timer_date ON activity_timer(date);

        -- Cumulative listening hours baseline
        CREATE TABLE IF NOT EXISTS learner_profile (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL
        );
        INSERT OR IGNORE INTO learner_profile (key, value) VALUES ('listening_hours_baseline', '900');
        INSERT OR IGNORE INTO learner_profile (key, value) VALUES ('daily_target_minutes', '600');

        -- Dictionary cache for per-tab GPT-4o results
        CREATE TABLE IF NOT EXISTS dictionary_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER NOT NULL,
            tab_name TEXT NOT NULL CHECK(tab_name IN ('definition','examples','pronunciation','expressions','conjugation','synonyms','chunks')),
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(word_id, tab_name)
        );
        CREATE INDEX IF NOT EXISTS idx_dict_cache ON dictionary_cache(word_id);

        -- Milestones (achievement tracking)
        CREATE TABLE IF NOT EXISTS milestones (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            milestone_type  TEXT NOT NULL,
            milestone_key   TEXT NOT NULL,
            milestone_data  TEXT NOT NULL DEFAULT '{}',
            achieved_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            notified        INTEGER NOT NULL DEFAULT 0,
            UNIQUE(milestone_type, milestone_key)
        );

        -- Content segments (paragraph/segment-level tracking)
        CREATE TABLE IF NOT EXISTS content_segments (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type      TEXT NOT NULL CHECK(content_type IN ('story','podcast')),
            content_id        INTEGER NOT NULL,
            segment_index     INTEGER NOT NULL,
            text              TEXT,
            comprehension_pct REAL,
            replays           INTEGER NOT NULL DEFAULT 0,
            latency_ms        REAL,
            timestamp         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(content_type, content_id, segment_index)
        );
        CREATE INDEX IF NOT EXISTS idx_seg_content ON content_segments(content_type, content_id);
    """)
    # Safe column additions (ignore if already exist)
    for col_sql in [
        "ALTER TABLE content_metadata ADD COLUMN topic TEXT DEFAULT ''",
        "ALTER TABLE content_metadata ADD COLUMN grammar_density REAL DEFAULT 0.0",
        "ALTER TABLE content_metadata ADD COLUMN emotional_intensity REAL DEFAULT 0.0",
        "ALTER TABLE content_metadata ADD COLUMN social_context TEXT DEFAULT ''",
        "ALTER TABLE content_metadata ADD COLUMN native_likeness REAL DEFAULT 0.0",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass
    conn.commit()
    conn.close()


def migrate_v3(db_path=DB_PATH):
    """V3 schema: voice profiles, audio coverage, word-chunk links, search index, accent columns."""
    conn = get_connection(db_path)
    conn.executescript("""
        -- Voice profiles for multi-accent TTS (Paulista default, Carioca, Baiano)
        CREATE TABLE IF NOT EXISTS voice_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            accent      TEXT NOT NULL UNIQUE,
            label       TEXT NOT NULL,
            voice_id    TEXT NOT NULL,
            model_id    TEXT NOT NULL DEFAULT 'eleven_multilingual_v2',
            stability   REAL NOT NULL DEFAULT 0.45,
            similarity  REAL NOT NULL DEFAULT 0.85,
            style       REAL NOT NULL DEFAULT 0.55,
            speaker_boost INTEGER NOT NULL DEFAULT 1,
            weight      REAL NOT NULL DEFAULT 0.0,
            tts_prefix  TEXT NOT NULL DEFAULT '',
            is_default  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        -- Seed voice profiles: Paulista (default), Carioca, Baiano
        INSERT OR IGNORE INTO voice_profiles (accent, label, voice_id, stability, similarity, style, weight, tts_prefix, is_default)
        VALUES ('paulista', 'Paulista Profissional', 'ELBrtmIkk40wCZ5YnlwM', 0.50, 0.85, 0.45, 0.60, '', 1);
        INSERT OR IGNORE INTO voice_profiles (accent, label, voice_id, stability, similarity, style, weight, tts_prefix, is_default)
        VALUES ('carioca', 'Carioca Social', 'ELBrtmIkk40wCZ5YnlwM', 0.40, 0.85, 0.60, 0.25, '', 0);
        INSERT OR IGNORE INTO voice_profiles (accent, label, voice_id, stability, similarity, style, weight, tts_prefix, is_default)
        VALUES ('baiano', 'Baiano Cultural', 'ELBrtmIkk40wCZ5YnlwM', 0.45, 0.85, 0.55, 0.15, 'Oxe, ', 0);

        -- Audio coverage tracking (per word/chunk × accent)
        CREATE TABLE IF NOT EXISTS audio_coverage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type   TEXT NOT NULL CHECK(item_type IN ('word','chunk')),
            item_id     INTEGER NOT NULL,
            accent      TEXT NOT NULL DEFAULT 'paulista',
            status      TEXT NOT NULL DEFAULT 'missing'
                CHECK(status IN ('missing','queued','generating','cached','failed','native')),
            audio_path  TEXT,
            source      TEXT DEFAULT 'tts'
                CHECK(source IN ('tts','native','clone','manual')),
            duration_ms INTEGER,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at  TEXT,
            UNIQUE(item_type, item_id, accent)
        );
        CREATE INDEX IF NOT EXISTS idx_audio_cov_status ON audio_coverage(status);
        CREATE INDEX IF NOT EXISTS idx_audio_cov_item ON audio_coverage(item_type, item_id);

        -- Word-to-chunk auto-linkage tracking
        CREATE TABLE IF NOT EXISTS word_chunk_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id     INTEGER NOT NULL REFERENCES word_bank(id),
            chunk_id    INTEGER REFERENCES chunk_queue(id),
            family_id   INTEGER REFERENCES chunk_families(id),
            link_type   TEXT NOT NULL DEFAULT 'auto'
                CHECK(link_type IN ('auto','manual','extracted')),
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(word_id, chunk_id)
        );
        CREATE INDEX IF NOT EXISTS idx_wcl_word ON word_chunk_links(word_id);

        -- Fast search index (normalized terms for instant prefix match)
        CREATE TABLE IF NOT EXISTS search_index (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            term        TEXT NOT NULL,
            normalized  TEXT NOT NULL,
            item_type   TEXT NOT NULL CHECK(item_type IN ('word','chunk','variant')),
            item_id     INTEGER NOT NULL,
            source      TEXT NOT NULL DEFAULT 'lemma',
            priority    INTEGER NOT NULL DEFAULT 0,
            UNIQUE(term, item_type, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_search_norm ON search_index(normalized);
        CREATE INDEX IF NOT EXISTS idx_search_type ON search_index(item_type, normalized);

        -- SRS internal clock: tracks review sessions, daily windows, session quality
        CREATE TABLE IF NOT EXISTS srs_clock (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            date              TEXT NOT NULL,
            session_start     TEXT NOT NULL,
            session_end       TEXT,
            reviews_completed INTEGER NOT NULL DEFAULT 0,
            chunks_reviewed   INTEGER NOT NULL DEFAULT 0,
            words_reviewed    INTEGER NOT NULL DEFAULT 0,
            avg_latency_ms    REAL,
            accuracy_pct      REAL,
            fatigue_at_end    REAL,
            session_type      TEXT NOT NULL DEFAULT 'drill'
                CHECK(session_type IN ('drill','shadowing','listening','conversa','mixed')),
            notes             TEXT,
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_srs_clock_date ON srs_clock(date);

        -- Daily review window summary (one row per day)
        CREATE TABLE IF NOT EXISTS srs_daily_window (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            date              TEXT NOT NULL UNIQUE,
            first_review      TEXT,
            last_review       TEXT,
            total_sessions    INTEGER NOT NULL DEFAULT 0,
            total_reviews     INTEGER NOT NULL DEFAULT 0,
            total_minutes     REAL NOT NULL DEFAULT 0.0,
            avg_accuracy      REAL,
            avg_latency_ms    REAL,
            peak_fatigue      REAL,
            items_due_start   INTEGER NOT NULL DEFAULT 0,
            items_due_end     INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_srs_daily_date ON srs_daily_window(date);
    """)

    # Additive columns on chunk_families for multi-accent relevance
    for col_sql in [
        "ALTER TABLE chunk_families ADD COLUMN paulista_relevance REAL DEFAULT 0.0",
        "ALTER TABLE chunk_families ADD COLUMN carioca_relevance REAL DEFAULT 0.0",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass

    conn.commit()
    conn.close()


# ── V4 Migration: Item tracking fields ────────────────────────────────

def migrate_v4(db_path=DB_PATH):
    """V4 schema: add replay_count, last_shadow_score, tag to chunk_queue."""
    conn = get_connection(db_path)
    for col, coltype, default in [
        ("replay_count", "INTEGER", "0"),
        ("last_shadow_score", "REAL", "NULL"),
        ("tag", "TEXT", "NULL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE chunk_queue ADD COLUMN {col} {coltype} DEFAULT {default}")
        except Exception:
            pass  # column already exists

    # Add replay_count to word_bank if missing
    try:
        conn.execute("ALTER TABLE word_bank ADD COLUMN replay_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE word_bank ADD COLUMN last_shadow_score REAL DEFAULT NULL")
    except Exception:
        pass

    conn.commit()
    conn.close()


# ── SRS Clock Service ���───────────────────────────────────────────────

def migrate_v5(db_path=DB_PATH):
    """V5 schema: targeted lexical training — multi-chunk per word, target audio, support links."""
    conn = get_connection(db_path)

    # 1. Add target_audio_path to chunk_queue (short TTS of just the chunk)
    for col, coltype, default in [
        ("target_audio_path", "TEXT", "NULL"),
        ("item_role", "TEXT", "'primary'"),  # primary | support | context
    ]:
        try:
            conn.execute(f"ALTER TABLE chunk_queue ADD COLUMN {col} {coltype} DEFAULT {default}")
        except Exception:
            pass

    # 2. Support chunks linking table — connects primary review item to support chunks
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS support_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            primary_chunk_id INTEGER NOT NULL REFERENCES chunk_queue(id),
            support_chunk_id INTEGER NOT NULL REFERENCES chunk_queue(id),
            link_type        TEXT NOT NULL DEFAULT 'chunk_support'
                CHECK(link_type IN ('chunk_support', 'sentence_context')),
            display_order    INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(primary_chunk_id, support_chunk_id)
        );
        CREATE INDEX IF NOT EXISTS idx_support_primary ON support_links(primary_chunk_id);
    """)

    conn.commit()
    conn.close()


def clock_start_session(session_type="drill", db_path=DB_PATH):
    """Start a new SRS clock session. Returns session id."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    cur = conn.execute(
        """INSERT INTO srs_clock (date, session_start, session_type)
           VALUES (?, ?, ?)""",
        (today, now.isoformat(), session_type),
    )
    session_id = cur.lastrowid

    # Upsert daily window
    conn.execute(
        """INSERT INTO srs_daily_window (date, first_review, total_sessions)
           VALUES (?, ?, 1)
           ON CONFLICT(date) DO UPDATE SET
               total_sessions = total_sessions + 1,
               first_review = COALESCE(first_review, excluded.first_review)""",
        (today, now.isoformat()),
    )
    conn.commit()
    conn.close()
    return session_id


def clock_end_session(session_id, reviews=0, chunks=0, words=0,
                      avg_latency=None, accuracy=None, fatigue=None,
                      db_path=DB_PATH):
    """End an SRS clock session with stats."""
    now = datetime.now(timezone.utc)
    conn = get_connection(db_path)
    conn.execute(
        """UPDATE srs_clock SET
               session_end = ?,
               reviews_completed = ?,
               chunks_reviewed = ?,
               words_reviewed = ?,
               avg_latency_ms = ?,
               accuracy_pct = ?,
               fatigue_at_end = ?
           WHERE id = ?""",
        (now.isoformat(), reviews, chunks, words, avg_latency, accuracy, fatigue,
         session_id),
    )

    # Update daily window
    row = conn.execute("SELECT date, session_start FROM srs_clock WHERE id=?",
                       (session_id,)).fetchone()
    if row:
        today = row["date"]
        start = row["session_start"]
        try:
            start_dt = datetime.fromisoformat(start)
            minutes = (now - start_dt).total_seconds() / 60.0
        except Exception:
            minutes = 0
        conn.execute(
            """UPDATE srs_daily_window SET
                   last_review = ?,
                   total_reviews = total_reviews + ?,
                   total_minutes = total_minutes + ?,
                   avg_accuracy = ?,
                   avg_latency_ms = COALESCE(?, avg_latency_ms),
                   peak_fatigue = MAX(COALESCE(peak_fatigue, 0), COALESCE(?, 0))
               WHERE date = ?""",
            (now.isoformat(), reviews, round(minutes, 1), accuracy,
             avg_latency, fatigue, today),
        )
    conn.commit()
    conn.close()


def clock_get_today(db_path=DB_PATH):
    """Return today's SRS daily window summary."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM srs_daily_window WHERE date=?", (today,)).fetchone()
    conn.close()
    return dict(row) if row else None


def clock_get_sessions(date=None, limit=10, db_path=DB_PATH):
    """Return recent SRS clock sessions."""
    conn = get_connection(db_path)
    if date:
        rows = conn.execute(
            "SELECT * FROM srs_clock WHERE date=? ORDER BY session_start DESC",
            (date,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM srs_clock ORDER BY session_start DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clock_get_weekly_summary(db_path=DB_PATH):
    """Return last 7 days of daily window data."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM srs_daily_window ORDER BY date DESC LIMIT 7"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    """Return the next due chunk from chunk_queue, respecting tier unlocks.

    Priority order:
    1. Manual/priority words first (source='manual')
    2. Then by frequency rank (most common words first)
    3. Randomized among items with similar priority (batch of 10)
    Already-reviewed items (reps > 0) ordered by due date as normal.
    """
    max_tier = get_unlocked_tier(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    # Pick from a batch of top candidates, randomized
    rows = conn.execute(
        """SELECT cq.*, wb.word, wb.frequency_rank, wb.difficulty_tier
           FROM chunk_queue cq
           JOIN word_bank wb ON cq.word_id = wb.id
           WHERE wb.difficulty_tier <= ?
             AND json_extract(cq.srs_state, '$.due') <= ?
           ORDER BY
             CASE WHEN json_extract(cq.srs_state, '$.reps') > 0 THEN 0 ELSE 1 END DESC,
             CASE WHEN json_extract(cq.srs_state, '$.reps') > 0
                  THEN json_extract(cq.srs_state, '$.due')
                  ELSE NULL END ASC,
             CASE WHEN cq.source = 'manual' THEN 0 ELSE 1 END,
             wb.frequency_rank ASC
           LIMIT 10""",
        (max_tier, now),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    # Among the top 10 candidates, pick randomly to avoid repetition
    import random
    return random.choice(rows)


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
    f = _make_fsrs()
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

    conn.execute(
        "INSERT INTO review_history (item_type, item_id, rating, latency_ms, biometric_score) VALUES (?, ?, ?, ?, ?)",
        ('chunk', chunk_id, rating, latency_ms, biometric_score),
    )
    conn.commit()
    conn.close()

    was_mastered = mastery >= 3 and old_mastery < 3
    record_daily_activity(was_mastered=was_mastered, db_path=db_path)

    # Update acquisition state and backfill review_history
    try:
        from acquisition_engine import update_state_after_review
        state_result = update_state_after_review(
            'chunk', chunk_id, rating, latency_ms, 'clean', biometric_score
        )
        conn2 = get_connection(db_path)
        conn2.execute(
            """UPDATE review_history SET state_before = ?, state_after = ?
               WHERE item_type = 'chunk' AND item_id = ? AND state_before IS NULL
               ORDER BY timestamp DESC LIMIT 1""",
            (state_result['old_state'], state_result['new_state'], chunk_id),
        )
        conn2.commit()
        conn2.close()
    except Exception as e:
        print(f"[SRS] Chunk acquisition state update warning: {e}")

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
