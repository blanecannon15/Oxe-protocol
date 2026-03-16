"""
acquisition_engine.py — Core automaticity state model and fragility detector for the Oxe Protocol.

Tracks each word/chunk through 7 acquisition states from UNKNOWN to AVAILABLE_OUTPUT.
Detects 5 types of fragility (familiar_but_fragile, known_but_slow, text_only,
clean_audio_only, blocked_by_prosody) and maintains a prioritized remediation queue.

Called after every review via update_state_after_review() to promote, demote, or hold
items based on confidence, latency, audio success rates, and biometric scores.
"""

import json
from datetime import datetime, timezone

from fsrs import Rating

from srs_engine import DB_PATH, get_connection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATES = [
    'UNKNOWN', 'RECOGNIZED', 'CONTEXT_KNOWN',
    'EFFORTFUL_AUDIO', 'AUTOMATIC_CLEAN',
    'AUTOMATIC_NATIVE', 'AVAILABLE_OUTPUT'
]

STATE_ORDER = {s: i for i, s in enumerate(STATES)}

FRAGILITY_TYPES = [
    'familiar_but_fragile', 'known_but_slow', 'text_only',
    'clean_audio_only', 'blocked_by_prosody'
]


# ---------------------------------------------------------------------------
# State CRUD
# ---------------------------------------------------------------------------

def get_or_create_state(item_type, item_id, db_path=DB_PATH):
    """Fetch the automaticity_state row for a word or chunk.

    If no row exists, INSERT one with state='UNKNOWN' and all defaults,
    then return it as a plain dict.
    """
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM automaticity_state WHERE item_type = ? AND item_id = ?",
        (item_type, item_id),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO automaticity_state (item_type, item_id, state, confidence,
               exposure_count, correct_streak, latency_history, avg_latency_ms,
               latency_trend, clean_audio_success, native_audio_success,
               clean_attempts, native_attempts, output_success, output_attempts)
               VALUES (?, ?, 'UNKNOWN', 0.0, 0, 0, '[]', NULL, 0.0, 0.0, 0.0, 0, 0, 0.0, 0)""",
            (item_type, item_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM automaticity_state WHERE item_type = ? AND item_id = ?",
            (item_type, item_id),
        ).fetchone()
    result = dict(row)
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Confidence & Latency
# ---------------------------------------------------------------------------

def compute_confidence(state_row):
    """Compute a 0.0-1.0 confidence score from the state row fields.

    Weights:
        30% correct_streak (capped at 5)
        25% exposure_count (capped at 15)
        20% latency (lower is better, capped at 2000ms)
        15% clean_audio_success rate
        10% native_audio_success rate
    """
    correct_streak = state_row.get('correct_streak', 0)
    exposure_count = state_row.get('exposure_count', 0)
    avg_latency_ms = state_row.get('avg_latency_ms') or 2000
    clean_audio_success = state_row.get('clean_audio_success', 0.0)
    native_audio_success = state_row.get('native_audio_success', 0.0)

    confidence = (
        0.30 * min(correct_streak / 5, 1.0)
        + 0.25 * min(exposure_count / 15, 1.0)
        + 0.20 * max(0, 1.0 - avg_latency_ms / 2000)
        + 0.15 * clean_audio_success
        + 0.10 * native_audio_success
    )
    return confidence


def compute_latency_trend(latency_history):
    """Compute the linear regression slope over the latency history.

    Uses the last 5 data points. Negative slope means improving (getting faster).
    Returns 0.0 if fewer than 2 data points.
    """
    data = latency_history[-5:] if len(latency_history) > 5 else latency_history
    n = len(data)
    if n < 2:
        return 0.0

    # Simple least-squares: slope = (n*sum(xy) - sum(x)*sum(y)) / (n*sum(x^2) - sum(x)^2)
    sum_x = 0.0
    sum_y = 0.0
    sum_xy = 0.0
    sum_x2 = 0.0
    for i, y in enumerate(data):
        x = float(i)
        sum_x += x
        sum_y += y
        sum_xy += x * y
        sum_x2 += x * x

    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


# ---------------------------------------------------------------------------
# Core review handler
# ---------------------------------------------------------------------------

def update_state_after_review(item_type, item_id, rating, latency_ms,
                              audio_type, biometric_score=None, db_path=DB_PATH):
    """Update automaticity state after a review. This is the CORE function.

    Args:
        item_type: 'word' or 'chunk'
        item_id: the word_bank.id or chunk_queue.id
        rating: an fsrs.Rating value (1=Again, 2=Hard, 3=Good, 4=Easy)
        latency_ms: response time in milliseconds
        audio_type: 'clean', 'native', 'output', or 'text'
        biometric_score: optional 0-100 nativeness score
        db_path: path to the SQLite database

    Returns:
        dict with keys: old_state, new_state, promoted, demoted, confidence
    """
    state_row = get_or_create_state(item_type, item_id, db_path)
    old_state = state_row['state']
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # 1. Increment exposure_count
    state_row['exposure_count'] += 1

    # 2. Update correct_streak
    if rating.value >= 3:  # Good or Easy
        state_row['correct_streak'] += 1
    else:
        state_row['correct_streak'] = 0

    # 3-4. Append latency_ms to history, keep last 10
    history = json.loads(state_row['latency_history']) if isinstance(state_row['latency_history'], str) else state_row['latency_history']
    history.append(latency_ms)
    history = history[-10:]
    state_row['latency_history'] = json.dumps(history)

    # 5. Recalculate avg_latency_ms
    state_row['avg_latency_ms'] = sum(history) / len(history)

    # 6. Recalculate latency_trend
    state_row['latency_trend'] = compute_latency_trend(history)

    # 7. Update success rates based on audio_type
    success = 1.0 if rating.value >= 3 else 0.0

    if audio_type == 'clean':
        old_attempts = state_row['clean_attempts']
        state_row['clean_attempts'] += 1
        state_row['clean_audio_success'] = (
            (state_row['clean_audio_success'] * old_attempts + success) / state_row['clean_attempts']
        )
    elif audio_type == 'native':
        old_attempts = state_row['native_attempts']
        state_row['native_attempts'] += 1
        state_row['native_audio_success'] = (
            (state_row['native_audio_success'] * old_attempts + success) / state_row['native_attempts']
        )
    elif audio_type == 'output':
        old_attempts = state_row['output_attempts']
        state_row['output_attempts'] += 1
        state_row['output_success'] = (
            (state_row['output_success'] * old_attempts + success) / state_row['output_attempts']
        )

    # 8. Update biometric
    if biometric_score is not None:
        state_row['last_biometric'] = biometric_score

    # 9. Recompute confidence
    state_row['confidence'] = compute_confidence(state_row)

    # 10. Check PROMOTION rules (in order, only first match)
    new_state = old_state
    promoted = False
    current_order = STATE_ORDER[old_state]

    if old_state == 'UNKNOWN' and rating.value >= 2 and state_row['exposure_count'] >= 1:
        new_state = 'RECOGNIZED'
        promoted = True
    elif old_state == 'RECOGNIZED' and state_row['correct_streak'] >= 3 and state_row['confidence'] > 0.4:
        new_state = 'CONTEXT_KNOWN'
        promoted = True
    elif old_state == 'CONTEXT_KNOWN' and state_row['clean_audio_success'] > 0.5 and state_row['clean_attempts'] >= 2:
        new_state = 'EFFORTFUL_AUDIO'
        promoted = True
    elif (old_state == 'EFFORTFUL_AUDIO'
          and state_row['correct_streak'] >= 5
          and (state_row['avg_latency_ms'] or 2000) < 1000
          and state_row['clean_audio_success'] > 0.85):
        new_state = 'AUTOMATIC_CLEAN'
        promoted = True
    elif (old_state == 'AUTOMATIC_CLEAN'
          and state_row['native_audio_success'] > 0.70
          and state_row['native_attempts'] >= 3
          and (state_row.get('last_biometric') or 0) >= 65):
        new_state = 'AUTOMATIC_NATIVE'
        promoted = True
    elif (old_state == 'AUTOMATIC_NATIVE'
          and state_row['output_success'] > 0.60
          and state_row['output_attempts'] >= 3
          and (state_row.get('last_biometric') or 0) >= 85
          and (state_row['avg_latency_ms'] or 2000) < 800):
        new_state = 'AVAILABLE_OUTPUT'
        promoted = True

    # 11. Check DEMOTION rules (only if no promotion happened)
    demoted = False
    if not promoted:
        if (old_state == 'AVAILABLE_OUTPUT'
                and state_row['output_success'] < 0.6
                and state_row['output_attempts'] >= 5):
            new_state = 'AUTOMATIC_NATIVE'
            demoted = True
        elif (old_state == 'AUTOMATIC_NATIVE'
              and (state_row['native_audio_success'] < 0.5
                   or (state_row.get('last_biometric') or 100) < 50)):
            new_state = 'AUTOMATIC_CLEAN'
            demoted = True
        elif (old_state == 'AUTOMATIC_CLEAN'
              and ((state_row['avg_latency_ms'] or 0) > 1500
                   or state_row['clean_audio_success'] < 0.7)):
            new_state = 'EFFORTFUL_AUDIO'
            demoted = True
        elif (old_state == 'EFFORTFUL_AUDIO'
              and state_row['correct_streak'] == 0
              and rating.value == 1
              and state_row['exposure_count'] >= 3):
            new_state = 'CONTEXT_KNOWN'
            demoted = True
        elif (old_state == 'CONTEXT_KNOWN'
              and state_row['exposure_count'] >= 5
              and state_row['confidence'] < 0.3):
            new_state = 'RECOGNIZED'
            demoted = True

    # 12-13. Update timestamps
    state_row['last_reviewed'] = now
    if new_state != old_state:
        state_row['last_state_change'] = now
    state_row['state'] = new_state

    # 14. Write all changes back to DB
    conn = get_connection(db_path)
    conn.execute(
        """UPDATE automaticity_state
           SET state = ?, confidence = ?, exposure_count = ?, correct_streak = ?,
               latency_history = ?, avg_latency_ms = ?, latency_trend = ?,
               clean_audio_success = ?, native_audio_success = ?,
               clean_attempts = ?, native_attempts = ?,
               output_success = ?, output_attempts = ?,
               last_biometric = ?, last_state_change = ?, last_reviewed = ?
           WHERE item_type = ? AND item_id = ?""",
        (
            state_row['state'], state_row['confidence'],
            state_row['exposure_count'], state_row['correct_streak'],
            state_row['latency_history'], state_row['avg_latency_ms'],
            state_row['latency_trend'],
            state_row['clean_audio_success'], state_row['native_audio_success'],
            state_row['clean_attempts'], state_row['native_attempts'],
            state_row['output_success'], state_row['output_attempts'],
            state_row.get('last_biometric'), state_row.get('last_state_change'),
            state_row['last_reviewed'],
            item_type, item_id,
        ),
    )
    conn.commit()
    conn.close()

    # 15. Return result
    return {
        'old_state': old_state,
        'new_state': new_state,
        'promoted': promoted,
        'demoted': demoted,
        'confidence': state_row['confidence'],
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_items_in_state(state, item_type=None, limit=100, db_path=DB_PATH):
    """Query automaticity_state rows matching a given state.

    Args:
        state: one of STATES (e.g. 'AUTOMATIC_CLEAN')
        item_type: optional filter for 'word' or 'chunk'
        limit: max rows to return (default 100)
        db_path: path to the SQLite database

    Returns:
        List of dicts representing matching rows.
    """
    conn = get_connection(db_path)
    if item_type is not None:
        rows = conn.execute(
            "SELECT * FROM automaticity_state WHERE state = ? AND item_type = ? LIMIT ?",
            (state, item_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM automaticity_state WHERE state = ? LIMIT ?",
            (state, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_state_distribution(db_path=DB_PATH):
    """Return item counts by acquisition state.

    Items not yet in the automaticity_state table are counted as UNKNOWN.

    Returns:
        dict mapping each state name to its count, e.g.
        {"UNKNOWN": 1200, "RECOGNIZED": 50, ...}
    """
    conn = get_connection(db_path)

    # Count rows per state in automaticity_state
    rows = conn.execute(
        "SELECT state, COUNT(*) as cnt FROM automaticity_state GROUP BY state"
    ).fetchall()
    dist = {s: 0 for s in STATES}
    tracked_total = 0
    for r in rows:
        dist[r['state']] = r['cnt']
        tracked_total += r['cnt']

    # Total possible items = words + chunks
    total_words = conn.execute("SELECT COUNT(*) FROM word_bank").fetchone()[0]
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunk_queue").fetchone()[0]
    conn.close()

    total_items = total_words + total_chunks
    dist['UNKNOWN'] += max(total_items - tracked_total, 0)

    return dist


# ---------------------------------------------------------------------------
# Fragility Detection
# ---------------------------------------------------------------------------

def detect_fragility(item_type, item_id, db_path=DB_PATH):
    """Run all 5 fragility detectors on an item.

    Returns a list of dicts, one per triggered fragility:
        [{"fragility_type": "...", "fragility_score": float}, ...]

    Only includes fragilities whose conditions are met.
    """
    state_row = get_or_create_state(item_type, item_id, db_path)
    results = []

    exposure_count = state_row.get('exposure_count', 0)
    correct_streak = state_row.get('correct_streak', 0)
    state = state_row.get('state', 'UNKNOWN')
    clean_audio_success = state_row.get('clean_audio_success', 0.0)
    native_audio_success = state_row.get('native_audio_success', 0.0)
    avg_latency_ms = state_row.get('avg_latency_ms') or 0
    confidence = state_row.get('confidence', 0.0)
    clean_attempts = state_row.get('clean_attempts', 0)
    native_attempts = state_row.get('native_attempts', 0)
    output_attempts = state_row.get('output_attempts', 0)
    last_biometric = state_row.get('last_biometric')

    # 1. familiar_but_fragile
    if exposure_count >= 8 and correct_streak < 2 and state != 'UNKNOWN':
        score = 100 * min(exposure_count / 20, 1.0) * max(1 - correct_streak / 5, 0)
        results.append({'fragility_type': 'familiar_but_fragile', 'fragility_score': score})

    # 2. known_but_slow
    if clean_audio_success > 0.7 and avg_latency_ms > 1200:
        score = 100 * min(avg_latency_ms / 2000, 1.0) * clean_audio_success
        results.append({'fragility_type': 'known_but_slow', 'fragility_score': score})

    # 3. text_only
    if (STATE_ORDER.get(state, 0) >= STATE_ORDER['CONTEXT_KNOWN']
            and clean_audio_success < 0.4
            and exposure_count >= 5):
        score = 100 * max(1 - clean_audio_success, 0) * confidence
        results.append({'fragility_type': 'text_only', 'fragility_score': score})

    # 4. clean_audio_only
    if (clean_audio_success > 0.7
            and native_audio_success < 0.4
            and native_attempts >= 3):
        score = 100 * (clean_audio_success - native_audio_success) * min(native_attempts / 3, 1.0)
        results.append({'fragility_type': 'clean_audio_only', 'fragility_score': score})

    # 5. blocked_by_prosody
    if (last_biometric or 100) < 65 and output_attempts >= 2:
        score = 100 * max(1 - (last_biometric or 100) / 100, 0)
        results.append({'fragility_type': 'blocked_by_prosody', 'fragility_score': score})

    return results


def run_fragility_scan(db_path=DB_PATH):
    """Scan all items in states CONTEXT_KNOWN through AUTOMATIC_NATIVE for fragilities.

    For each item, calls detect_fragility and upserts results into the fragile_items table
    using INSERT OR REPLACE on the UNIQUE(item_type, item_id, fragility_type) constraint.

    Returns:
        dict with keys: scanned (int), detected (int),
        by_type (dict mapping fragility type to count)
    """
    target_states = [
        'CONTEXT_KNOWN', 'EFFORTFUL_AUDIO', 'AUTOMATIC_CLEAN', 'AUTOMATIC_NATIVE'
    ]
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT item_type, item_id FROM automaticity_state WHERE state IN (?, ?, ?, ?)",
        target_states,
    ).fetchall()
    conn.close()

    scanned = 0
    detected = 0
    by_type = {ft: 0 for ft in FRAGILITY_TYPES}

    for row in rows:
        scanned += 1
        fragilities = detect_fragility(row['item_type'], row['item_id'], db_path)
        if fragilities:
            conn = get_connection(db_path)
            for f in fragilities:
                detected += 1
                by_type[f['fragility_type']] += 1
                conn.execute(
                    """INSERT OR REPLACE INTO fragile_items
                       (item_type, item_id, fragility_type, fragility_score, detected_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        row['item_type'], row['item_id'],
                        f['fragility_type'], f['fragility_score'],
                        datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    ),
                )
            conn.commit()
            conn.close()

    return {'scanned': scanned, 'detected': detected, 'by_type': by_type}


def get_fragile_queue(fragility_type, limit=20, db_path=DB_PATH):
    """Get the highest-priority unresolved fragile items for a given type.

    Args:
        fragility_type: one of FRAGILITY_TYPES
        limit: max items to return (default 20)
        db_path: path to the SQLite database

    Returns:
        List of dicts ordered by fragility_score descending.
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT * FROM fragile_items
           WHERE fragility_type = ? AND resolved_at IS NULL
           ORDER BY fragility_score DESC
           LIMIT ?""",
        (fragility_type, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_fragile_summary(db_path=DB_PATH):
    """Return counts of unresolved fragile items grouped by type.

    Returns:
        dict mapping fragility type to count, e.g.
        {"familiar_but_fragile": 12, "known_but_slow": 5, ...}
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT fragility_type, COUNT(*) as cnt
           FROM fragile_items
           WHERE resolved_at IS NULL
           GROUP BY fragility_type"""
    ).fetchall()
    conn.close()
    summary = {ft: 0 for ft in FRAGILITY_TYPES}
    for r in rows:
        summary[r['fragility_type']] = r['cnt']
    return summary


def resolve_fragility(item_type, item_id, fragility_type, db_path=DB_PATH):
    """Mark a fragility as resolved for a given item.

    Sets resolved_at to the current UTC timestamp on the matching
    unresolved fragile_items row.

    Args:
        item_type: 'word' or 'chunk'
        item_id: the word_bank.id or chunk_queue.id
        fragility_type: one of FRAGILITY_TYPES
        db_path: path to the SQLite database
    """
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = get_connection(db_path)
    conn.execute(
        """UPDATE fragile_items
           SET resolved_at = ?
           WHERE item_type = ? AND item_id = ? AND fragility_type = ? AND resolved_at IS NULL""",
        (now, item_type, item_id, fragility_type),
    )
    conn.commit()
    conn.close()
