"""
speech_ladder.py — Speech unlock ladder for the Oxe Protocol.

Manages the learner's progression through 6 speech production stages,
from Echo (shadowing) to Free conversation. Each stage has an exit gate
based on acquisition state counts, biometric scores, latency, and
output success rates. Supports advancement and regression.

Usage (programmatic):
    from speech_ladder import get_current_stage, evaluate_gates, advance_stage

Stages:
    1. Eco              — Repete exatamente o que ouve
    2. Troca de Chunk   — Substitui um chunk na frase modelo
    3. Reconto Guiado   — Reconta uma história com prompts visuais
    4. Expressão Guiada — Responde perguntas usando chunks conhecidos
    5. Semi-Livre       — Conversa com tópico definido, vocabulário sugerido
    6. Livre            — Conversa livre, qualquer tópico
"""

import json
from datetime import datetime, timezone

from srs_engine import DB_PATH, get_connection
from acquisition_engine import get_state_distribution

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPEECH_STAGES = {
    1: {
        "name": "echo",
        "label": "Eco",
        "description": "Repete exatamente o que ouve",
        "exercises": ["shadowing_repeat", "audio_playback"],
        "gate": {
            "min_EFFORTFUL_AUDIO": 20,
            "min_biometric_avg": 50,
        },
    },
    2: {
        "name": "chunk_substitution",
        "label": "Troca de Chunk",
        "description": "Substitui um chunk na frase modelo",
        "exercises": ["shadowing_repeat", "chunk_swap", "fill_in_blank"],
        "gate": {
            "min_AUTOMATIC_CLEAN": 15,
            "max_avg_latency_ms": 1200,
            "min_shadow_good_pct": 0.70,
        },
    },
    3: {
        "name": "guided_retell",
        "label": "Reconto Guiado",
        "description": "Reconta uma história com prompts visuais",
        "exercises": ["story_retell", "image_describe", "guided_qa"],
        "gate": {
            "min_AUTOMATIC_CLEAN": 40,
            "min_biometric_avg": 65,
        },
    },
    4: {
        "name": "constrained_expression",
        "label": "Expressão Guiada",
        "description": "Responde perguntas usando chunks conhecidos",
        "exercises": ["constrained_qa", "chunk_sentence_build", "role_play_guided"],
        "gate": {
            "min_AUTOMATIC_NATIVE": 20,
            "min_biometric_avg": 75,
            "min_output_success": 0.50,
        },
    },
    5: {
        "name": "semi_free",
        "label": "Semi-Livre",
        "description": "Conversa com tópico definido, vocabulário sugerido",
        "exercises": ["topic_conversation", "debate_simple", "narrate_experience"],
        "gate": {
            "min_AUTOMATIC_NATIVE": 50,
            "min_AVAILABLE_OUTPUT": 10,
            "min_biometric_avg": 80,
        },
    },
    6: {
        "name": "free",
        "label": "Livre",
        "description": "Conversa livre, qualquer tópico",
        "exercises": ["free_conversation", "improvise", "storytelling"],
        "gate": {
            "min_AVAILABLE_OUTPUT": 30,
            "min_biometric_avg": 85,
        },
    },
}

# Ordered list of acquisition states from lowest to highest, used for
# cumulative "this state + all higher" gate checks.
_STATE_ORDER = [
    "UNKNOWN",
    "RECOGNIZED",
    "CONTEXT_KNOWN",
    "EFFORTFUL_AUDIO",
    "AUTOMATIC_CLEAN",
    "AUTOMATIC_NATIVE",
    "AVAILABLE_OUTPUT",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cumulative_count(distribution, state):
    """Return the count for *state* plus all states above it.

    For example, _cumulative_count(dist, "AUTOMATIC_CLEAN") returns the sum
    of AUTOMATIC_CLEAN + AUTOMATIC_NATIVE + AVAILABLE_OUTPUT.
    """
    idx = _STATE_ORDER.index(state)
    return sum(distribution.get(s, 0) for s in _STATE_ORDER[idx:])


def _fetch_avg_biometric(conn):
    """Return the average non-null biometric_score from chunk_queue."""
    row = conn.execute(
        "SELECT AVG(biometric_score) FROM chunk_queue "
        "WHERE biometric_score IS NOT NULL"
    ).fetchone()
    return row[0] if row[0] is not None else 0.0


def _fetch_avg_latency(conn):
    """Return the average non-null last_retrieval_latency from chunk_queue."""
    row = conn.execute(
        "SELECT AVG(last_retrieval_latency) FROM chunk_queue "
        "WHERE last_retrieval_latency IS NOT NULL"
    ).fetchone()
    return row[0] if row[0] is not None else 0.0


def _fetch_output_success_avg(conn):
    """Return the average output_success from automaticity_state where output_attempts > 0."""
    row = conn.execute(
        "SELECT AVG(output_success) FROM automaticity_state "
        "WHERE output_attempts > 0"
    ).fetchone()
    return row[0] if row[0] is not None else 0.0


def _fetch_shadow_good_pct(conn):
    """Return the fraction of reviewed chunks with mastery_level >= 2 and current_pass >= 4.

    'Reviewed' means any chunk with last_reviewed IS NOT NULL.
    Returns 0.0 if no chunks have been reviewed.
    """
    total_row = conn.execute(
        "SELECT COUNT(*) FROM chunk_queue WHERE last_reviewed IS NOT NULL"
    ).fetchone()
    total = total_row[0]
    if total == 0:
        return 0.0

    good_row = conn.execute(
        "SELECT COUNT(*) FROM chunk_queue "
        "WHERE mastery_level >= 2 AND current_pass >= 4 "
        "AND last_reviewed IS NOT NULL"
    ).fetchone()
    return good_row[0] / total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_current_stage(db_path=DB_PATH):
    """Return the learner's current speech stage (1-6).

    Queries the ``speech_unlock`` table for the highest stage number.
    If the table is empty, inserts stage 1 (echo) and returns 1.

    Args:
        db_path: Path to the SQLite database. Defaults to DB_PATH.

    Returns:
        int: The current stage number (1-6).
    """
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT MAX(stage) AS max_stage FROM speech_unlock"
    ).fetchone()

    if row["max_stage"] is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO speech_unlock (stage, stage_name, entered_at, criteria_met) "
            "VALUES (?, ?, ?, ?)",
            (1, "echo", now, "{}"),
        )
        conn.commit()
        conn.close()
        return 1

    stage = row["max_stage"]
    conn.close()
    return stage


def evaluate_gates(db_path=DB_PATH):
    """Check whether the current stage's exit gate is met.

    Evaluates every criterion in the current stage's gate dict against
    live data from the database (acquisition state distribution, biometric
    averages, latency, output success, and shadowing quality).

    Args:
        db_path: Path to the SQLite database. Defaults to DB_PATH.

    Returns:
        dict with keys:
            current_stage (int): The learner's current stage.
            current_name (str): The stage's internal name.
            gate_met (bool): True if all exit criteria are satisfied.
            criteria (dict): Per-criterion breakdown with required, actual, met.
            missing (list[str]): Names of criteria not yet met.
    """
    current = get_current_stage(db_path)

    if current == 6:
        return {
            "current_stage": 6,
            "current_name": SPEECH_STAGES[6]["name"],
            "gate_met": False,
            "criteria": {},
            "missing": [],
            "message": "Nível máximo",
        }

    gate = SPEECH_STAGES[current]["gate"]
    distribution = get_state_distribution(db_path)

    conn = get_connection(db_path)
    avg_biometric = _fetch_avg_biometric(conn)
    avg_latency = _fetch_avg_latency(conn)
    output_success_avg = _fetch_output_success_avg(conn)
    shadow_good_pct = _fetch_shadow_good_pct(conn)
    conn.close()

    criteria = {}
    missing = []

    for criterion, required in gate.items():
        actual = None
        met = False

        if criterion == "min_EFFORTFUL_AUDIO":
            actual = _cumulative_count(distribution, "EFFORTFUL_AUDIO")
            met = actual >= required
        elif criterion == "min_AUTOMATIC_CLEAN":
            actual = _cumulative_count(distribution, "AUTOMATIC_CLEAN")
            met = actual >= required
        elif criterion == "min_AUTOMATIC_NATIVE":
            actual = _cumulative_count(distribution, "AUTOMATIC_NATIVE")
            met = actual >= required
        elif criterion == "min_AVAILABLE_OUTPUT":
            actual = distribution.get("AVAILABLE_OUTPUT", 0)
            met = actual >= required
        elif criterion == "min_biometric_avg":
            actual = avg_biometric
            met = actual >= required
        elif criterion == "max_avg_latency_ms":
            actual = avg_latency
            met = actual <= required
        elif criterion == "min_shadow_good_pct":
            actual = shadow_good_pct
            met = actual >= required
        elif criterion == "min_output_success":
            actual = output_success_avg
            met = actual >= required

        criteria[criterion] = {
            "required": required,
            "actual": actual,
            "met": met,
        }
        if not met:
            missing.append(criterion)

    return {
        "current_stage": current,
        "current_name": SPEECH_STAGES[current]["name"],
        "gate_met": len(missing) == 0,
        "criteria": criteria,
        "missing": missing,
    }


def advance_stage(db_path=DB_PATH):
    """Attempt to advance the learner to the next speech stage.

    Calls :func:`evaluate_gates` and, if the exit gate is met, inserts a
    new row into ``speech_unlock`` for the next stage.

    Args:
        db_path: Path to the SQLite database. Defaults to DB_PATH.

    Returns:
        dict with keys:
            advanced (bool): True if the learner was promoted.
            new_stage (int): The new stage number (only if advanced).
            new_name (str): The new stage name (only if advanced).
            reason (str): Explanation if not advanced.
            missing (list[str]): Unmet criteria (only if not advanced).
    """
    result = evaluate_gates(db_path)

    if not result["gate_met"]:
        return {
            "advanced": False,
            "reason": "gate not met",
            "missing": result["missing"],
        }

    new_stage = result["current_stage"] + 1
    new_name = SPEECH_STAGES[new_stage]["name"]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO speech_unlock (stage, stage_name, entered_at, criteria_met) "
        "VALUES (?, ?, ?, ?)",
        (new_stage, new_name, now, json.dumps(result["criteria"])),
    )
    conn.commit()
    conn.close()

    return {
        "advanced": True,
        "new_stage": new_stage,
        "new_name": new_name,
    }


def check_regression(db_path=DB_PATH):
    """Check whether the learner should be regressed to a lower stage.

    Regression fires when any criterion from the *entry* gate (i.e., the
    gate of stage N-1 that was met to reach stage N) now falls below 70%
    of its required value.

    Args:
        db_path: Path to the SQLite database. Defaults to DB_PATH.

    Returns:
        dict with keys:
            regressed (bool): True if the learner was demoted.
            from_stage (int): Previous stage (only if regressed).
            to_stage (int): New stage after demotion (only if regressed).
    """
    current = get_current_stage(db_path)

    if current == 1:
        return {"regressed": False}

    # The gate that was met to ENTER the current stage is the exit gate
    # of the previous stage.
    entry_gate = SPEECH_STAGES[current - 1]["gate"]
    distribution = get_state_distribution(db_path)

    conn = get_connection(db_path)
    avg_biometric = _fetch_avg_biometric(conn)
    avg_latency = _fetch_avg_latency(conn)
    output_success_avg = _fetch_output_success_avg(conn)
    shadow_good_pct = _fetch_shadow_good_pct(conn)

    should_regress = False

    for criterion, required in entry_gate.items():
        actual = None
        threshold = required * 0.70

        if criterion == "min_EFFORTFUL_AUDIO":
            actual = _cumulative_count(distribution, "EFFORTFUL_AUDIO")
            if actual < threshold:
                should_regress = True
        elif criterion == "min_AUTOMATIC_CLEAN":
            actual = _cumulative_count(distribution, "AUTOMATIC_CLEAN")
            if actual < threshold:
                should_regress = True
        elif criterion == "min_AUTOMATIC_NATIVE":
            actual = _cumulative_count(distribution, "AUTOMATIC_NATIVE")
            if actual < threshold:
                should_regress = True
        elif criterion == "min_AVAILABLE_OUTPUT":
            actual = distribution.get("AVAILABLE_OUTPUT", 0)
            if actual < threshold:
                should_regress = True
        elif criterion == "min_biometric_avg":
            if avg_biometric < threshold:
                should_regress = True
        elif criterion == "max_avg_latency_ms":
            # For latency, regression means latency has grown too high.
            # 70% threshold inverted: regress if actual > required / 0.70
            if avg_latency > required / 0.70:
                should_regress = True
        elif criterion == "min_shadow_good_pct":
            if shadow_good_pct < threshold:
                should_regress = True
        elif criterion == "min_output_success":
            if output_success_avg < threshold:
                should_regress = True

        if should_regress:
            break

    if should_regress:
        conn.execute(
            "DELETE FROM speech_unlock WHERE stage = ?", (current,)
        )
        conn.commit()
        conn.close()
        return {
            "regressed": True,
            "from_stage": current,
            "to_stage": current - 1,
        }

    conn.close()
    return {"regressed": False}


def get_stage_info(stage=None):
    """Return speech stage metadata.

    Args:
        stage: An integer 1-6 to retrieve a single stage's dict,
               or None to retrieve all stages.

    Returns:
        dict: A single stage dict if *stage* is provided.
        dict: All stages keyed by stage number if *stage* is None.
    """
    if stage is not None:
        return SPEECH_STAGES.get(stage)
    return SPEECH_STAGES


def get_activities_for_stage(stage=None, db_path=DB_PATH):
    """Return the list of exercise types available for a speech stage.

    Args:
        stage: An integer 1-6, or None to use the learner's current stage.
        db_path: Path to the SQLite database. Defaults to DB_PATH.

    Returns:
        list[str]: Exercise identifiers for the requested stage.
    """
    if stage is None:
        stage = get_current_stage(db_path)
    info = SPEECH_STAGES.get(stage)
    if info is None:
        return []
    return info["exercises"]
