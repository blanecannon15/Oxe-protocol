"""
content_router.py — Smart Content Routing engine for the Oxe Protocol.

Finds stories/podcasts containing chunks the learner recently drilled
and queues them for re-encounter within 24 hours.

Usage:
    from content_router import (
        find_content_for_chunks, get_recently_drilled_chunks,
        get_reencounter_queue, log_reencounter, get_reencounter_stats,
    )
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from srs_engine import DB_PATH, get_connection


# ---------------------------------------------------------------------------
# 1. Find content containing given chunks
# ---------------------------------------------------------------------------

def find_content_for_chunks(chunk_texts, content_type=None, limit=5, db_path=DB_PATH):
    # type: (List[str], Optional[str], int, object) -> List[Dict]
    """Find stories/podcasts whose body contains any of the given chunk texts.

    Scores each piece of content by number of matching chunks, with a bonus
    for content whose compression_pct is closer to the learner's level.

    Args:
        chunk_texts: List of chunk text strings to search for.
        content_type: Optional filter — 'story' or 'podcast'.
        limit: Maximum results to return.
        db_path: Path to the SQLite database.

    Returns:
        Sorted list of dicts with keys: content_type, content_id, title,
        matching_chunks, match_count, difficulty_level.
    """
    if not chunk_texts:
        return []

    conn = get_connection(db_path)
    results = []  # type: List[Dict]

    # Build search targets: (content_type, table, text_column)
    targets = []  # type: List[tuple]
    if content_type is None or content_type == "story":
        targets.append(("story", "story_library", "body"))
    if content_type is None or content_type == "podcast":
        targets.append(("podcast", "podcast_library", "body"))

    for ct, table, col in targets:
        try:
            rows = conn.execute(
                "SELECT id, title, {} FROM {}".format(col, table)
            ).fetchall()
        except Exception:
            continue

        for row in rows:
            rid = row["id"]
            title = row["title"]
            body = row[col] or ""
            body_lower = body.lower()

            matching = []  # type: List[str]
            for chunk in chunk_texts:
                if chunk.lower() in body_lower:
                    matching.append(chunk)

            if not matching:
                continue

            # Look up difficulty from content_metadata
            meta = conn.execute(
                "SELECT difficulty_level, compression_pct FROM content_metadata "
                "WHERE content_type = ? AND content_id = ?",
                (ct, rid),
            ).fetchone()

            difficulty_level = ""
            compression_pct = None  # type: Optional[float]
            if meta:
                difficulty_level = meta["difficulty_level"] or ""
                compression_pct = meta["compression_pct"]

            # Score: match count is primary; compression proximity is a tiebreaker
            # Ideal compression is ~0.90 (high comprehension). Closer = better.
            proximity_bonus = 0.0
            if compression_pct is not None:
                proximity_bonus = 1.0 - abs(0.90 - compression_pct)

            score = len(matching) + proximity_bonus

            results.append({
                "content_type": ct,
                "content_id": rid,
                "title": title,
                "matching_chunks": matching,
                "match_count": len(matching),
                "difficulty_level": difficulty_level,
                "_score": score,
            })

    conn.close()

    # Sort by score descending, then by match_count descending
    results.sort(key=lambda r: (r["_score"], r["match_count"]), reverse=True)

    # Strip internal score key and limit
    for r in results:
        r.pop("_score", None)

    return results[:limit]


# ---------------------------------------------------------------------------
# 2. Get recently drilled chunks
# ---------------------------------------------------------------------------

def get_recently_drilled_chunks(hours=24, db_path=DB_PATH):
    # type: (int, object) -> List[str]
    """Get chunk texts that were drilled in the last N hours.

    Checks chunk_queue.last_reviewed and session_events for drill completions.

    Args:
        hours: Lookback window in hours (default 24).
        db_path: Path to the SQLite database.

    Returns:
        Deduplicated list of chunk text strings.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = get_connection(db_path)

    chunks = set()  # type: set

    # Source 1: chunk_queue items reviewed recently
    try:
        rows = conn.execute(
            "SELECT target_chunk FROM chunk_queue WHERE last_reviewed >= ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            chunks.add(row["target_chunk"])
    except Exception:
        pass

    # Source 2: session_events with drill-related event types
    try:
        rows = conn.execute(
            """SELECT event_data FROM session_events
               WHERE timestamp >= ?
                 AND event_type IN ('drill_complete', 'chunk_review', 'reencounter')""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["event_data"])
                # drill_complete / chunk_review may store chunk text
                if "target_chunk" in data:
                    chunks.add(data["target_chunk"])
                if "chunks" in data and isinstance(data["chunks"], list):
                    for c in data["chunks"]:
                        chunks.add(c)
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        pass

    conn.close()
    return list(chunks)


# ---------------------------------------------------------------------------
# 3. Main re-encounter queue
# ---------------------------------------------------------------------------

def get_reencounter_queue(limit=5, db_path=DB_PATH):
    # type: (int, object) -> List[Dict]
    """Build an ordered list of content recommendations for re-encounter.

    1. Gets recently drilled chunks (last 24 hours).
    2. Finds content containing those chunks.
    3. Filters out content already consumed today.
    4. Prioritizes content with the most matching recently-drilled chunks.

    Args:
        limit: Maximum number of recommendations.
        db_path: Path to the SQLite database.

    Returns:
        List of dicts: content_type, content_id, title, matching_chunks,
        match_count, difficulty_level.
    """
    recent_chunks = get_recently_drilled_chunks(hours=24, db_path=db_path)
    if not recent_chunks:
        return []

    # Get more candidates than needed so we can filter
    candidates = find_content_for_chunks(
        recent_chunks, limit=limit * 3, db_path=db_path
    )

    if not candidates:
        return []

    # Filter out content already consumed today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection(db_path)

    consumed_today = set()  # type: set
    # Check session_events for reencounter logs today
    try:
        rows = conn.execute(
            """SELECT event_data FROM session_events
               WHERE date = ? AND event_type = 'reencounter'""",
            (today,),
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["event_data"])
                ct = data.get("content_type", "")
                cid = data.get("content_id", 0)
                consumed_today.add((ct, cid))
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        pass

    # Also check story play history (times_played updated today via last_played)
    try:
        rows = conn.execute(
            "SELECT id FROM story_library WHERE last_played >= ?",
            (today,),
        ).fetchall()
        for row in rows:
            consumed_today.add(("story", row["id"]))
    except Exception:
        pass

    # Check podcast play history
    try:
        rows = conn.execute(
            "SELECT id FROM podcast_library WHERE last_played >= ?",
            (today,),
        ).fetchall()
        for row in rows:
            consumed_today.add(("podcast", row["id"]))
    except Exception:
        pass

    conn.close()

    # Filter and return
    filtered = [
        c for c in candidates
        if (c["content_type"], c["content_id"]) not in consumed_today
    ]

    return filtered[:limit]


# ---------------------------------------------------------------------------
# 4. Log a re-encounter event
# ---------------------------------------------------------------------------

def log_reencounter(content_type, content_id, chunks_encountered, db_path=DB_PATH):
    # type: (str, int, List[str], object) -> int
    """Log that the learner consumed content containing specific chunks.

    Inserts a 'reencounter' event into session_events.

    Args:
        content_type: 'story' or 'podcast'.
        content_id: The content row ID.
        chunks_encountered: List of chunk text strings found in the content.
        db_path: Path to the SQLite database.

    Returns:
        The session_events row ID.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    event_data = json.dumps({
        "content_type": content_type,
        "content_id": content_id,
        "chunks": chunks_encountered,
    })

    conn = get_connection(db_path)
    cur = conn.execute(
        """INSERT INTO session_events (date, event_type, event_data)
           VALUES (?, 'reencounter', ?)""",
        (today, event_data),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


# ---------------------------------------------------------------------------
# 5. Re-encounter statistics
# ---------------------------------------------------------------------------

def get_reencounter_stats(days=7, db_path=DB_PATH):
    # type: (int, object) -> Dict
    """Return re-encounter statistics over the last N days.

    Args:
        days: Number of days to look back (default 7).
        db_path: Path to the SQLite database.

    Returns:
        Dict with keys: total_reencounters, unique_chunks, unique_content,
        by_day (list of per-day summaries), chunks_detail (list of chunk texts
        with encounter counts).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_connection(db_path)

    try:
        rows = conn.execute(
            """SELECT date, event_data FROM session_events
               WHERE event_type = 'reencounter' AND date >= ?
               ORDER BY date""",
            (cutoff,),
        ).fetchall()
    except Exception:
        conn.close()
        return {
            "total_reencounters": 0,
            "unique_chunks": 0,
            "unique_content": 0,
            "by_day": [],
            "chunks_detail": [],
        }

    conn.close()

    all_chunks = {}  # type: Dict[str, int]
    all_content = set()  # type: set
    by_day = {}  # type: Dict[str, Dict]

    for row in rows:
        date = row["date"]
        try:
            data = json.loads(row["event_data"])
        except (json.JSONDecodeError, TypeError):
            continue

        ct = data.get("content_type", "")
        cid = data.get("content_id", 0)
        chunks = data.get("chunks", [])

        all_content.add((ct, cid))

        if date not in by_day:
            by_day[date] = {"date": date, "reencounters": 0, "chunks": set(), "content": set()}
        by_day[date]["reencounters"] += 1
        by_day[date]["content"].add((ct, cid))

        for c in chunks:
            all_chunks[c] = all_chunks.get(c, 0) + 1
            by_day[date]["chunks"].add(c)

    # Serialize sets for JSON
    by_day_list = []
    for d in sorted(by_day.keys()):
        entry = by_day[d]
        by_day_list.append({
            "date": entry["date"],
            "reencounters": entry["reencounters"],
            "unique_chunks": len(entry["chunks"]),
            "unique_content": len(entry["content"]),
        })

    chunks_detail = [
        {"chunk": c, "encounters": n}
        for c, n in sorted(all_chunks.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        "total_reencounters": len(rows),
        "unique_chunks": len(all_chunks),
        "unique_content": len(all_content),
        "by_day": by_day_list,
        "chunks_detail": chunks_detail,
    }
