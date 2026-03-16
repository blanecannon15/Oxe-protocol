"""
fatigue_monitor.py — Fatigue detection and session design for Oxe Protocol.

Tracks cognitive fatigue signals (accuracy drop, latency rise, replay
frequency, pace slowdown, elapsed time) and recommends session adjustments
in real time.  Also generates optimised block schedules for daily study.
"""

import json
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from srs_engine import DB_PATH, get_connection

# ---------------------------------------------------------------------------
# In-memory rolling window
# ---------------------------------------------------------------------------
# Resets on server restart (acceptable for MVP).

_rolling_window = []  # type: List[Dict]
# Each entry: {"timestamp": float, "latency_ms": float, "rating": int, "replays": int}

_session_start = None  # type: Optional[float]
# Epoch timestamp of the first review event in the current session.


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------

def record_review_event(latency_ms: float, rating: int, replays: int = 0,
                        db_path=DB_PATH) -> None:
    """Append a review event to the in-memory rolling window.

    Sets ``_session_start`` on the first call.  The window is capped at the
    most recent 100 events to prevent unbounded memory growth.

    Every 5 minutes (measured from ``_session_start``) a fatigue snapshot is
    automatically persisted to the database so we can analyse fatigue curves
    after the session.
    """
    global _rolling_window, _session_start

    now = time.time()

    if _session_start is None:
        _session_start = now

    _rolling_window.append({
        "timestamp": now,
        "latency_ms": latency_ms,
        "rating": rating,
        "replays": replays,
    })

    # Keep only the last 100 events.
    if len(_rolling_window) > 100:
        _rolling_window = _rolling_window[-100:]

    # --- Periodic fatigue snapshot ---
    minutes_elapsed = (now - _session_start) / 60.0
    expected_snapshots = math.floor(minutes_elapsed / 5)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    existing = conn.execute(
        "SELECT COUNT(*) FROM fatigue_snapshots WHERE date = ?",
        (today_str,),
    ).fetchone()[0]
    conn.close()

    if expected_snapshots > existing:
        take_fatigue_snapshot(db_path=db_path)


# ---------------------------------------------------------------------------
# Rolling metrics
# ---------------------------------------------------------------------------

def _compute_rolling_metrics(window_minutes: float = 5.0) -> dict:
    """Return accuracy, latency, replay frequency, and pace over the recent window.

    If no events fall within the window, sensible defaults are returned so
    that downstream calculations remain stable.
    """
    now = time.time()
    cutoff = now - window_minutes * 60

    recent = [e for e in _rolling_window if e["timestamp"] >= cutoff]

    if not recent:
        return {
            "accuracy_5min": 0.85,
            "avg_latency_5min": 0.0,
            "replay_freq": 0.0,
            "items_per_minute": 0.0,
        }

    good_or_easy = sum(1 for e in recent if e["rating"] >= 3)
    accuracy = good_or_easy / len(recent)

    avg_latency = sum(e["latency_ms"] for e in recent) / len(recent)
    replay_freq = sum(e["replays"] for e in recent) / len(recent)
    items_per_minute = len(recent) / window_minutes

    return {
        "accuracy_5min": round(accuracy, 4),
        "avg_latency_5min": round(avg_latency, 1),
        "replay_freq": round(replay_freq, 2),
        "items_per_minute": round(items_per_minute, 2),
    }


# ---------------------------------------------------------------------------
# Fatigue score
# ---------------------------------------------------------------------------

def compute_fatigue_score(accuracy_5min: float, avg_latency_5min: float,
                          replay_freq: float, items_per_minute: float,
                          minute_offset: float) -> float:
    """Compute a 0-100 fatigue score from rolling metrics and elapsed time.

    Weights
    -------
    - 25 %  accuracy drop (relative to 85 % baseline)
    - 25 %  response latency (saturates at 2 000 ms)
    - 20 %  audio replay frequency (saturates at 3 replays/item)
    - 15 %  pace slowdown (below 2 items/min)
    - 15 %  elapsed time (saturates at 180 min)
    """
    fatigue = (
        25 * max(0, 1 - accuracy_5min / 0.85)
      + 25 * min(avg_latency_5min / 2000, 1.0)
      + 20 * min(replay_freq / 3.0, 1.0)
      + 15 * max(0, 1 - items_per_minute / 2.0)
      + 15 * min(minute_offset / 180, 1.0)
    )
    return round(min(max(fatigue, 0), 100), 1)


# ---------------------------------------------------------------------------
# Main API — check fatigue
# ---------------------------------------------------------------------------

def check_fatigue(db_path=DB_PATH) -> dict:
    """Return the current fatigue state and a recommendation.

    Recommendations
    ---------------
    - ``"start_session"`` — no events recorded yet.
    - ``"continue"``      — fatigue < 30, keep drilling.
    - ``"switch_mode"``   — fatigue 30-50, swap to passive listening.
    - ``"take_break"``    — fatigue 50-70, pause for 5 minutes.
    - ``"end_session"``   — fatigue > 70, stop for the day.
    """
    if _session_start is None:
        return {
            "fatigue_score": 0,
            "recommendation": "start_session",
            "minutes_active": 0,
        }

    now = time.time()
    minute_offset = (now - _session_start) / 60.0

    metrics = _compute_rolling_metrics()
    score = compute_fatigue_score(
        accuracy_5min=metrics["accuracy_5min"],
        avg_latency_5min=metrics["avg_latency_5min"],
        replay_freq=metrics["replay_freq"],
        items_per_minute=metrics["items_per_minute"],
        minute_offset=minute_offset,
    )

    if score < 30:
        recommendation = "continue"
        suggested_action = ""
    elif score <= 50:
        recommendation = "switch_mode"
        suggested_action = "Troca pra escuta passiva"
    elif score <= 70:
        recommendation = "take_break"
        suggested_action = "Pausa de 5 minutos"
    else:
        recommendation = "end_session"
        suggested_action = "Melhor parar por hoje"

    result = {
        "fatigue_score": score,
        "recommendation": recommendation,
        "minutes_active": round(minute_offset, 1),
        "metrics": metrics,
    }
    if suggested_action:
        result["suggested_action"] = suggested_action

    return result


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------

def take_fatigue_snapshot(db_path=DB_PATH) -> None:
    """Persist the current rolling metrics and fatigue score to the database."""
    if _session_start is None:
        return

    now = time.time()
    minute_offset = (now - _session_start) / 60.0
    metrics = _compute_rolling_metrics()
    score = compute_fatigue_score(
        accuracy_5min=metrics["accuracy_5min"],
        avg_latency_5min=metrics["avg_latency_5min"],
        replay_freq=metrics["replay_freq"],
        items_per_minute=metrics["items_per_minute"],
        minute_offset=minute_offset,
    )

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO fatigue_snapshots
           (date, minute_offset, accuracy_5min, avg_latency_5min,
            replay_freq, items_per_minute, fatigue_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            today_str,
            round(minute_offset),
            metrics["accuracy_5min"],
            metrics["avg_latency_5min"],
            metrics["replay_freq"],
            metrics["items_per_minute"],
            score,
        ),
    )
    conn.commit()
    conn.close()


def get_fatigue_history(date=None, db_path=DB_PATH):
    """Return all fatigue snapshots for *date* (defaults to today), ordered by time."""
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT minute_offset, accuracy_5min, avg_latency_5min,
                  replay_freq, items_per_minute, fatigue_score, timestamp
           FROM fatigue_snapshots
           WHERE date = ?
           ORDER BY minute_offset ASC""",
        (date,),
    ).fetchall()
    conn.close()

    return [
        {
            "minute_offset": r[0],
            "accuracy_5min": r[1],
            "avg_latency_5min": r[2],
            "replay_freq": r[3],
            "items_per_minute": r[4],
            "fatigue_score": r[5],
            "timestamp": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Session block designer
# ---------------------------------------------------------------------------

def design_session_blocks(total_minutes=240, fatigue_history=None):
    """Generate an ordered list of session blocks (drill / listen / shadow / break).

    Parameters
    ----------
    total_minutes : int
        Target session length in minutes.
    fatigue_history : list[dict], optional
        Output of :func:`get_fatigue_history`.  When supplied and early
        fatigue is detected (score > 50 before minute 90), the schedule
        shifts toward shorter drills, extra breaks, and more listening.

    Returns
    -------
    list[dict]
        Each element: ``{"block_type": str, "duration_minutes": int, "order": int}``
    """
    # Detect early fatigue from history.
    early_fatigue = False
    if fatigue_history:
        for snap in fatigue_history:
            if snap.get("minute_offset", 999) < 90 and snap.get("fatigue_score", 0) > 50:
                early_fatigue = True
                break

    # --- Build the canonical 240-minute template --------------------------
    if early_fatigue:
        # Lighter schedule: shorter drills, more listening, extra breaks.
        template = [
            ("drill",  20), ("break", 5), ("listen", 25), ("break", 5),   # 55
            ("listen", 25), ("break", 5), ("drill",  20), ("break", 5),   # 55
            ("listen", 25), ("break", 5), ("shadow", 20), ("break", 5),   # 55
            ("listen", 25), ("break", 5), ("drill",  20), ("break", 5),   # 55
        ]
        # 220 active + buffer
    else:
        # Standard schedule.
        template = [
            ("drill",  25), ("break", 5), ("drill",  25), ("break", 5),   # 60
            ("listen", 25), ("break", 5), ("drill",  25), ("break", 5),   # 60
            ("listen", 25), ("break", 5), ("shadow", 20), ("break", 5),   # 55
            ("drill",  25), ("break", 5), ("listen", 25), ("break", 5),   # 60
        ]
        # 235 active + 5 buffer

    template_total = sum(d for _, d in template)

    # Scale proportionally for non-240-minute sessions.
    scale = total_minutes / template_total if template_total else 1.0

    blocks = []
    for idx, (btype, dur) in enumerate(template):
        scaled_dur = max(1, round(dur * scale))
        blocks.append({
            "block_type": btype,
            "duration_minutes": scaled_dur,
            "order": idx + 1,
        })

    # Trim or pad so the total matches the request as closely as possible.
    current_total = sum(b["duration_minutes"] for b in blocks)
    if current_total < total_minutes:
        blocks.append({
            "block_type": "break",
            "duration_minutes": total_minutes - current_total,
            "order": len(blocks) + 1,
        })

    return blocks


# ---------------------------------------------------------------------------
# Session reset
# ---------------------------------------------------------------------------

def reset_session() -> None:
    """Clear the rolling window and session timer.

    Call at the start of a new day or manually between sessions.
    """
    global _rolling_window, _session_start
    _rolling_window = []
    _session_start = None
