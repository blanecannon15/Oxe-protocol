"""
daily_router.py — Daily plan generator and session orchestrator for the Oxe Protocol.

Generates a structured daily training plan based on learner state, SRS due items,
fragility data, and yesterday's performance. Orchestrates blocks of drills,
listening, shadowing, breaks, and conversation practice across a configurable
session duration (default 600 minutes / 10 hours).

Tracks real time spent per activity via the activity_timer table.
Learner baseline: 900 listening hours.

Usage:
    from daily_router import get_today_plan, get_next_block, record_block_completion
    plan = get_today_plan()
    block = get_next_block()
"""

import json
from datetime import datetime, timezone, timedelta

from srs_engine import (
    DB_PATH,
    get_connection,
    get_due_chunks,
    get_due_words,
    get_daily_stats,
    get_unlocked_tier,
    TIER_LABELS,
)
from acquisition_engine import (
    get_state_distribution,
    run_fragility_scan,
    get_fragile_queue,
    get_fragile_summary,
)
from training_modes import select_mode_for_item, TRAINING_MODES
from fatigue_monitor import get_fatigue_history, check_fatigue
from content_ladder import get_learner_level


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_str():
    """Return today's date as YYYY-MM-DD in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_str():
    """Return yesterday's date as YYYY-MM-DD in UTC."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _get_yesterday_stats(db_path=DB_PATH):
    """Fetch daily_stats row for yesterday. Returns dict or None."""
    yesterday = _yesterday_str()
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM daily_stats WHERE date = ?", (yesterday,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def _compute_difficulty_bias(yesterday_stats, fatigue_data=None):
    """Determine session allocation ratios based on yesterday's accuracy, latency, and fatigue."""
    if yesterday_stats is None or yesterday_stats.get("words_reviewed", 0) == 0:
        return {
            "drill": 0.40,
            "listen": 0.30,
            "shadow": 0.20,
            "conversa": 0.0,
            "rest": 0.10,
        }

    accuracy = yesterday_stats["words_mastered"] / max(yesterday_stats["words_reviewed"], 1)

    # Check for early fatigue from yesterday
    early_fatigue = False
    if fatigue_data:
        for snap in fatigue_data:
            if snap.get("minute_offset", 999) < 90 and snap.get("fatigue_score", 0) > 50:
                early_fatigue = True
                break

    if accuracy < 0.3 or early_fatigue:
        return {
            "drill": 0.60 if early_fatigue else 0.70,
            "listen": 0.25 if early_fatigue else 0.20,
            "shadow": 0.0,
            "conversa": 0.0,
            "rest": 0.15 if early_fatigue else 0.10,
        }
    elif accuracy < 0.7:
        return {
            "drill": 0.50,
            "listen": 0.30,
            "shadow": 0.20,
            "conversa": 0.0,
            "rest": 0.0,
        }
    else:
        return {
            "drill": 0.40,
            "listen": 0.30,
            "shadow": 0.20,
            "conversa": 0.10,
            "rest": 0.0,
        }


def _build_item_list(item):
    """Convert an sqlite3.Row item to a serializable dict for target_items."""
    try:
        d = dict(item)
        # Remove non-serializable or bulky fields
        d.pop("srs_state", None)
        return d
    except Exception:
        return {"id": item["id"] if "id" in item.keys() else None}


def _select_mode_for_block(items, block_type, is_fragile=False):
    """Determine the training mode key for a drill block.

    Uses the first item in the block to pick a dominant mode via
    select_mode_for_item. Fragile blocks get special rescue modes.
    """
    if is_fragile and items:
        # Check if items are slow-but-known vs generally fragile
        first = items[0]
        latency = first.get("last_retrieval_latency") or first.get("avg_latency_ms")
        if latency and latency > 1000:
            return "known_but_slow_drill"
        return "fragile_rescue_drill"

    if not items:
        # Fallback modes by block type
        fallback = {
            "srs_drill": "standard_drill",
            "listening": "passive_listening",
            "shadowing": "echo_shadowing",
            "conversa": "free_conversation",
            "break": "break",
        }
        return fallback.get(block_type, "standard_drill")

    # Use acquisition engine's mode selector on the first item
    try:
        mode = select_mode_for_item(items[0])
        return mode
    except Exception:
        return "standard_drill"


# ---------------------------------------------------------------------------
# Core plan generation
# ---------------------------------------------------------------------------

def generate_daily_plan(total_minutes=600, db_path=DB_PATH):
    """Generate a structured daily training plan.

    Algorithm:
        1. Analyse yesterday's performance to set difficulty bias.
        2. Pull SRS due queues (chunks first, then words).
        3. Run fragility scan and collect rescue items.
        4. Build timed blocks following a drill/listen/shadow/break pattern.
        5. Assign training modes to each block.
        6. Persist the plan to the daily_plan table.

    Args:
        total_minutes: Total session length in minutes (default 240).
        db_path: Path to the SQLite database.

    Returns:
        A dict containing date, blocks, priority_queues, and target_metrics.
    """
    today = _today_str()

    # ------------------------------------------------------------------
    # 1. Yesterday's stats
    # ------------------------------------------------------------------
    yesterday_stats = _get_yesterday_stats(db_path)

    # ------------------------------------------------------------------
    # 2. State distribution
    # ------------------------------------------------------------------
    try:
        state_dist = get_state_distribution(db_path)
    except Exception:
        state_dist = {}

    # ------------------------------------------------------------------
    # 3. Due SRS items — chunks first, then words
    # ------------------------------------------------------------------
    try:
        due_chunks = get_due_chunks(db_path)
    except Exception:
        due_chunks = []

    try:
        due_words = get_due_words(db_path)
    except Exception:
        due_words = []

    due_items = list(due_chunks) + list(due_words)

    # ------------------------------------------------------------------
    # 4. Fragility scan
    # ------------------------------------------------------------------
    try:
        run_fragility_scan(db_path)
    except Exception:
        pass

    try:
        fragile_queue = get_fragile_queue(db_path)
    except Exception:
        fragile_queue = []

    try:
        fragile_summary = get_fragile_summary(db_path)
    except Exception:
        fragile_summary = {}

    # ------------------------------------------------------------------
    # 5. Current tier and learner level
    # ------------------------------------------------------------------
    try:
        tier = get_unlocked_tier(db_path)
    except Exception:
        tier = 1

    # ------------------------------------------------------------------
    # 5b. Fatigue history from yesterday
    # ------------------------------------------------------------------
    try:
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        fatigue_data = get_fatigue_history(yesterday, db_path)
    except Exception:
        fatigue_data = []

    # ------------------------------------------------------------------
    # 5c. Content level
    # ------------------------------------------------------------------
    try:
        content_level = get_learner_level(db_path)
    except Exception:
        content_level = "P1"

    # ------------------------------------------------------------------
    # 6. Difficulty bias
    # ------------------------------------------------------------------
    bias = _compute_difficulty_bias(yesterday_stats, fatigue_data)
    conversa_eligible = bias["conversa"] > 0

    # ------------------------------------------------------------------
    # 7. Build blocks
    # ------------------------------------------------------------------
    #
    # Session template for 240 minutes:
    #   drill(25) break(5) drill(25) break(5)        = 60 min
    #   listen(25) break(5) drill(25) break(5)       = 60 min
    #   listen(25) break(5) shadow(20) break(5)      = 55 min
    #   drill(25) break(5) listen(25) break(5)       = 60 min
    #   [optional conversa(15) if gated]
    #
    # We scale linearly for non-240 sessions.

    # 10-hour template (~600 min): heavy CI listening, SRS drills, shadowing, conversa
    # Pattern: 3 sessions of ~3h20m each with breaks
    # Session 1 (Morning): drill-heavy to clear SRS queue
    # Session 2 (Midday): listening-heavy for CI
    # Session 3 (Afternoon): mix of shadow, conversa, listening
    template = [
        # ── Session 1: Morning (drill-heavy) ── ~200 min
        ("srs_drill", 25),
        ("break", 5),
        ("srs_drill", 25),
        ("break", 5),
        ("listening", 30),
        ("break", 5),
        ("srs_drill", 25),
        ("break", 5),
        ("listening", 30),
        ("break", 10),
        ("shadowing", 20),
        ("break", 5),
        # ── Session 2: Midday (listening-heavy) ── ~200 min
        ("listening", 30),
        ("break", 5),
        ("srs_drill", 25),
        ("break", 5),
        ("listening", 30),
        ("break", 5),
        ("shadowing", 25),
        ("break", 5),
        ("listening", 30),
        ("break", 10),
        ("srs_drill", 25),
        ("break", 5),
        # ── Session 3: Afternoon (output + CI) ── ~200 min
        ("listening", 30),
        ("break", 5),
        ("srs_drill", 25),
        ("break", 5),
        ("shadowing", 25),
        ("break", 5),
        ("listening", 30),
        ("break", 5),
        ("srs_drill", 25),
        ("break", 10),
        ("listening", 30),
        ("break", 5),
    ]

    if conversa_eligible:
        template.append(("conversa", 20))

    # Compute total template minutes for scaling
    template_total = sum(dur for _, dur in template)
    scale = total_minutes / template_total if template_total > 0 else 1.0

    # Prepare item pools
    fragile_items = [_build_item_list(item) for item in fragile_queue[:30]]
    srs_items = [_build_item_list(item) for item in due_items]
    fragile_idx = 0
    srs_idx = 0

    # Items at AUTOMATIC_CLEAN for shadowing
    try:
        shadow_candidates = [
            _build_item_list(item)
            for item in due_items
            if dict(item).get("state") == "AUTOMATIC_CLEAN"
        ]
    except Exception:
        shadow_candidates = []

    # If no explicit AUTOMATIC_CLEAN items, use general due items for shadowing
    if not shadow_candidates:
        shadow_candidates = srs_items[:20]

    blocks = []
    block_id = 1

    for block_type, base_dur in template:
        duration = max(5, round(base_dur * scale))

        if block_type == "break":
            blocks.append({
                "block_id": block_id,
                "type": "break",
                "mode": "break",
                "duration_minutes": duration,
                "target_items": [],
                "difficulty_params": {},
            })
        elif block_type == "srs_drill":
            # Each block holds ~12-15 items (2 min per item average)
            items_per_block = max(1, duration // 2)

            # First 30% from fragile queue
            fragile_count = max(1, round(items_per_block * 0.3))
            block_items = []

            # Pull fragile items
            for _ in range(fragile_count):
                if fragile_idx < len(fragile_items):
                    block_items.append(fragile_items[fragile_idx])
                    fragile_idx += 1

            # Fill remaining with SRS due items
            remaining = items_per_block - len(block_items)
            for _ in range(remaining):
                if srs_idx < len(srs_items):
                    block_items.append(srs_items[srs_idx])
                    srs_idx += 1

            is_fragile_block = (
                len(block_items) > 0
                and fragile_count > 0
                and fragile_idx <= len(fragile_items)
                and any(i in fragile_items for i in block_items[:fragile_count])
            )
            mode = _select_mode_for_block(block_items, "srs_drill", is_fragile=is_fragile_block)

            blocks.append({
                "block_id": block_id,
                "type": "srs_drill",
                "mode": mode,
                "duration_minutes": duration,
                "target_items": block_items,
                "difficulty_params": {
                    "bias": bias,
                    "fragile_ratio": 0.3,
                },
            })
        elif block_type == "listening":
            # Listening blocks suggest content type and difficulty
            blocks.append({
                "block_id": block_id,
                "type": "listening",
                "mode": "passive_listening",
                "duration_minutes": duration,
                "target_items": [],
                "difficulty_params": {
                    "content_type": "podcast" if tier >= 2 else "story",
                    "suggested_difficulty": min(tier * 20, 100),
                    "tier": tier,
                },
            })
        elif block_type == "shadowing":
            shadow_count = max(1, duration // 2)
            shadow_items = shadow_candidates[:shadow_count]
            blocks.append({
                "block_id": block_id,
                "type": "shadowing",
                "mode": "echo_shadowing",
                "duration_minutes": duration,
                "target_items": shadow_items,
                "difficulty_params": {
                    "target_state": "AUTOMATIC_CLEAN",
                    "delay_ms": 200,
                },
            })
        elif block_type == "conversa":
            blocks.append({
                "block_id": block_id,
                "type": "conversa",
                "mode": "free_conversation",
                "duration_minutes": duration,
                "target_items": [],
                "difficulty_params": {
                    "speech_stage": tier,
                    "prompt_type": "situational",
                },
            })

        block_id += 1

    # ------------------------------------------------------------------
    # 8. Priority queues
    # ------------------------------------------------------------------
    prosody_blocked = []
    try:
        for item in fragile_queue:
            d = dict(item)
            if d.get("fragility_type") == "blocked_by_prosody":
                prosody_blocked.append(_build_item_list(item))
    except Exception:
        pass

    # New chunks: items from due_chunks not yet reviewed
    new_chunks = []
    for item in due_chunks[:20]:
        try:
            d = dict(item)
            if d.get("mastery_level", 0) == 0:
                new_chunks.append(_build_item_list(item))
        except Exception:
            pass

    priority_queues = {
        "fragile_rescue": fragile_items[:10],
        "new_chunks": new_chunks[:8],
        "prosody_blocked": prosody_blocked[:10],
    }

    # ------------------------------------------------------------------
    # 9. Target metrics
    # ------------------------------------------------------------------
    listening_minutes = sum(
        b["duration_minutes"] for b in blocks if b["type"] == "listening"
    )
    shadowing_minutes = sum(
        b["duration_minutes"] for b in blocks if b["type"] == "shadowing"
    )

    target_metrics = {
        "new_chunks_target": 8,
        "srs_reviews_target": min(len(due_items), 80),
        "listening_minutes": listening_minutes,
        "shadowing_minutes": shadowing_minutes,
    }

    # ------------------------------------------------------------------
    # 10. Assemble plan
    # ------------------------------------------------------------------
    plan = {
        "date": today,
        "learner_level": f"Tier {tier}",
        "content_level": content_level,
        "total_minutes": total_minutes,
        "blocks": blocks,
        "priority_queues": priority_queues,
        "target_metrics": target_metrics,
    }

    # ------------------------------------------------------------------
    # 11. Persist to daily_plan table
    # ------------------------------------------------------------------
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO daily_plan (date, plan_json) VALUES (?, ?)",
        (today, json.dumps(plan, default=str)),
    )
    conn.commit()
    conn.close()

    return plan


# ---------------------------------------------------------------------------
# Plan retrieval
# ---------------------------------------------------------------------------

def get_today_plan(db_path=DB_PATH):
    """Retrieve today's plan from the database, generating one if needed.

    Checks the daily_plan table for today's date. If a plan exists, returns
    it as a parsed dict. Otherwise, calls generate_daily_plan() to create
    a fresh plan and returns it.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        The daily plan dict.
    """
    today = _today_str()
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT plan_json FROM daily_plan WHERE date = ?", (today,)
    ).fetchone()
    conn.close()

    if row is not None:
        return json.loads(row["plan_json"])

    return generate_daily_plan(db_path=db_path)


# ---------------------------------------------------------------------------
# Block navigation
# ---------------------------------------------------------------------------

def _get_completed_block_ids(today, db_path=DB_PATH):
    """Return a set of block_ids that have 'block_complete' events today."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT event_data FROM session_events
           WHERE date = ? AND event_type = 'block_complete'""",
        (today,),
    ).fetchall()
    conn.close()

    completed = set()
    for row in rows:
        try:
            data = json.loads(row["event_data"])
            completed.add(data.get("block_id"))
        except (json.JSONDecodeError, TypeError):
            pass
    return completed


def get_next_block(db_path=DB_PATH):
    """Return the first uncompleted block in today's plan, or None.

    Checks session_events for 'block_complete' entries matching each
    block_id. The first block without a completion event is returned.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        A block dict, or None if all blocks are complete (or no plan exists).
    """
    today = _today_str()
    plan = get_today_plan(db_path)
    if not plan or "blocks" not in plan:
        return None

    completed = _get_completed_block_ids(today, db_path)

    for block in plan["blocks"]:
        if block["block_id"] not in completed:
            return block

    return None


# ---------------------------------------------------------------------------
# Block completion
# ---------------------------------------------------------------------------

def record_block_completion(block_id, actual_data, db_path=DB_PATH):
    """Record that a block has been completed.

    Inserts a session_event with event_type='block_complete' and updates
    the daily_plan's completed_pct.

    Args:
        block_id: The integer block_id from the plan.
        actual_data: Dict of actual results (items_reviewed, accuracy, etc.).
        db_path: Path to the SQLite database.
    """
    today = _today_str()
    now = datetime.now(timezone.utc).isoformat()

    event_data = {
        "block_id": block_id,
        "actual": actual_data,
        "completed_at": now,
    }

    conn = get_connection(db_path)

    # Insert completion event
    conn.execute(
        """INSERT INTO session_events (date, event_type, event_data, timestamp)
           VALUES (?, 'block_complete', ?, ?)""",
        (today, json.dumps(event_data, default=str), now),
    )
    conn.commit()
    conn.close()

    # Update completed_pct
    plan = get_today_plan(db_path)
    if plan and "blocks" in plan:
        total_blocks = len(plan["blocks"])
        if total_blocks > 0:
            completed = _get_completed_block_ids(today, db_path)
            pct = len(completed) / total_blocks * 100.0

            conn = get_connection(db_path)
            conn.execute(
                "UPDATE daily_plan SET completed_pct = ? WHERE date = ?",
                (pct, today),
            )
            conn.commit()
            conn.close()


# ---------------------------------------------------------------------------
# Mid-session adjustment
# ---------------------------------------------------------------------------

def adjust_plan_mid_session(fatigue_score, db_path=DB_PATH):
    """Adjust remaining blocks when fatigue is detected.

    Modifies uncompleted blocks in today's plan based on fatigue level:
    - fatigue_score >= 50: convert the next drill block to a listening block.
    - fatigue_score >= 70: also add an extra break and halve remaining drill
      durations.

    Logs a 'plan_adjusted' session_event.

    Args:
        fatigue_score: Integer 0-100 representing detected fatigue.
        db_path: Path to the SQLite database.

    Returns:
        The updated plan dict.
    """
    today = _today_str()
    plan = get_today_plan(db_path)
    if not plan or "blocks" not in plan:
        return plan

    completed = _get_completed_block_ids(today, db_path)
    blocks = plan["blocks"]
    modified = False

    if fatigue_score >= 50:
        # Convert next uncompleted drill block to listening
        for block in blocks:
            if block["block_id"] not in completed and block["type"] == "srs_drill":
                block["type"] = "listening"
                block["mode"] = "passive_listening"
                block["difficulty_params"] = {
                    "content_type": "story",
                    "suggested_difficulty": 40,
                    "fatigue_adjusted": True,
                }
                block["target_items"] = []
                modified = True
                break

    if fatigue_score >= 70:
        # Add extra break and halve remaining drill durations
        new_blocks = []
        inserted_break = False
        for block in blocks:
            if block["block_id"] in completed:
                new_blocks.append(block)
                continue

            if block["type"] == "srs_drill":
                block["duration_minutes"] = max(5, block["duration_minutes"] // 2)
                modified = True

            # Insert one extra break before the next active block
            if not inserted_break and block["block_id"] not in completed and block["type"] != "break":
                extra_break = {
                    "block_id": max(b["block_id"] for b in blocks) + 1,
                    "type": "break",
                    "mode": "break",
                    "duration_minutes": 10,
                    "target_items": [],
                    "difficulty_params": {"fatigue_break": True},
                }
                new_blocks.append(extra_break)
                inserted_break = True
                modified = True

            new_blocks.append(block)

        blocks = new_blocks
        plan["blocks"] = blocks

    if modified:
        # Persist updated plan
        conn = get_connection(db_path)
        conn.execute(
            "UPDATE daily_plan SET plan_json = ? WHERE date = ?",
            (json.dumps(plan, default=str), today),
        )
        conn.commit()
        conn.close()

    # Log the adjustment event
    now = datetime.now(timezone.utc).isoformat()
    event_data = {
        "fatigue_score": fatigue_score,
        "modified": modified,
        "adjusted_at": now,
    }
    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO session_events (date, event_type, event_data, timestamp)
           VALUES (?, 'plan_adjusted', ?, ?)""",
        (today, json.dumps(event_data, default=str), now),
    )
    conn.commit()
    conn.close()

    return plan


# ---------------------------------------------------------------------------
# Progress summary
# ---------------------------------------------------------------------------

def get_plan_progress(db_path=DB_PATH):
    """Return a summary of today's plan progress.

    Aggregates data from the daily plan, session_events, and daily_stats
    to produce a snapshot of the learner's current session state.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        A dict with total_blocks, completed_blocks, completed_pct,
        current_block, items_reviewed_today, and items_promoted_today.
    """
    today = _today_str()
    plan = get_today_plan(db_path)

    if not plan or "blocks" not in plan:
        return {
            "date": today,
            "total_blocks": 0,
            "completed_blocks": 0,
            "completed_pct": 0.0,
            "current_block": None,
            "items_reviewed_today": 0,
            "items_promoted_today": 0,
        }

    total_blocks = len(plan["blocks"])
    completed = _get_completed_block_ids(today, db_path)
    completed_blocks = len(completed)
    completed_pct = (completed_blocks / total_blocks * 100.0) if total_blocks > 0 else 0.0

    # Current block = first uncompleted
    current_block = None
    for block in plan["blocks"]:
        if block["block_id"] not in completed:
            current_block = block
            break

    # Items reviewed and promoted from session_events
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT event_data FROM session_events
           WHERE date = ? AND event_type = 'block_complete'""",
        (today,),
    ).fetchall()
    conn.close()

    items_reviewed = 0
    items_promoted = 0
    for row in rows:
        try:
            data = json.loads(row["event_data"])
            actual = data.get("actual", {})
            items_reviewed += actual.get("items_reviewed", 0)
            items_promoted += actual.get("items_promoted", 0)
        except (json.JSONDecodeError, TypeError):
            pass

    # Supplement with daily_stats if session_events undercount
    try:
        daily = get_daily_stats(db_path)
        items_reviewed = max(items_reviewed, daily.get("words_reviewed", 0))
        items_promoted = max(items_promoted, daily.get("words_mastered", 0))
    except Exception:
        pass

    # Time spent per activity today
    time_by_activity = get_time_by_activity(today, db_path)
    total_minutes_today = sum(time_by_activity.values())
    listening_hours_total = get_cumulative_listening_hours(db_path)

    return {
        "date": today,
        "total_blocks": total_blocks,
        "completed_blocks": completed_blocks,
        "completed_pct": round(completed_pct, 1),
        "current_block": current_block,
        "items_reviewed_today": items_reviewed,
        "items_promoted_today": items_promoted,
        "time_by_activity": time_by_activity,
        "total_minutes_today": round(total_minutes_today, 1),
        "listening_hours_total": round(listening_hours_total, 1),
    }


# ---------------------------------------------------------------------------
# Activity time tracking
# ---------------------------------------------------------------------------

def start_activity(activity, db_path=DB_PATH):
    """Record the start of an activity. Returns the timer row id."""
    today = _today_str()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection(db_path)

    # Close any open timer for this activity today
    conn.execute(
        """UPDATE activity_timer SET ended_at = ?, seconds_spent = CAST(
            (julianday(?) - julianday(started_at)) * 86400 AS INTEGER
        ) WHERE date = ? AND activity = ? AND ended_at IS NULL""",
        (now, now, today, activity),
    )

    conn.execute(
        "INSERT INTO activity_timer (date, activity, started_at) VALUES (?, ?, ?)",
        (today, activity, now),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def stop_activity(activity, db_path=DB_PATH):
    """Stop any open timer for this activity today. Returns seconds spent."""
    today = _today_str()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection(db_path)
    conn.execute(
        """UPDATE activity_timer SET ended_at = ?, seconds_spent = CAST(
            (julianday(?) - julianday(started_at)) * 86400 AS INTEGER
        ) WHERE date = ? AND activity = ? AND ended_at IS NULL""",
        (now, now, today, activity),
    )
    conn.commit()

    # Return total seconds for this activity today
    total = conn.execute(
        "SELECT COALESCE(SUM(seconds_spent), 0) FROM activity_timer WHERE date = ? AND activity = ?",
        (today, activity),
    ).fetchone()[0]
    conn.close()
    return total


def get_time_by_activity(date=None, db_path=DB_PATH):
    """Return dict of activity → minutes spent for a given date (default today)."""
    if date is None:
        date = _today_str()
    conn = get_connection(db_path)

    # For open timers, compute live elapsed
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT activity,
            COALESCE(SUM(
                CASE WHEN ended_at IS NULL
                    THEN CAST((julianday(?) - julianday(started_at)) * 86400 AS INTEGER)
                    ELSE seconds_spent
                END
            ), 0) as total_secs
        FROM activity_timer WHERE date = ? GROUP BY activity""",
        (now, date),
    ).fetchall()
    conn.close()

    result = {}
    for r in rows:
        result[r["activity"]] = round(r["total_secs"] / 60.0, 1)
    return result


def get_cumulative_listening_hours(db_path=DB_PATH):
    """Return total listening hours = baseline + all tracked listening minutes."""
    conn = get_connection(db_path)

    # Get baseline
    baseline = 900.0
    try:
        row = conn.execute(
            "SELECT value FROM learner_profile WHERE key = 'listening_hours_baseline'"
        ).fetchone()
        if row:
            baseline = float(row["value"])
    except Exception:
        pass

    # Sum all listening activity across all days
    try:
        total_secs = conn.execute(
            """SELECT COALESCE(SUM(
                CASE WHEN ended_at IS NULL
                    THEN CAST((julianday('now') - julianday(started_at)) * 86400 AS INTEGER)
                    ELSE seconds_spent
                END
            ), 0) FROM activity_timer WHERE activity = 'listening'"""
        ).fetchone()[0]
    except Exception:
        total_secs = 0

    conn.close()
    return baseline + (total_secs / 3600.0)


def get_daily_target_minutes(db_path=DB_PATH):
    """Return the daily target in minutes (default 600)."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM learner_profile WHERE key = 'daily_target_minutes'"
        ).fetchone()
        if row:
            return int(row["value"])
    except Exception:
        pass
    finally:
        conn.close()
    return 600
