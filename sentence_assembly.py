"""
sentence_assembly.py — Sentence Assembly engine for the Oxe Protocol.

Given 3 chunks the learner knows, they build a grammatically correct sentence.
Tests productive chunk combination, not just recognition.

All GPT-4o prompts are in Portuguese (NUNCA usa inglês).
"""

import json
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

from srs_engine import DB_PATH, get_connection
from dictionary_engine import _chat, generate_tts, SYSTEM_PROMPT
from acquisition_engine import get_items_in_state, STATES, STATE_ORDER

# ── In-memory challenge store (short-lived) ────────────────────
_active_challenges = {}

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Minimum state to be considered "known"
_MIN_STATE = "CONTEXT_KNOWN"
_MIN_ORDER = STATE_ORDER[_MIN_STATE]

# States that qualify as "known" for assembly challenges
_KNOWN_STATES = [s for s in STATES if STATE_ORDER[s] >= _MIN_ORDER]

# Difficulty config: (num_chunks, num_distractors)
DIFFICULTY_CONFIG = {
    "easy": (2, 1),
    "medium": (3, 2),
    "hard": (4, 2),
}


# ── Helpers ────────────────────────────────────────────────────

def _get_known_chunks(db_path=DB_PATH, limit=200):
    """Return chunks the learner knows (state >= CONTEXT_KNOWN).

    Joins automaticity_state with chunk_queue to get the chunk text.
    """
    conn = get_connection(db_path)
    placeholders = ",".join("?" for _ in _KNOWN_STATES)
    rows = conn.execute(
        f"""SELECT a.item_id, cq.target_chunk, a.state, a.confidence
            FROM automaticity_state a
            JOIN chunk_queue cq ON a.item_id = cq.id
            WHERE a.item_type = 'chunk'
              AND a.state IN ({placeholders})
            ORDER BY RANDOM()
            LIMIT ?""",
        (*_KNOWN_STATES, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _record_session_event(event_type, event_data, db_path=DB_PATH):
    """Insert a row into session_events."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO session_events (date, event_type, event_data, timestamp) VALUES (?, ?, ?, ?)",
        (date_str, event_type, json.dumps(event_data, ensure_ascii=False), ts_str),
    )
    conn.commit()
    conn.close()


# ── 1. get_assembly_challenge ──────────────────────────────────

def get_assembly_challenge(difficulty="medium", db_path=DB_PATH):
    """Generate a sentence assembly challenge.

    1. Pick N chunks the learner knows
    2. Call GPT-4o to create a natural sentence using all N chunks,
       plus distractor chunks that DON'T belong
    3. Return challenge dict with shuffled options
    """
    num_chunks, num_distractors = DIFFICULTY_CONFIG.get(difficulty, (3, 2))

    known = _get_known_chunks(db_path, limit=200)
    if len(known) < num_chunks + num_distractors:
        # Not enough known chunks — try with whatever we have
        if len(known) < num_chunks:
            return {
                "error": "Não tem chunks suficientes. Continue treinando!",
                "known_count": len(known),
                "needed": num_chunks,
            }

    # Pick target chunks
    random.shuffle(known)
    target_items = known[:num_chunks]
    target_chunks = [item["target_chunk"] for item in target_items]

    # Build GPT-4o prompt
    difficulty_instruction = {
        "easy": "Usa ordem simples e direta.",
        "medium": "Usa uma frase natural de conversação.",
        "hard": "Usa uma frase mais elaborada, com estrutura complexa.",
    }.get(difficulty, "Usa uma frase natural de conversação.")

    chunks_str = ", ".join(f'"{c}"' for c in target_chunks)
    user_prompt = (
        f"Cria UMA frase natural em português baiano usando TODOS estes chunks: {chunks_str}\n\n"
        f"Também cria {num_distractors} chunks distratores — chunks que parecem caber mas NÃO "
        "fazem parte da frase.\n\n"
        f"Nível: {difficulty_instruction}\n\n"
        "REGRAS:\n"
        "- NUNCA usa inglês\n"
        "- A frase tem que soar natural, como alguém de Salvador falaria\n"
        "- Os chunks devem aparecer na frase numa ordem natural\n"
        "- Os distratores devem ser plausíveis mas não encaixar na frase\n\n"
        "Responde em JSON com:\n"
        '- "frase": a frase completa\n'
        '- "distratores": lista de strings com os chunks distratores\n\n'
        "Responde SOMENTE em JSON."
    )

    result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)

    if result is None:
        return {"error": "Erro ao gerar desafio. Tenta de novo!"}

    correct_sentence = result.get("frase", "")
    distractors = result.get("distratores", [])

    # Ensure we have enough distractors
    while len(distractors) < num_distractors:
        distractors.append("por acaso")

    distractors = distractors[:num_distractors]

    # Generate audio for the correct sentence
    audio_file = generate_assembly_audio(correct_sentence)

    # Shuffle all options
    all_options = list(target_chunks) + list(distractors)
    random.shuffle(all_options)

    challenge_id = str(uuid.uuid4())

    challenge = {
        "challenge_id": challenge_id,
        "target_chunks": target_chunks,
        "distractors": distractors,
        "all_options": all_options,
        "correct_sentence": correct_sentence,
        "audio_file": audio_file,
        "difficulty": difficulty,
    }

    # Store in memory for checking later
    _active_challenges[challenge_id] = challenge

    return challenge


# ── 2. check_assembly ──────────────────────────────────────────

def check_assembly(challenge_id, submitted_order, db_path=DB_PATH):
    """Check the learner's submitted chunk order against the correct answer.

    - Exact match: score 100
    - Correct chunks, wrong order but grammatically valid: score 90
    - Close but not quite: score 70
    - Included a distractor: score 30
    """
    challenge = _active_challenges.get(challenge_id)
    if challenge is None:
        return {
            "correct": False,
            "score": 0,
            "feedback": "Desafio não encontrado. Gera um novo!",
            "correct_sentence": "",
            "audio_file": None,
        }

    target_chunks = challenge["target_chunks"]
    distractors = challenge["distractors"]
    correct_sentence = challenge["correct_sentence"]
    audio_file = challenge["audio_file"]

    # Check if any distractors were included
    submitted_set = set(submitted_order)
    distractor_set = set(distractors)
    included_distractors = submitted_set & distractor_set

    if included_distractors:
        bad_ones = ", ".join(f'"{d}"' for d in included_distractors)
        feedback = f"Eita! {bad_ones} não faz parte da frase. A frase certa é: {correct_sentence}"
        score = 30
        result = {
            "correct": False,
            "score": score,
            "feedback": feedback,
            "correct_sentence": correct_sentence,
            "audio_file": audio_file,
        }
        _record_session_event("sentence_assembly", {
            "challenge_id": challenge_id,
            "score": score,
            "submitted": submitted_order,
            "correct_sentence": correct_sentence,
            "difficulty": challenge["difficulty"],
        }, db_path)
        return result

    # Check if submitted chunks match target chunks (ignoring distractors)
    submitted_targets = [c for c in submitted_order if c not in distractor_set]

    # Exact order match
    if submitted_targets == target_chunks:
        result = {
            "correct": True,
            "score": 100,
            "feedback": "Perfeito! Arretado demais!",
            "correct_sentence": correct_sentence,
            "audio_file": audio_file,
        }
        _record_session_event("sentence_assembly", {
            "challenge_id": challenge_id,
            "score": 100,
            "submitted": submitted_order,
            "correct_sentence": correct_sentence,
            "difficulty": challenge["difficulty"],
        }, db_path)
        return result

    # Same chunks but different order — ask GPT-4o if it's valid
    if set(submitted_targets) == set(target_chunks):
        submitted_sentence_str = " + ".join(f'"{c}"' for c in submitted_targets)
        check_prompt = (
            f"O aprendiz montou esta ordem de chunks: {submitted_sentence_str}\n"
            f"A frase original era: \"{correct_sentence}\"\n"
            f"Os chunks originais na ordem certa: {', '.join(target_chunks)}\n\n"
            "A ordem do aprendiz forma uma frase gramaticalmente correta e natural "
            "em português baiano?\n\n"
            "NUNCA usa inglês. Responde em JSON com:\n"
            '- "valido": true/false\n'
            '- "explicacao": explicação curta em português\n\n'
            "Responde SOMENTE em JSON."
        )
        gpt_result = _chat(SYSTEM_PROMPT, check_prompt, json_mode=True)

        if gpt_result and gpt_result.get("valido", False):
            score = 90
            feedback = gpt_result.get("explicacao", "Boa! Ordem diferente mas funciona.")
            feedback = f"Quase perfeito! {feedback}"
        else:
            score = 70
            explicacao = ""
            if gpt_result:
                explicacao = gpt_result.get("explicacao", "")
            feedback = f"Quase! {explicacao} A frase certa é: {correct_sentence}"

        result = {
            "correct": score >= 90,
            "score": score,
            "feedback": feedback,
            "correct_sentence": correct_sentence,
            "audio_file": audio_file,
        }
        _record_session_event("sentence_assembly", {
            "challenge_id": challenge_id,
            "score": score,
            "submitted": submitted_order,
            "correct_sentence": correct_sentence,
            "difficulty": challenge["difficulty"],
        }, db_path)
        return result

    # Missing chunks or extra chunks
    feedback = f"Tá faltando chunks! A frase certa é: {correct_sentence}"
    result = {
        "correct": False,
        "score": 30,
        "feedback": feedback,
        "correct_sentence": correct_sentence,
        "audio_file": audio_file,
    }
    _record_session_event("sentence_assembly", {
        "challenge_id": challenge_id,
        "score": 30,
        "submitted": submitted_order,
        "correct_sentence": correct_sentence,
        "difficulty": challenge["difficulty"],
    }, db_path)
    return result


# ── 3. get_assembly_stats ──────────────────────────────────────

def get_assembly_stats(days=7, db_path=DB_PATH):
    """Return stats: challenges attempted, average score, most-missed chunks."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT event_data FROM session_events
           WHERE event_type = 'sentence_assembly'
             AND date >= date('now', ? || ' days')
           ORDER BY timestamp DESC""",
        (f"-{days}",),
    ).fetchall()
    conn.close()

    if not rows:
        return {
            "attempts": 0,
            "average_score": 0,
            "perfect_count": 0,
            "most_missed_chunks": [],
        }

    total_score = 0
    perfect = 0
    chunk_misses = {}

    for row in rows:
        data = json.loads(row["event_data"])
        score = data.get("score", 0)
        total_score += score
        if score == 100:
            perfect += 1
        elif score < 90:
            # Track chunks from failed attempts
            submitted = data.get("submitted", [])
            correct = data.get("correct_sentence", "")
            for chunk in submitted:
                if chunk not in correct:
                    chunk_misses[chunk] = chunk_misses.get(chunk, 0) + 1

    # Sort most missed
    sorted_misses = sorted(chunk_misses.items(), key=lambda x: -x[1])[:5]

    return {
        "attempts": len(rows),
        "average_score": round(total_score / len(rows)) if rows else 0,
        "perfect_count": perfect,
        "most_missed_chunks": [{"chunk": c, "misses": n} for c, n in sorted_misses],
    }


# ── 4. generate_assembly_audio ─────────────────────────────────

def generate_assembly_audio(sentence):
    """Generate TTS for the correct sentence using ElevenLabs.

    Reuses generate_tts from dictionary_engine.
    Returns filename or None.
    """
    if not sentence:
        return None
    return generate_tts(sentence)
