"""
training_modes.py — Training mode definitions and item-to-mode mapping for the Oxe Protocol.

Maps each item's automaticity state (and optional fragility) to the optimal drill mode.
The frontend uses get_drill_config() to adapt the UI for each mode.

Usage:
    from training_modes import select_mode_for_item, get_drill_config, get_available_modes
"""

from srs_engine import DB_PATH, get_connection
from acquisition_engine import STATES as _STATES_LIST, STATE_ORDER as STATES, get_or_create_state


# ---------------------------------------------------------------------------
# Training mode definitions
# ---------------------------------------------------------------------------

TRAINING_MODES = {
    "audio_meaning_recognition": {
        "label": "Reconhecimento Auditivo",
        "description": "Ouve o áudio, identifica o significado",
        "min_state": "UNKNOWN",
        "max_state": "EFFORTFUL_AUDIO",
        "audio_type": "clean",
        "measures_latency": True,
        "measures_biometric": False,
        "show_image": True,
        "show_text": False,
        "fragility_types": [],
    },
    "chunk_recognition": {
        "label": "Reconhecimento de Chunk",
        "description": "Ouve um chunk no contexto, identifica",
        "min_state": "RECOGNIZED",
        "max_state": "AUTOMATIC_CLEAN",
        "audio_type": "clean",
        "measures_latency": True,
        "measures_biometric": False,
        "show_image": True,
        "show_text": False,
        "fragility_types": [],
    },
    "next_phrase_prediction": {
        "label": "Previsão de Frase",
        "description": "Ouve o início, prevê o próximo chunk",
        "min_state": "CONTEXT_KNOWN",
        "max_state": "AUTOMATIC_CLEAN",
        "audio_type": "clean",
        "measures_latency": True,
        "measures_biometric": False,
        "show_image": False,
        "show_text": False,
        "fragility_types": [],
    },
    "clean_vs_native_comparison": {
        "label": "Limpo vs Nativo",
        "description": "Ouve limpo depois nativo, identifica o mesmo chunk",
        "min_state": "AUTOMATIC_CLEAN",
        "max_state": "AUTOMATIC_NATIVE",
        "audio_type": "both",
        "measures_latency": True,
        "measures_biometric": False,
        "show_image": True,
        "show_text": False,
        "fragility_types": ["clean_audio_only"],
    },
    "fragile_rescue_drill": {
        "label": "Resgate de Frágeis",
        "description": "Multi-exposição intensiva para itens frágeis",
        "min_state": "RECOGNIZED",
        "max_state": "EFFORTFUL_AUDIO",
        "audio_type": "clean",
        "measures_latency": True,
        "measures_biometric": False,
        "show_image": True,
        "show_text": False,
        "fragility_types": ["familiar_but_fragile", "text_only"],
    },
    "known_but_slow_drill": {
        "label": "Treino de Velocidade",
        "description": "Reconhecimento rápido, latência é a métrica",
        "min_state": "EFFORTFUL_AUDIO",
        "max_state": "AUTOMATIC_CLEAN",
        "audio_type": "clean",
        "measures_latency": True,
        "measures_biometric": False,
        "show_image": True,
        "show_text": False,
        "fragility_types": ["known_but_slow"],
    },
    "shadow_linked_vocab": {
        "label": "Vocabulário com Sombra",
        "description": "5-pass shadowing com áudio nativo + biometria",
        "min_state": "AUTOMATIC_CLEAN",
        "max_state": "AVAILABLE_OUTPUT",
        "audio_type": "native",
        "measures_latency": True,
        "measures_biometric": True,
        "show_image": True,
        "show_text": True,
        "fragility_types": ["blocked_by_prosody"],
    },
    "native_speed_parsing": {
        "label": "Parsing Nativo",
        "description": "Compreensão em velocidade nativa",
        "min_state": "AUTOMATIC_CLEAN",
        "max_state": "AUTOMATIC_NATIVE",
        "audio_type": "native",
        "measures_latency": True,
        "measures_biometric": False,
        "show_image": True,
        "show_text": False,
        "fragility_types": ["clean_audio_only"],
    },
    "output_production_drill": {
        "label": "Produção",
        "description": "Produz o chunk com pronúncia correta",
        "min_state": "AUTOMATIC_NATIVE",
        "max_state": "AVAILABLE_OUTPUT",
        "audio_type": "output",
        "measures_latency": True,
        "measures_biometric": True,
        "show_image": True,
        "show_text": False,
        "fragility_types": [],
    },
}


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

def _state_in_range(state, min_state, max_state):
    """Check whether a state falls within [min_state, max_state] inclusive."""
    idx = STATES.get(state, -1)
    return STATES.get(min_state, 0) <= idx <= STATES.get(max_state, len(STATES))


def select_mode_for_item(item_type, item_id, db_path=DB_PATH):
    """Select the best training mode for a single item.

    Logic:
        1. Get the item's automaticity state.
        2. Check for active fragilities (fragile_items WHERE resolved_at IS NULL).
        3. If fragile: find a mode whose fragility_types includes the fragility_type
           AND the item's state is within the mode's [min_state, max_state] range.
        4. If no fragility match: find the mode whose state range includes the current
           state, preferring the one where the item state is closest to max_state
           (pushing toward advancement).
        5. Default to 'audio_meaning_recognition' if nothing matches.

    Args:
        item_type: 'word' or 'chunk'.
        item_id: The item's database ID.
        db_path: Path to the SQLite database.

    Returns:
        Mode key string (e.g. 'chunk_recognition').
    """
    state_row = get_or_create_state(item_type, item_id, db_path)
    state = state_row["state"] if isinstance(state_row, dict) else state_row
    state_idx = STATES.get(state, 0)

    # Check for active fragilities
    conn = get_connection(db_path)
    fragilities = conn.execute(
        """SELECT fragility_type FROM fragile_items
           WHERE item_type = ? AND item_id = ? AND resolved_at IS NULL""",
        (item_type, item_id),
    ).fetchall()
    conn.close()

    fragility_types = [r["fragility_type"] for r in fragilities]

    # If fragile, try to match a fragility-specific mode
    if fragility_types:
        for mode_key, mode_def in TRAINING_MODES.items():
            if not mode_def["fragility_types"]:
                continue
            for ft in fragility_types:
                if ft in mode_def["fragility_types"]:
                    if _state_in_range(state, mode_def["min_state"], mode_def["max_state"]):
                        return mode_key

    # No fragility match — pick mode by state range, closest to max_state
    best_mode = None
    best_distance = float("inf")

    for mode_key, mode_def in TRAINING_MODES.items():
        if not _state_in_range(state, mode_def["min_state"], mode_def["max_state"]):
            continue
        # Distance from current state to mode's max_state (smaller = closer to advancing)
        max_idx = STATES.get(mode_def["max_state"], 0)
        distance = max_idx - state_idx
        if distance < best_distance:
            best_distance = distance
            best_mode = mode_key

    return best_mode or "audio_meaning_recognition"


def select_mode_for_block(items, db_path=DB_PATH):
    """Select the best training mode for a block of items via majority vote.

    Args:
        items: List of dicts, each with 'item_type' and 'item_id'.
        db_path: Path to the SQLite database.

    Returns:
        Mode key string chosen by majority vote.
    """
    from collections import Counter

    if not items:
        return "audio_meaning_recognition"

    votes = Counter()
    for item in items:
        mode = select_mode_for_item(item["item_type"], item["item_id"], db_path)
        votes[mode] += 1

    return votes.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Drill configuration
# ---------------------------------------------------------------------------

def get_drill_config(mode, item=None):
    """Return the drill configuration dict for a mode.

    This is what the frontend uses to adapt the UI (show/hide image, text,
    set response time limits, etc.).

    Args:
        mode: A TRAINING_MODES key string.
        item: Optional item dict (reserved for future per-item overrides).

    Returns:
        Dict with UI configuration for the drill.
    """
    mode_def = TRAINING_MODES[mode]
    return {
        "mode": mode,
        "label": mode_def["label"],
        "audio_type": mode_def["audio_type"],
        "show_image": mode_def["show_image"],
        "show_text": mode_def["show_text"],
        "measures_latency": mode_def["measures_latency"],
        "measures_biometric": mode_def["measures_biometric"],
        "max_response_time_ms": 3000 if mode == "known_but_slow_drill" else 5000,
    }


# ---------------------------------------------------------------------------
# Stage-gated availability
# ---------------------------------------------------------------------------

def get_available_modes(speech_stage=1):
    """Return which mode keys are available given the speech unlock stage.

    Stage 1-2: audio_meaning_recognition, chunk_recognition, fragile_rescue_drill
    Stage 3:   + next_phrase_prediction, known_but_slow_drill, clean_vs_native_comparison
    Stage 4+:  + shadow_linked_vocab, native_speed_parsing, output_production_drill

    Args:
        speech_stage: Current speech unlock stage (1-6).

    Returns:
        List of mode key strings available at this stage.
    """
    modes = [
        "audio_meaning_recognition",
        "chunk_recognition",
        "fragile_rescue_drill",
    ]

    if speech_stage >= 3:
        modes.extend([
            "next_phrase_prediction",
            "known_but_slow_drill",
            "clean_vs_native_comparison",
        ])

    if speech_stage >= 4:
        modes.extend([
            "shadow_linked_vocab",
            "native_speed_parsing",
            "output_production_drill",
        ])

    return modes
