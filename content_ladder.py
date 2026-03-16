"""
content_ladder.py — Expanded content difficulty system for the Oxe Protocol.

Maps learner compression (percentage of known words) to a 10-level difficulty
ladder ranging from P1 (Primeiro Passo) through NATIVE_CHAOTIC.  Provides
utilities to classify stories/podcasts by difficulty, select content for
different training modes (compression, stretch, robustness), and determine
the learner's current level.

Usage:
    from content_ladder import (
        classify_content, select_content_for_mode,
        get_learner_level, classify_all_content, get_level_info,
    )
"""

import json
from srs_engine import DB_PATH, get_connection, get_unlocked_tier
from acquisition_engine import get_or_create_state, STATE_ORDER

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPANDED_LEVELS = {
    "P1":             {"label": "Primeiro Passo",     "clarity": 100, "rate_range": (80, 100),  "known_floor": 0.98, "order": 0},
    "P2":             {"label": "Primeiras Palavras",  "clarity": 100, "rate_range": (80, 110),  "known_floor": 0.97, "order": 1},
    "P3":             {"label": "Começando",           "clarity": 100, "rate_range": (90, 120),  "known_floor": 0.95, "order": 2},
    "A1":             {"label": "Tudo Tranquilo",      "clarity": 100, "rate_range": (100, 130), "known_floor": 0.95, "order": 3},
    "A2":             {"label": "Quase Lá",            "clarity": 95,  "rate_range": (110, 140), "known_floor": 0.93, "order": 4},
    "A3":             {"label": "Entendendo",          "clarity": 90,  "rate_range": (120, 150), "known_floor": 0.90, "order": 5},
    "A4":             {"label": "Fluindo",             "clarity": 85,  "rate_range": (130, 160), "known_floor": 0.88, "order": 6},
    "NATIVE_CLEAR":   {"label": "Nativo Claro",       "clarity": 80,  "rate_range": (140, 180), "known_floor": 0.80, "order": 7},
    "NATIVE_CASUAL":  {"label": "Nativo Casual",      "clarity": 60,  "rate_range": (160, 200), "known_floor": 0.70, "order": 8},
    "NATIVE_CHAOTIC": {"label": "Nativo Caótico",     "clarity": 40,  "rate_range": (180, 240), "known_floor": 0.50, "order": 9},
}

CONTENT_MODES = {
    "compression": {"target_range": (0.90, 0.95), "description": "High comprehension repetition"},
    "stretch":     {"target_range": (0.80, 0.88), "description": "Moderate challenge"},
    "robustness":  {"target_range": (0.40, 0.70), "description": "Native-speed exposure"},
}


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

def compute_compression_pct(content_words, db_path=DB_PATH):
    """Compute the percentage of content words the learner knows at automaticity.

    A word counts as "known" when its automaticity state is CONTEXT_KNOWN or
    higher in the STATE_ORDER hierarchy.

    Args:
        content_words: List of words from the content text.
        db_path: Path to the SQLite database.

    Returns:
        Float between 0.0 and 1.0 representing the known-word ratio.
        Returns 1.0 if the word list is empty.
    """
    unique_words = list(set(w.lower() for w in content_words if w.strip()))
    if not unique_words:
        return 1.0

    conn = get_connection(db_path)
    known_count = 0
    context_known_order = STATE_ORDER["CONTEXT_KNOWN"]

    for word in unique_words:
        row = conn.execute(
            "SELECT id FROM word_bank WHERE LOWER(word) = ?", (word,)
        ).fetchone()
        if row is None:
            continue
        word_id = row["id"] if isinstance(row, dict) else row[0]

        state_row = conn.execute(
            "SELECT state FROM automaticity_state WHERE item_type = 'word' AND item_id = ?",
            (word_id,),
        ).fetchone()
        if state_row is not None:
            state = state_row["state"] if isinstance(state_row, dict) else state_row[0]
            if state in STATE_ORDER and STATE_ORDER[state] >= context_known_order:
                known_count += 1

    conn.close()
    return known_count / len(unique_words)


def classify_content(content_type, content_id, db_path=DB_PATH):
    """Analyze a piece of content and assign it a difficulty level.

    Fetches the content body from story_library or podcast_library, computes
    the learner's compression percentage over its words, calculates lexical
    density, and selects the closest matching EXPANDED_LEVELS entry.  The
    result is upserted into the content_metadata table.

    Args:
        content_type: Either 'story' or 'podcast'.
        content_id: The primary key id in the corresponding library table.
        db_path: Path to the SQLite database.

    Returns:
        The assigned difficulty level key string (e.g. "A2").

    Raises:
        ValueError: If the content_type is not recognized or the content is
            not found.
    """
    conn = get_connection(db_path)

    if content_type == "story":
        row = conn.execute(
            "SELECT body FROM story_library WHERE id = ?", (content_id,)
        ).fetchone()
    elif content_type == "podcast":
        row = conn.execute(
            "SELECT body FROM podcast_library WHERE id = ?", (content_id,)
        ).fetchone()
    else:
        conn.close()
        raise ValueError(f"Unknown content_type: {content_type!r}")

    if row is None:
        conn.close()
        raise ValueError(f"{content_type} with id={content_id} not found")

    body = row["body"] if isinstance(row, dict) else row[0]
    conn.close()

    words = body.split()
    if not words:
        # Edge case: empty body defaults to easiest level
        return "P1"

    compression_pct = compute_compression_pct(words, db_path)
    unique_words = set(w.lower() for w in words)
    lexical_density = len(unique_words) / len(words)

    # Find the best matching level: closest known_floor to compression_pct
    best_level = "P1"
    best_distance = float("inf")
    for level_key, info in EXPANDED_LEVELS.items():
        distance = abs(compression_pct - info["known_floor"])
        if distance < best_distance:
            best_distance = distance
            best_level = level_key

    # Upsert into content_metadata
    conn = get_connection(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO content_metadata
           (content_type, content_id, difficulty_level, lexical_density, compression_pct)
           VALUES (?, ?, ?, ?, ?)""",
        (content_type, content_id, best_level, lexical_density, compression_pct),
    )
    conn.commit()
    conn.close()

    return best_level


def select_content_for_mode(mode, limit=5, db_path=DB_PATH):
    """Select content best suited for a given training mode.

    Modes control the target compression range:
        - compression: high comprehension repetition (0.90-0.95)
        - stretch: moderate challenge (0.80-0.88)
        - robustness: native-speed exposure (0.40-0.70)

    Already-classified content is queried first.  If fewer than *limit*
    results are found, unclassified stories and podcasts are classified on
    the fly and considered.

    Args:
        mode: One of 'compression', 'stretch', or 'robustness'.
        limit: Maximum number of results to return.
        db_path: Path to the SQLite database.

    Returns:
        List of dicts with keys: content_type, content_id, difficulty_level,
        compression_pct, title.

    Raises:
        ValueError: If mode is not a recognized CONTENT_MODES key.
    """
    if mode not in CONTENT_MODES:
        raise ValueError(f"Unknown mode: {mode!r}. Choose from {list(CONTENT_MODES.keys())}")

    lo, hi = CONTENT_MODES[mode]["target_range"]
    midpoint = (lo + hi) / 2.0

    conn = get_connection(db_path)

    # Query already-classified content within the target range
    rows = conn.execute(
        """SELECT cm.content_type, cm.content_id, cm.difficulty_level, cm.compression_pct
           FROM content_metadata cm
           WHERE cm.compression_pct BETWEEN ? AND ?
           ORDER BY ABS(cm.compression_pct - ?) ASC
           LIMIT ?""",
        (lo, hi, midpoint, limit),
    ).fetchall()

    results = []
    for r in rows:
        ct = r["content_type"] if isinstance(r, dict) else r[0]
        cid = r["content_id"] if isinstance(r, dict) else r[1]
        dl = r["difficulty_level"] if isinstance(r, dict) else r[2]
        cpct = r["compression_pct"] if isinstance(r, dict) else r[3]

        title = _fetch_title(conn, ct, cid)
        results.append({
            "content_type": ct,
            "content_id": cid,
            "difficulty_level": dl,
            "compression_pct": cpct,
            "title": title,
        })

    # If not enough results, classify unclassified content on the fly
    if len(results) < limit:
        needed = limit - len(results)
        already_classified = set()
        for r in results:
            already_classified.add((r["content_type"], r["content_id"]))

        # Gather unclassified stories
        unclassified = []
        try:
            story_rows = conn.execute(
                """SELECT s.id FROM story_library s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM content_metadata cm
                       WHERE cm.content_type = 'story' AND cm.content_id = s.id
                   )"""
            ).fetchall()
            for sr in story_rows:
                sid = sr["id"] if isinstance(sr, dict) else sr[0]
                unclassified.append(("story", sid))
        except Exception:
            pass

        # Gather unclassified podcasts
        try:
            podcast_rows = conn.execute(
                """SELECT p.id FROM podcast_library p
                   WHERE NOT EXISTS (
                       SELECT 1 FROM content_metadata cm
                       WHERE cm.content_type = 'podcast' AND cm.content_id = p.id
                   )"""
            ).fetchall()
            for pr in podcast_rows:
                pid = pr["id"] if isinstance(pr, dict) else pr[0]
                unclassified.append(("podcast", pid))
        except Exception:
            pass

        conn.close()

        # Classify each and check if it falls in range
        for ct, cid in unclassified:
            if len(results) >= limit:
                break
            if (ct, cid) in already_classified:
                continue
            try:
                level = classify_content(ct, cid, db_path)
            except ValueError:
                continue

            # Re-read the compression_pct we just stored
            c2 = get_connection(db_path)
            meta = c2.execute(
                "SELECT compression_pct FROM content_metadata WHERE content_type = ? AND content_id = ?",
                (ct, cid),
            ).fetchone()
            c2.close()

            if meta is not None:
                cpct = meta["compression_pct"] if isinstance(meta, dict) else meta[0]
                if cpct is not None and lo <= cpct <= hi:
                    c3 = get_connection(db_path)
                    title = _fetch_title(c3, ct, cid)
                    c3.close()
                    results.append({
                        "content_type": ct,
                        "content_id": cid,
                        "difficulty_level": level,
                        "compression_pct": cpct,
                        "title": title,
                    })
        return results

    conn.close()
    return results


def get_learner_level(db_path=DB_PATH):
    """Determine the learner's current content level.

    Computes an overall compression ratio by comparing words at CONTEXT_KNOWN
    or above against the total words in unlocked tiers, then maps that ratio
    to the highest EXPANDED_LEVELS entry whose known_floor does not exceed it.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        A level key string (e.g. "A2", "NATIVE_CLEAR").
    """
    conn = get_connection(db_path)
    context_known_order = STATE_ORDER["CONTEXT_KNOWN"]

    # Count items at CONTEXT_KNOWN or above
    all_states = conn.execute(
        "SELECT state FROM automaticity_state WHERE item_type = 'word'"
    ).fetchall()

    effective_known = 0
    for row in all_states:
        state = row["state"] if isinstance(row, dict) else row[0]
        if state in STATE_ORDER and STATE_ORDER[state] >= context_known_order:
            effective_known += 1

    # Get total items in unlocked tiers
    max_tier = get_unlocked_tier(db_path)
    total = conn.execute(
        "SELECT COUNT(*) FROM word_bank WHERE difficulty_tier <= ?", (max_tier,)
    ).fetchone()
    total_count = total[0]

    conn.close()

    if total_count == 0:
        return "P1"

    overall_compression = effective_known / total_count

    # Find the highest level whose known_floor does not exceed overall_compression
    best_level = "P1"
    best_order = -1
    for level_key, info in EXPANDED_LEVELS.items():
        if info["known_floor"] <= overall_compression and info["order"] > best_order:
            best_order = info["order"]
            best_level = level_key

    return best_level


def classify_all_content(db_path=DB_PATH):
    """Batch classify all stories and podcasts without content_metadata entries.

    Iterates over every row in story_library and podcast_library that does not
    yet have a corresponding content_metadata row and calls classify_content
    on each.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Dict with keys 'classified' (total), 'stories', and 'podcasts'.
    """
    conn = get_connection(db_path)
    counts = {"classified": 0, "stories": 0, "podcasts": 0}

    # Unclassified stories
    try:
        story_rows = conn.execute(
            """SELECT s.id FROM story_library s
               WHERE NOT EXISTS (
                   SELECT 1 FROM content_metadata cm
                   WHERE cm.content_type = 'story' AND cm.content_id = s.id
               )"""
        ).fetchall()
    except Exception:
        story_rows = []

    # Unclassified podcasts
    try:
        podcast_rows = conn.execute(
            """SELECT p.id FROM podcast_library p
               WHERE NOT EXISTS (
                   SELECT 1 FROM content_metadata cm
                   WHERE cm.content_type = 'podcast' AND cm.content_id = p.id
               )"""
        ).fetchall()
    except Exception:
        podcast_rows = []

    conn.close()

    for row in story_rows:
        sid = row["id"] if isinstance(row, dict) else row[0]
        try:
            classify_content("story", sid, db_path)
            counts["stories"] += 1
            counts["classified"] += 1
        except ValueError:
            continue

    for row in podcast_rows:
        pid = row["id"] if isinstance(row, dict) else row[0]
        try:
            classify_content("podcast", pid, db_path)
            counts["podcasts"] += 1
            counts["classified"] += 1
        except ValueError:
            continue

    return counts


def get_level_info(level=None):
    """Return level metadata from EXPANDED_LEVELS.

    Args:
        level: An optional level key (e.g. "A2").  If provided, returns the
            single dict for that level.  If None, returns all levels as a
            list of dicts with the level key added under "key".

    Returns:
        A dict for a single level, or a list of dicts for all levels.

    Raises:
        KeyError: If a specific level is requested but not found.
    """
    if level is not None:
        if level not in EXPANDED_LEVELS:
            raise KeyError(f"Unknown level: {level!r}. Valid: {list(EXPANDED_LEVELS.keys())}")
        return dict(EXPANDED_LEVELS[level])

    return [
        {"key": k, **v}
        for k, v in sorted(EXPANDED_LEVELS.items(), key=lambda x: x[1]["order"])
    ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_title(conn, content_type, content_id):
    """Fetch the title for a story or podcast from its library table.

    Args:
        conn: An open SQLite connection.
        content_type: 'story' or 'podcast'.
        content_id: The row id.

    Returns:
        The title string, or None if not found.
    """
    if content_type == "story":
        row = conn.execute(
            "SELECT title FROM story_library WHERE id = ?", (content_id,)
        ).fetchone()
    elif content_type == "podcast":
        row = conn.execute(
            "SELECT title FROM podcast_library WHERE id = ?", (content_id,)
        ).fetchone()
    else:
        return None

    if row is None:
        return None
    return row["title"] if isinstance(row, dict) else row[0]
