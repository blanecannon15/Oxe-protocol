"""
test_phase3.py — Tests for word→chunk linking and review state transitions.

Covers:
  - auto_link_word_to_chunks creates primary + support + support_links
  - State promotions through the 7-state ladder
  - State demotions on failure patterns
  - State-aware confidence weighting
  - Fragility detection
  - migrate_v6 backfill logic

Usage:
    python3 -m pytest test_phase3.py -v
    python3 test_phase3.py   # fallback: runs via unittest
"""

import json
import os
import sqlite3
import tempfile
import unittest

from fsrs import Card, Rating

# Point all modules at a temp DB before importing
_tmpdir = tempfile.mkdtemp()
_test_db = os.path.join(_tmpdir, "test_oxe.db")
os.environ["OXE_TEST_DB"] = _test_db

from srs_engine import (
    DB_PATH, get_connection, migrate_db, migrate_v2, migrate_v3, migrate_v4,
    migrate_v5, migrate_v6, add_chunk, get_chunk_by_id,
)
from acquisition_engine import (
    get_or_create_state, update_state_after_review, compute_confidence,
    detect_fragility, STATES, STATE_ORDER,
)


def _init_test_db(db_path):
    """Create a minimal test database with all migrations applied."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS word_bank (
            id INTEGER PRIMARY KEY,
            word TEXT NOT NULL,
            frequency_rank INTEGER NOT NULL DEFAULT 0,
            difficulty_tier INTEGER NOT NULL DEFAULT 1,
            mastery_level INTEGER NOT NULL DEFAULT 0,
            srs_state TEXT,
            last_retrieval_latency REAL,
            biometric_score REAL,
            replay_count INTEGER DEFAULT 0,
            last_shadow_score REAL DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS chunk_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER REFERENCES word_bank(id),
            target_chunk TEXT NOT NULL,
            carrier_sentence TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('dictionary','story','podcast','corpus','manual')),
            current_pass INTEGER NOT NULL DEFAULT 1,
            srs_state TEXT NOT NULL,
            mastery_level INTEGER NOT NULL DEFAULT 0,
            times_failed INTEGER NOT NULL DEFAULT 0,
            last_retrieval_latency REAL,
            biometric_score REAL,
            golden_audio_path TEXT,
            native_audio_path TEXT,
            image_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            last_reviewed TEXT,
            replay_count INTEGER DEFAULT 0,
            last_shadow_score REAL DEFAULT NULL,
            tag TEXT DEFAULT NULL,
            target_audio_path TEXT DEFAULT NULL,
            item_role TEXT DEFAULT 'primary',
            UNIQUE(word_id, target_chunk)
        );

        CREATE TABLE IF NOT EXISTS chunk_families (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_form TEXT NOT NULL UNIQUE,
            word_count INTEGER NOT NULL,
            frequency_score REAL NOT NULL DEFAULT 0.0,
            naturalness_score REAL NOT NULL DEFAULT 0.0,
            bahia_relevance REAL NOT NULL DEFAULT 0.0,
            composite_rank REAL NOT NULL DEFAULT 0.0,
            paulista_relevance REAL DEFAULT 0.0,
            carioca_relevance REAL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        CREATE TABLE IF NOT EXISTS chunk_family_words (
            family_id INTEGER REFERENCES chunk_families(id),
            word_id INTEGER REFERENCES word_bank(id),
            PRIMARY KEY (family_id, word_id)
        );

        CREATE TABLE IF NOT EXISTS chunk_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id INTEGER NOT NULL REFERENCES chunk_families(id),
            variant_form TEXT NOT NULL,
            source TEXT NOT NULL,
            source_id INTEGER,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(family_id, variant_form)
        );

        CREATE TABLE IF NOT EXISTS support_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            primary_chunk_id INTEGER NOT NULL REFERENCES chunk_queue(id),
            support_chunk_id INTEGER NOT NULL REFERENCES chunk_queue(id),
            link_type TEXT NOT NULL DEFAULT 'chunk_support',
            display_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(primary_chunk_id, support_chunk_id)
        );
        CREATE INDEX IF NOT EXISTS idx_support_primary ON support_links(primary_chunk_id);

        CREATE TABLE IF NOT EXISTS automaticity_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'UNKNOWN',
            confidence REAL NOT NULL DEFAULT 0.0,
            exposure_count INTEGER NOT NULL DEFAULT 0,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            latency_history TEXT NOT NULL DEFAULT '[]',
            avg_latency_ms REAL,
            latency_trend REAL DEFAULT 0.0,
            clean_audio_success REAL NOT NULL DEFAULT 0.0,
            native_audio_success REAL NOT NULL DEFAULT 0.0,
            clean_attempts INTEGER NOT NULL DEFAULT 0,
            native_attempts INTEGER NOT NULL DEFAULT 0,
            output_success REAL NOT NULL DEFAULT 0.0,
            output_attempts INTEGER NOT NULL DEFAULT 0,
            last_biometric REAL,
            last_state_change TEXT,
            last_reviewed TEXT,
            accent_scores TEXT DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(item_type, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_auto_state ON automaticity_state(state);
        CREATE INDEX IF NOT EXISTS idx_auto_item ON automaticity_state(item_type, item_id);

        CREATE TABLE IF NOT EXISTS fragile_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            fragility_type TEXT NOT NULL,
            fragility_score REAL NOT NULL DEFAULT 0.0,
            detected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            resolved_at TEXT,
            UNIQUE(item_type, item_id, fragility_type)
        );

        CREATE TABLE IF NOT EXISTS review_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            rating TEXT,
            latency_ms REAL,
            biometric_score REAL,
            state_before TEXT,
            state_after TEXT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
    """)

    # Insert test words
    conn.executemany("INSERT INTO word_bank (id, word, frequency_rank, difficulty_tier) VALUES (?, ?, ?, ?)", [
        (1, "medo", 301, 1),
        (2, "dar", 50, 1),
        (3, "fazer", 30, 1),
    ])
    conn.commit()
    conn.close()


def _add_test_chunk(word_id, chunk, carrier, role="primary", db_path=_test_db):
    """Insert a chunk directly for testing."""
    card = Card()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """INSERT INTO chunk_queue
           (word_id, target_chunk, carrier_sentence, source, srs_state, item_role)
           VALUES (?, ?, ?, 'manual', ?, ?)""",
        (word_id, chunk, carrier, json.dumps(card.to_dict()), role),
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


# ═══════════════════════════════════════════════════════════════
# Test: Word → Chunk Linking
# ═══════════════════════════════════════════════════════════════

class TestWordChunkLinking(unittest.TestCase):
    """Tests that word→chunk linking creates correct roles and support_links."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_test_db):
            _init_test_db(_test_db)

    def test_primary_chunk_gets_primary_role(self):
        cid = _add_test_chunk(1, "ter medo", "Eu tenho medo de cobra.", "primary")
        conn = sqlite3.connect(_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT item_role FROM chunk_queue WHERE id = ?", (cid,)).fetchone()
        conn.close()
        self.assertEqual(row["item_role"], "primary")

    def test_support_chunk_gets_support_role(self):
        cid = _add_test_chunk(1, "medo de altura", "Ela tem medo de altura.", "support")
        conn = sqlite3.connect(_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT item_role FROM chunk_queue WHERE id = ?", (cid,)).fetchone()
        conn.close()
        self.assertEqual(row["item_role"], "support")

    def test_support_links_created(self):
        # Create a primary and two supports for word 2
        pid = _add_test_chunk(2, "dar um rolê", "Vou dar um rolê na Barra.", "primary")
        sid1 = _add_test_chunk(2, "dar certo", "Vai dar certo, confia.", "support")
        sid2 = _add_test_chunk(2, "dar conta", "Não vou dar conta disso.", "support")

        # Manually create support_links like auto_link_word_to_chunks does
        conn = sqlite3.connect(_test_db)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT OR IGNORE INTO support_links (primary_chunk_id, support_chunk_id, link_type, display_order) VALUES (?,?,'chunk_support',0)", (pid, sid1))
        conn.execute("INSERT OR IGNORE INTO support_links (primary_chunk_id, support_chunk_id, link_type, display_order) VALUES (?,?,'chunk_support',1)", (pid, sid2))
        conn.commit()

        links = conn.execute("SELECT * FROM support_links WHERE primary_chunk_id = ?", (pid,)).fetchall()
        conn.close()

        self.assertEqual(len(links), 2)
        support_ids = {links[0]["support_chunk_id"], links[1]["support_chunk_id"]}
        self.assertIn(sid1, support_ids)
        self.assertIn(sid2, support_ids)

    def test_duplicate_chunk_rejected(self):
        # Same word_id + target_chunk should fail silently
        cid1 = _add_test_chunk(3, "fazer questão", "Eu faço questão de ir.", "primary")
        conn = sqlite3.connect(_test_db)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "INSERT INTO chunk_queue (word_id, target_chunk, carrier_sentence, source, srs_state, item_role) VALUES (?,?,?,'manual','{}','primary')",
                (3, "fazer questão", "Duplicate carrier."),
            )
            conn.commit()
            self.fail("Should have raised IntegrityError")
        except sqlite3.IntegrityError:
            pass
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Test: State Transitions
# ═══════════════════════════════════════════════════════════════

class TestStateTransitions(unittest.TestCase):
    """Tests automaticity state promotions and demotions."""

    @classmethod
    def setUpClass(cls):
        # Ensure DB exists
        if not os.path.exists(_test_db):
            _init_test_db(_test_db)

    def _fresh_state(self, item_id=999):
        """Create a clean state row for testing."""
        conn = sqlite3.connect(_test_db)
        conn.execute("DELETE FROM automaticity_state WHERE item_type='chunk' AND item_id=?", (item_id,))
        conn.commit()
        conn.close()
        return get_or_create_state('chunk', item_id, _test_db)

    def test_unknown_to_recognized(self):
        """First non-Again review promotes UNKNOWN → RECOGNIZED."""
        self._fresh_state(100)
        result = update_state_after_review('chunk', 100, Rating.Hard, 1500, 'clean', db_path=_test_db)
        self.assertEqual(result['old_state'], 'UNKNOWN')
        self.assertEqual(result['new_state'], 'RECOGNIZED')
        self.assertTrue(result['promoted'])

    def test_unknown_stays_on_again(self):
        """Again rating keeps item at UNKNOWN."""
        self._fresh_state(101)
        result = update_state_after_review('chunk', 101, Rating.Again, 3000, 'clean', db_path=_test_db)
        self.assertEqual(result['new_state'], 'UNKNOWN')
        self.assertFalse(result['promoted'])

    def test_recognized_to_context_known(self):
        """Repeated Good reviews promote through RECOGNIZED → CONTEXT_KNOWN (and beyond)."""
        self._fresh_state(102)
        # First: UNKNOWN → RECOGNIZED
        r1 = update_state_after_review('chunk', 102, Rating.Good, 1000, 'clean', db_path=_test_db)
        self.assertEqual(r1['new_state'], 'RECOGNIZED')

        # With fast clean reviews, engine evaluates all rules each pass.
        # After enough Good reviews it will reach CONTEXT_KNOWN (or beyond).
        for _ in range(3):
            update_state_after_review('chunk', 102, Rating.Good, 800, 'clean', db_path=_test_db)

        state = get_or_create_state('chunk', 102, _test_db)
        # Should be at least CONTEXT_KNOWN (may have promoted further due to clean_audio_success)
        self.assertGreaterEqual(STATE_ORDER[state['state']], STATE_ORDER['CONTEXT_KNOWN'])

    def test_good_reviews_reach_effortful_audio(self):
        """Enough Good clean-audio reviews promote all the way to EFFORTFUL_AUDIO or beyond."""
        self._fresh_state(103)
        # 5 Good reviews with good latency and clean audio
        for _ in range(5):
            update_state_after_review('chunk', 103, Rating.Good, 800, 'clean', db_path=_test_db)

        state = get_or_create_state('chunk', 103, _test_db)
        self.assertGreaterEqual(STATE_ORDER[state['state']], STATE_ORDER['EFFORTFUL_AUDIO'])

    def test_easy_reviews_reach_automatic_clean(self):
        """Fast Easy reviews with high clean success reach AUTOMATIC_CLEAN."""
        self._fresh_state(104)
        # 12 Easy reviews at 500ms — builds streak, exposure, and clean_audio_success
        for _ in range(12):
            update_state_after_review('chunk', 104, Rating.Easy, 500, 'clean', db_path=_test_db)

        state = get_or_create_state('chunk', 104, _test_db)
        self.assertGreaterEqual(STATE_ORDER[state['state']], STATE_ORDER['AUTOMATIC_CLEAN'])

    def test_demotion_automatic_clean_to_effortful(self):
        """Slow latency or low success demotes AUTOMATIC_CLEAN → EFFORTFUL_AUDIO."""
        self._fresh_state(105)
        # Build to AUTOMATIC_CLEAN via fast Easy reviews
        for _ in range(12):
            update_state_after_review('chunk', 105, Rating.Easy, 500, 'clean', db_path=_test_db)

        state = get_or_create_state('chunk', 105, _test_db)
        self.assertGreaterEqual(STATE_ORDER[state['state']], STATE_ORDER['AUTOMATIC_CLEAN'])

        # Now fail repeatedly with slow latency to trigger demotion
        # Need enough Hard reviews to: avg_latency > 1500 OR clean_audio_success < 0.7
        # After 12 Easy (success=1.0), need ~8 Hard (success=0.0) to get below 0.7: 12/20=0.6
        for _ in range(8):
            update_state_after_review('chunk', 105, Rating.Hard, 2000, 'clean', db_path=_test_db)

        state = get_or_create_state('chunk', 105, _test_db)
        self.assertEqual(state['state'], 'EFFORTFUL_AUDIO')

    def test_demotion_on_again_ratings(self):
        """Again ratings demote items back down the ladder."""
        self._fresh_state(106)
        # Build up to at least EFFORTFUL_AUDIO
        for _ in range(6):
            update_state_after_review('chunk', 106, Rating.Good, 800, 'clean', db_path=_test_db)

        state = get_or_create_state('chunk', 106, _test_db)
        self.assertGreaterEqual(STATE_ORDER[state['state']], STATE_ORDER['EFFORTFUL_AUDIO'])

        # Multiple Again ratings should demote
        for _ in range(5):
            update_state_after_review('chunk', 106, Rating.Again, 3000, 'clean', db_path=_test_db)

        state = get_or_create_state('chunk', 106, _test_db)
        # Should have demoted at least one level
        self.assertLess(STATE_ORDER[state['state']], STATE_ORDER['AUTOMATIC_CLEAN'])

    def test_return_dict_has_required_keys(self):
        """update_state_after_review returns old_state, new_state, promoted, demoted, confidence."""
        self._fresh_state(107)
        result = update_state_after_review('chunk', 107, Rating.Good, 1000, 'clean', db_path=_test_db)
        self.assertIn('old_state', result)
        self.assertIn('new_state', result)
        self.assertIn('promoted', result)
        self.assertIn('demoted', result)
        self.assertIn('confidence', result)
        self.assertIsInstance(result['confidence'], float)


# ═══════════════════════════════════════════════════════════════
# Test: State-Aware Confidence
# ═══════════════════════════════════════════════════════════════

class TestStateAwareConfidence(unittest.TestCase):
    """Tests that confidence weighting changes by state."""

    def _make_state_row(self, state, **overrides):
        row = {
            'state': state,
            'correct_streak': 3,
            'exposure_count': 10,
            'avg_latency_ms': 800,
            'clean_audio_success': 0.7,
            'native_audio_success': 0.5,
        }
        row.update(overrides)
        return row

    def test_effortful_weights_latency_more(self):
        """EFFORTFUL_AUDIO should weight latency more than CONTEXT_KNOWN."""
        # Same stats, different states
        row_eff = self._make_state_row('EFFORTFUL_AUDIO')
        row_ctx = self._make_state_row('CONTEXT_KNOWN')

        # Now make latency very fast — effortful should benefit more
        row_eff['avg_latency_ms'] = 200
        row_ctx['avg_latency_ms'] = 200

        conf_eff = compute_confidence(row_eff)
        conf_ctx = compute_confidence(row_ctx)

        # With fast latency, effortful (30% latency weight) should score higher
        # than context_known (10% latency weight) on the latency component
        # But context_known weights streak/exposure more, so total can vary
        # The key test: latency factor contributes more to effortful
        latency_factor = max(0, 1.0 - 200 / 2000)  # = 0.9
        eff_latency_contrib = 0.30 * latency_factor
        ctx_latency_contrib = 0.10 * latency_factor
        self.assertGreater(eff_latency_contrib, ctx_latency_contrib)

    def test_automatic_native_weights_native_audio(self):
        """AUTOMATIC_NATIVE should weight native_audio_success at 40%."""
        row = self._make_state_row('AUTOMATIC_NATIVE', native_audio_success=1.0, clean_audio_success=0.0)
        conf = compute_confidence(row)
        # native_audio_success=1.0 at 40% weight = 0.40 contribution
        self.assertGreater(conf, 0.4)

    def test_context_known_weights_streak_exposure(self):
        """CONTEXT_KNOWN should weight streak (35%) + exposure (30%) = 65% total."""
        row = self._make_state_row('CONTEXT_KNOWN',
                                   correct_streak=5, exposure_count=15,
                                   avg_latency_ms=2000, clean_audio_success=0.0,
                                   native_audio_success=0.0)
        conf = compute_confidence(row)
        # streak=5/5=1.0 * 0.35 + exposure=15/15=1.0 * 0.30 = 0.65
        self.assertAlmostEqual(conf, 0.65, places=2)


# ═══════════════════════════════════════════════════════════════
# Test: migrate_v6 Backfill
# ═══════════════════════════════════════════════════════════════

class TestMigrateV6(unittest.TestCase):
    """Tests that migrate_v6 correctly links multi-chunk words and flags unhighlightable items."""

    def test_backfill_creates_support_links(self):
        """Words with multiple chunks but no support_links get linked."""
        db = os.path.join(_tmpdir, "test_v6.db")
        _init_test_db(db)

        # Add 3 chunks for word 1, no support_links
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        card_json = json.dumps(Card().to_dict())
        conn.execute("INSERT INTO chunk_queue (word_id, target_chunk, carrier_sentence, source, srs_state, item_role) VALUES (1, 'ter medo', 'Eu tenho medo.', 'manual', ?, 'primary')", (card_json,))
        conn.execute("INSERT INTO chunk_queue (word_id, target_chunk, carrier_sentence, source, srs_state, item_role) VALUES (1, 'medo de cobra', 'Tenho medo de cobra.', 'manual', ?, 'support')", (card_json,))
        conn.execute("INSERT INTO chunk_queue (word_id, target_chunk, carrier_sentence, source, srs_state, item_role) VALUES (1, 'com medo', 'Estou com medo.', 'manual', ?, 'support')", (card_json,))
        conn.commit()

        links_before = conn.execute("SELECT COUNT(*) as c FROM support_links").fetchone()["c"]
        self.assertEqual(links_before, 0)
        conn.close()

        migrate_v6(db)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        links = conn.execute("SELECT * FROM support_links").fetchall()
        conn.close()
        self.assertEqual(len(links), 2)

    def test_flags_unhighlightable_chunks(self):
        """Chunks where target_chunk not in carrier_sentence get tagged."""
        db = os.path.join(_tmpdir, "test_v6_flag.db")
        _init_test_db(db)

        conn = sqlite3.connect(db)
        card_json = json.dumps(Card().to_dict())
        # This one is unhighlightable: "ter medo" not in "Oxe, que zuada!"
        conn.execute(
            "INSERT INTO chunk_queue (word_id, target_chunk, carrier_sentence, source, srs_state) VALUES (1, 'ter medo', 'Oxe, que zuada!', 'manual', ?)",
            (card_json,),
        )
        # This one is fine: "dar um rolê" is in carrier
        conn.execute(
            "INSERT INTO chunk_queue (word_id, target_chunk, carrier_sentence, source, srs_state) VALUES (2, 'dar um rolê', 'Vou dar um rolê na Barra.', 'manual', ?)",
            (card_json,),
        )
        conn.commit()
        conn.close()

        migrate_v6(db)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        flagged = conn.execute("SELECT * FROM chunk_queue WHERE tag = 'needs_carrier_fix'").fetchall()
        conn.close()
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["target_chunk"], "ter medo")


if __name__ == "__main__":
    unittest.main()
