"""
image_policy.py — Lexical type classification and image generation policy.

Determines whether an image is useful for a given word/chunk based on
lexical type. Images are only generated for concrete nouns, places,
objects, food, and people — everything else gets audio + context variation.

Usage:
    from image_policy import classify_lexical_type, should_generate_image
"""

import json
import os
import re

import openai

from srs_engine import DB_PATH, get_connection


# ---------------------------------------------------------------------------
# Lexical type → image policy mapping
# ---------------------------------------------------------------------------

LEXICAL_TYPES = [
    'concrete_noun', 'person', 'place', 'object', 'food',
    'action_verb', 'abstract_word', 'discourse_marker',
    'slang_expression', 'chunk', 'sentence',
    'emotional_expression', 'idiomatic_expression',
    'connector', 'function_word',
]

# Which lexical types get images
IMAGE_ALLOWED = {
    'concrete_noun':        True,
    'person':               True,
    'place':                True,
    'object':               True,
    'food':                 True,
    'action_verb':          False,  # optional, but default off
    'abstract_word':        False,
    'discourse_marker':     False,
    'slang_expression':     False,
    'chunk':                False,
    'sentence':             False,
    'emotional_expression': False,
    'idiomatic_expression': False,
    'connector':            False,
    'function_word':        False,
}


# ---------------------------------------------------------------------------
# Quick local heuristic (no API call)
# ---------------------------------------------------------------------------

# Common Portuguese function words / connectors / discourse markers
_FUNCTION_WORDS = {
    'o', 'a', 'os', 'as', 'um', 'uma', 'uns', 'umas',
    'de', 'do', 'da', 'dos', 'das', 'em', 'no', 'na', 'nos', 'nas',
    'por', 'pelo', 'pela', 'com', 'sem', 'para', 'pra', 'pro',
    'e', 'ou', 'mas', 'que', 'se', 'como', 'quando', 'porque',
    'não', 'sim', 'já', 'ainda', 'mais', 'menos', 'muito', 'pouco',
    'este', 'esta', 'esse', 'essa', 'aquele', 'aquela',
    'ele', 'ela', 'eles', 'elas', 'eu', 'tu', 'você', 'nós', 'vocês',
    'meu', 'minha', 'seu', 'sua', 'nosso', 'nossa',
    'ao', 'à', 'às', 'aos', 'num', 'numa',
}

_DISCOURSE_MARKERS = {
    'oxe', 'vixe', 'né', 'tipo', 'aí', 'então', 'bom', 'olha',
    'pois', 'aliás', 'enfim', 'entretanto', 'porém', 'contudo',
    'assim', 'portanto', 'logo', 'daí', 'inclusive',
}

_SLANG = {
    'massa', 'arretado', 'barril', 'zuada', 'tá ligado',
    'é mermo', 'lá ele', 'painho', 'mainha', 'véi',
    'de boa', 'suave', 'tranquilo', 'firmeza', 'beleza',
    'mano', 'cara', 'brother', 'parceiro',
}


def _heuristic_classify(text):
    """Fast local classification without API call. Returns lexical_type or None if unsure."""
    text_lower = text.lower().strip()
    words = text_lower.split()

    # Multi-word = chunk or sentence
    if len(words) >= 4:
        return 'sentence'
    if len(words) >= 2:
        return 'chunk'

    # Single word checks
    if text_lower in _FUNCTION_WORDS:
        return 'function_word'
    if text_lower in _DISCOURSE_MARKERS:
        return 'discourse_marker'
    if text_lower in _SLANG:
        return 'slang_expression'

    # Ends in common verb suffixes
    if re.match(r'.*(?:ar|er|ir|ando|endo|indo|ado|ido|ou|ei|amos|emos|imos)$', text_lower):
        return 'action_verb'

    # Can't determine locally
    return None


# ---------------------------------------------------------------------------
# GPT classification (for single words where heuristic is unsure)
# ---------------------------------------------------------------------------

def _gpt_classify(text):
    """Use GPT-4o-mini to classify a word's lexical type. Returns lexical_type string."""
    try:
        client = openai.OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "Classifica a palavra/expressão portuguesa em exatamente UMA dessas categorias: "
                    "concrete_noun, person, place, object, food, action_verb, abstract_word, "
                    "discourse_marker, slang_expression, chunk, sentence, emotional_expression, "
                    "idiomatic_expression, connector, function_word. "
                    "Responda SOMENTE com a categoria, nada mais."
                )},
                {"role": "user", "content": text},
            ],
            max_tokens=20,
            temperature=0.0,
        )
        result = resp.choices[0].message.content.strip().lower().replace(' ', '_')
        if result in LEXICAL_TYPES:
            return result
        return 'abstract_word'  # safe default
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_lexical_type(text, use_gpt=True):
    """Classify a word or chunk into a lexical type.

    Args:
        text: The word or chunk text.
        use_gpt: Whether to use GPT for uncertain cases (default True).

    Returns:
        One of the LEXICAL_TYPES strings.
    """
    result = _heuristic_classify(text)
    if result:
        return result
    if use_gpt:
        gpt_result = _gpt_classify(text)
        if gpt_result:
            return gpt_result
    return 'abstract_word'  # safe default: no image


def should_generate_image(text, lexical_type=None):
    """Determine if an image should be generated for this text.

    Args:
        text: The word or chunk.
        lexical_type: Pre-classified type, or None to auto-classify.

    Returns:
        bool — True if image generation is recommended.
    """
    if lexical_type is None:
        lexical_type = classify_lexical_type(text)
    return IMAGE_ALLOWED.get(lexical_type, False)


def get_image_policy(text, lexical_type=None):
    """Full policy decision with metadata.

    Returns:
        dict with keys: text, lexical_type, image_allowed, reason
    """
    if lexical_type is None:
        lexical_type = classify_lexical_type(text)
    allowed = IMAGE_ALLOWED.get(lexical_type, False)
    reason = 'concrete/visual' if allowed else f'{lexical_type}: audio+context preferred'
    return {
        'text': text,
        'lexical_type': lexical_type,
        'image_allowed': allowed,
        'reason': reason,
    }


# ---------------------------------------------------------------------------
# Batch classification for existing DB
# ---------------------------------------------------------------------------

def classify_chunk_queue(db_path=DB_PATH, limit=0, use_gpt=True):
    """Classify all unclassified chunks in chunk_queue. Updates lexical_type and image_policy columns.

    Returns: dict with counts.
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT id, target_chunk, word_id FROM chunk_queue WHERE lexical_type IS NULL ORDER BY id"
        + (f" LIMIT {limit}" if limit > 0 else "")
    ).fetchall()

    classified = 0
    for row in rows:
        text = row["target_chunk"]
        lt = classify_lexical_type(text, use_gpt=use_gpt)
        policy = 'allowed' if IMAGE_ALLOWED.get(lt, False) else 'suppressed'
        conn.execute(
            "UPDATE chunk_queue SET lexical_type = ?, image_policy = ? WHERE id = ?",
            (lt, policy, row["id"]),
        )
        classified += 1

    conn.commit()
    conn.close()
    return {"classified": classified, "total": len(rows)}
