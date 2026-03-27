"""
audio_audit.py — Audio coverage scanner and queue manager.

Scans word_bank and chunk_queue, detects missing audio per accent,
populates audio_coverage table, and provides coverage metrics.
"""

import os
from pathlib import Path

from srs_engine import DB_PATH, get_connection
from voice_profiles import get_profiles, generate_tts_for_accent

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"


# ── Scanner ──────────────────────────────────────────────────────────

def scan_word_coverage(db_path=DB_PATH):
    """Scan word_bank and insert missing audio_coverage rows for each accent."""
    conn = get_connection(db_path)
    profiles = conn.execute("SELECT accent FROM voice_profiles").fetchall()
    accents = [r["accent"] for r in profiles]

    words = conn.execute("SELECT id, word FROM word_bank").fetchall()
    inserted = 0
    for w in words:
        for accent in accents:
            existing = conn.execute(
                "SELECT id FROM audio_coverage WHERE item_type='word' AND item_id=? AND accent=?",
                (w["id"], accent),
            ).fetchone()
            if existing:
                continue

            # Check if audio already exists on disk (word-based filename convention)
            status = "missing"
            audio_path = None
            candidate = AUDIO_DIR / f"{w['word']}.mp3"
            if candidate.exists() and candidate.stat().st_size > 100 and accent == "paulista":
                status = "cached"
                audio_path = f"{w['word']}.mp3"

            conn.execute(
                """INSERT OR IGNORE INTO audio_coverage
                   (item_type, item_id, accent, status, audio_path)
                   VALUES ('word', ?, ?, ?, ?)""",
                (w["id"], accent, status, audio_path),
            )
            inserted += 1

    conn.commit()
    conn.close()
    return inserted


def scan_chunk_coverage(db_path=DB_PATH):
    """Scan chunk_queue and insert missing audio_coverage rows for each accent."""
    conn = get_connection(db_path)
    profiles = conn.execute("SELECT accent FROM voice_profiles").fetchall()
    accents = [r["accent"] for r in profiles]

    # chunk_queue may not exist yet
    try:
        chunks = conn.execute("SELECT id, golden_audio_path FROM chunk_queue").fetchall()
    except Exception:
        conn.close()
        return 0

    inserted = 0
    for c in chunks:
        for accent in accents:
            existing = conn.execute(
                "SELECT id FROM audio_coverage WHERE item_type='chunk' AND item_id=? AND accent=?",
                (c["id"], accent),
            ).fetchone()
            if existing:
                continue

            status = "missing"
            audio_path = None
            af = c["golden_audio_path"] if "golden_audio_path" in c.keys() else None
            if af and accent == "paulista":
                fpath = AUDIO_DIR / af
                if fpath.exists() and fpath.stat().st_size > 100:
                    status = "cached"
                    audio_path = af

            conn.execute(
                """INSERT OR IGNORE INTO audio_coverage
                   (item_type, item_id, accent, status, audio_path)
                   VALUES ('chunk', ?, ?, ?, ?)""",
                (c["id"], accent, status, audio_path),
            )
            inserted += 1

    conn.commit()
    conn.close()
    return inserted


def full_scan(db_path=DB_PATH):
    """Run full audio coverage scan for words and chunks."""
    w = scan_word_coverage(db_path)
    c = scan_chunk_coverage(db_path)
    return {"words_scanned": w, "chunks_scanned": c, "total_rows_added": w + c}


# ── Coverage Metrics ─────────────────────────────────────────────────

def get_coverage_summary(db_path=DB_PATH):
    """Return coverage stats by accent and item type."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT accent, item_type, status, COUNT(*) as cnt
        FROM audio_coverage
        GROUP BY accent, item_type, status
    """).fetchall()
    conn.close()

    summary = {}
    for r in rows:
        key = f"{r['accent']}_{r['item_type']}"
        if key not in summary:
            summary[key] = {"accent": r["accent"], "item_type": r["item_type"],
                            "total": 0, "cached": 0, "missing": 0, "queued": 0,
                            "generating": 0, "failed": 0, "native": 0}
        summary[key]["total"] += r["cnt"]
        summary[key][r["status"]] = r["cnt"]

    results = list(summary.values())
    for r in results:
        r["coverage_pct"] = round(
            100.0 * (r["cached"] + r["native"]) / max(r["total"], 1), 1
        )
    return results


def get_missing_queue(accent=None, item_type="word", limit=50, db_path=DB_PATH):
    """Return items missing audio, prioritized by frequency rank."""
    conn = get_connection(db_path)

    if item_type == "word":
        sql = """
            SELECT ac.id as coverage_id, ac.item_id, ac.accent,
                   wb.word, wb.frequency_rank
            FROM audio_coverage ac
            JOIN word_bank wb ON wb.id = ac.item_id
            WHERE ac.status = 'missing' AND ac.item_type = 'word'
        """
        params = []
        if accent:
            sql += " AND ac.accent = ?"
            params.append(accent)
        sql += " ORDER BY wb.frequency_rank ASC LIMIT ?"
        params.append(limit)
    else:
        sql = """
            SELECT ac.id as coverage_id, ac.item_id, ac.accent
            FROM audio_coverage ac
            WHERE ac.status = 'missing' AND ac.item_type = 'chunk'
        """
        params = []
        if accent:
            sql += " AND ac.accent = ?"
            params.append(accent)
        sql += " ORDER BY ac.item_id ASC LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Generation Queue ─────────────────────────────────────────────────

def queue_generation(coverage_ids, db_path=DB_PATH):
    """Mark coverage rows as 'queued' for generation."""
    conn = get_connection(db_path)
    for cid in coverage_ids:
        conn.execute(
            "UPDATE audio_coverage SET status='queued' WHERE id=? AND status='missing'",
            (cid,),
        )
    conn.commit()
    conn.close()
    return len(coverage_ids)


def generate_next_batch(batch_size=5, db_path=DB_PATH):
    """Generate TTS for the next batch of queued items. Returns count generated."""
    conn = get_connection(db_path)
    queued = conn.execute("""
        SELECT ac.id, ac.item_type, ac.item_id, ac.accent
        FROM audio_coverage ac
        WHERE ac.status = 'queued'
        ORDER BY ac.id ASC
        LIMIT ?
    """, (batch_size,)).fetchall()

    generated = 0
    for row in queued:
        conn.execute(
            "UPDATE audio_coverage SET status='generating' WHERE id=?",
            (row["id"],),
        )
        conn.commit()

        # Get the text to speak
        if row["item_type"] == "word":
            item = conn.execute(
                "SELECT word FROM word_bank WHERE id=?", (row["item_id"],)
            ).fetchone()
            text = item["word"] if item else None
        else:
            item = conn.execute(
                "SELECT target_chunk FROM chunk_queue WHERE id=?", (row["item_id"],)
            ).fetchone()
            text = item["target_chunk"] if item else None

        if not text:
            conn.execute(
                "UPDATE audio_coverage SET status='failed' WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            continue

        fname = generate_tts_for_accent(text, accent=row["accent"], db_path=db_path)
        if fname:
            conn.execute(
                """UPDATE audio_coverage
                   SET status='cached', audio_path=?,
                       updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                   WHERE id=?""",
                (fname, row["id"]),
            )
            generated += 1
        else:
            conn.execute(
                "UPDATE audio_coverage SET status='failed' WHERE id=?",
                (row["id"],),
            )
        conn.commit()

    conn.close()
    return generated
