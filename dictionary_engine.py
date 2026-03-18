"""
dictionary_engine.py — Dictionary Engine for the Oxe Protocol.

Handles word search, definition generation, example sentences,
pronunciation data, and expressions — all in Português Simples,
Baiano style. Imported by oxe_server.py.

All definitions and explanations use GPT-4o with strict "NUNCA usa inglês"
enforcement. Audio is generated via ElevenLabs TTS.
"""

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import openai

from srs_engine import DB_PATH, get_connection, add_chunk

# Reuse TTS infrastructure from drill_server
AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ── GPT-4o System Prompt ────────────────────────────────────────────

SYSTEM_PROMPT = (
    "Tu é um dicionário baiano. Tua função é explicar palavras usando "
    "português simples — só as 1000 palavras mais comuns do português brasileiro.\n\n"
    "REGRAS:\n"
    "- NUNCA usa inglês. Nem uma palavra.\n"
    "- Explica como um colega de Salvador explicaria — natural, direto, sem frescura.\n"
    "- Sempre mostra a palavra dentro de uma frase curta "
    '(um "chunk" natural, não a palavra sozinha).\n'
    "- Se a palavra tem uso especial na Bahia, menciona isso."
)

# ── OpenAI Client ───────────────────────────────────────────────────

def _get_openai_client():
    """Return an OpenAI client. Keys come from the environment."""
    return openai.OpenAI()


def _chat(system, user, json_mode=True):
    """Send a chat completion to GPT-4o and return the parsed result.

    Returns the parsed JSON dict when json_mode=True, or the raw text
    string when json_mode=False. Returns None on any failure.
    """
    try:
        client = _get_openai_client()
        kwargs = dict(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.7,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content
        if json_mode:
            return json.loads(text)
        return text
    except Exception:
        return None


# ── TTS Helper ──────────────────────────────────────────────────────

BAIANO_VOICE_ID = "ELBrtmIkk40wCZ5YnlwM"  # Thiago — native Brazilian male, warm and inviting

def generate_tts(text):
    """Generate ElevenLabs TTS audio with Bahian voice. Returns the filename or None."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None
    try:
        from elevenlabs import ElevenLabs
        client = ElevenLabs(api_key=api_key)
        audio_iter = client.text_to_speech.convert(
            text=text,
            voice_id=BAIANO_VOICE_ID,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
            voice_settings={
                "stability": 0.45,
                "similarity_boost": 0.85,
                "style": 0.55,
                "use_speaker_boost": True,
            },
        )
        fname = f"dict_tts_{int(time.time() * 1000)}.mp3"
        outpath = AUDIO_DIR / fname
        with open(outpath, "wb") as f:
            for chunk in audio_iter:
                f.write(chunk)
        return fname
    except Exception:
        return None


# ── Cache-through wrapper ──────────────────────────────────────────

def _ensure_cache_table(db_path=DB_PATH):
    """Create dictionary_cache table if it doesn't exist yet."""
    try:
        conn = get_connection(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dictionary_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id INTEGER NOT NULL,
                tab_name TEXT NOT NULL CHECK(tab_name IN ('definition','examples','pronunciation','expressions','conjugation','synonyms','chunks')),
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                UNIQUE(word_id, tab_name)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dict_cache ON dictionary_cache(word_id)")
        conn.commit()
        conn.close()
    except Exception:
        pass

_cache_table_ensured = False


def _cached_call(word_id, tab_name, generate_fn, db_path=DB_PATH):
    """Cache-through wrapper for dictionary tab data.

    1. Checks dictionary_cache for existing data.
    2. If found, returns parsed JSON instantly.
    3. If not, calls generate_fn(), stores result in cache, returns it.
    """
    global _cache_table_ensured
    if not _cache_table_ensured:
        _ensure_cache_table(db_path)
        _cache_table_ensured = True

    # Check cache
    try:
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT data_json FROM dictionary_cache WHERE word_id = ? AND tab_name = ?",
            (word_id, tab_name),
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row["data_json"])
    except Exception:
        pass

    # Generate fresh data
    result = generate_fn()

    # Store in cache
    try:
        conn = get_connection(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO dictionary_cache (word_id, tab_name, data_json) VALUES (?, ?, ?)",
            (word_id, tab_name, json.dumps(result, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return result


# ── 1. search_word ──────────────────────────────────────────────────

def search_word(query, db_path=DB_PATH):
    """Fuzzy search against word_bank.

    Tries exact → prefix → contains. Returns top 10 results sorted by
    frequency_rank (most common first).
    """
    if not query or not query.strip():
        return []

    query = query.strip().lower()
    conn = get_connection(db_path)

    # Exact match
    rows = conn.execute(
        "SELECT id, word, frequency_rank, difficulty_tier "
        "FROM word_bank WHERE LOWER(word) = ? ORDER BY frequency_rank LIMIT 10",
        (query,),
    ).fetchall()

    # Prefix match (fill up to 10)
    if len(rows) < 10:
        existing_ids = {r["id"] for r in rows}
        prefix_rows = conn.execute(
            "SELECT id, word, frequency_rank, difficulty_tier "
            "FROM word_bank WHERE LOWER(word) LIKE ? ORDER BY frequency_rank LIMIT ?",
            (query + "%", 10 - len(rows)),
        ).fetchall()
        for r in prefix_rows:
            if r["id"] not in existing_ids:
                rows.append(r)
                existing_ids.add(r["id"])

    # Contains match (fill up to 10)
    if len(rows) < 10:
        existing_ids = {r["id"] for r in rows}
        contains_rows = conn.execute(
            "SELECT id, word, frequency_rank, difficulty_tier "
            "FROM word_bank WHERE LOWER(word) LIKE ? ORDER BY frequency_rank LIMIT ?",
            ("%" + query + "%", 10 - len(rows)),
        ).fetchall()
        for r in contains_rows:
            if r["id"] not in existing_ids:
                rows.append(r)
                existing_ids.add(r["id"])

    conn.close()

    # Build results preserving match-type priority:
    # exact (0) → prefix (1) → contains (2), then frequency within each group
    results = []
    for r in rows:
        w = r["word"].lower()
        if w == query:
            match_type = 0
        elif w.startswith(query):
            match_type = 1
        else:
            match_type = 2
        results.append({
            "word_id": r["id"],
            "word": r["word"],
            "frequency_rank": r["frequency_rank"],
            "difficulty_tier": r["difficulty_tier"],
            "_match_type": match_type,
        })
    results.sort(key=lambda x: (x["_match_type"], x["frequency_rank"]))
    for r in results:
        del r["_match_type"]

    # If no exact match found, offer a live GPT lookup option
    has_exact = any(r["word"].lower() == query for r in results)
    if not has_exact:
        results.insert(0, {
            "word_id": -1,
            "word": query,
            "frequency_rank": 0,
            "difficulty_tier": 0,
            "is_live_lookup": True,
        })

    return results[:10]


# ── 2. get_definition ───────────────────────────────────────────────

def get_definition(word, db_path=DB_PATH):
    """Call GPT-4o to generate a definition in Português Simples.

    Returns a dict with keys: definicao, uso_regional, exemplo_chunk.
    On failure, returns a fallback dict with empty strings.
    """
    fallback = {"definicao": "", "uso_regional": "", "exemplo_chunk": ""}

    user_prompt = (
        f'Explica a palavra "{word}" em português simples.\n\n'
        "NUNCA usa inglês. Responde em JSON com estas chaves:\n"
        '- "definicao": explicação curta usando só palavras comuns\n'
        '- "uso_regional": "baiano" se tem uso especial na Bahia, senão "geral"\n'
        '- "exemplo_chunk": uma frase curta natural usando a palavra como chunk\n\n'
        "Responde SOMENTE em JSON."
    )

    result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result is None:
        return fallback

    # Ensure expected keys exist
    return {
        "definicao": result.get("definicao", ""),
        "uso_regional": result.get("uso_regional", ""),
        "exemplo_chunk": result.get("exemplo_chunk", ""),
    }


# ── 3. get_examples ────────────────────────────────────────────────

def get_examples(word, db_path=DB_PATH):
    """Generate 5 example sentences using the word as a chunk.

    Checks story_library first for existing sentences, then fills the
    rest with GPT-4o generated examples (Baiano style, chunk-focused).
    """
    examples = []

    # Pull sentences from story_library
    try:
        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT body FROM story_library WHERE body LIKE ?",
            ("%" + word + "%",),
        ).fetchall()
        conn.close()

        for row in rows:
            body = row["body"]
            # Split into sentences and find ones containing the word
            sentences = re.split(r'(?<=[.!?])\s+', body)
            for sentence in sentences:
                if word.lower() in sentence.lower() and len(sentence) > 10:
                    # Extract the chunk containing the word
                    chunk = _extract_chunk(sentence, word)
                    examples.append({
                        "texto": sentence.strip(),
                        "chunk_destaque": chunk,
                    })
                    if len(examples) >= 2:
                        break
            if len(examples) >= 2:
                break
    except Exception:
        pass  # story_library may not exist yet

    # Generate remaining examples via GPT-4o
    remaining = 5 - len(examples)
    if remaining > 0:
        user_prompt = (
            f'Gera {remaining} frases de exemplo usando a palavra "{word}" '
            "como parte de um chunk natural (não a palavra sozinha).\n\n"
            "NUNCA usa inglês. Usa estilo baiano — como se fosse um colega "
            "de Salvador falando.\n\n"
            "Responde em JSON com a chave \"exemplos\" contendo uma lista. "
            "Cada item tem:\n"
            '- "texto": a frase completa\n'
            '- "chunk_destaque": o chunk dentro da frase que contém a palavra\n\n'
            "Responde SOMENTE em JSON."
        )

        result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
        if result and "exemplos" in result:
            for ex in result["exemplos"]:
                if len(examples) >= 5:
                    break
                examples.append({
                    "texto": ex.get("texto", ""),
                    "chunk_destaque": ex.get("chunk_destaque", ""),
                })

    # If we still have fewer than 5, pad with empty placeholders
    while len(examples) < 5:
        examples.append({"texto": "", "chunk_destaque": ""})

    return examples[:5]


def _extract_chunk(sentence, word):
    """Extract a short chunk (2-5 words) from a sentence around the target word."""
    words = sentence.split()
    lower_words = [w.lower().strip(".,!?;:\"'()") for w in words]
    target = word.lower()

    idx = None
    for i, w in enumerate(lower_words):
        if target in w:
            idx = i
            break

    if idx is None:
        return word

    start = max(0, idx - 1)
    end = min(len(words), idx + 3)
    return " ".join(words[start:end]).strip(".,!?;:\"'()")


# ── 4. get_pronunciation_data ───────────────────────────────────────

def get_pronunciation_data(word):
    """Generate pronunciation info via GPT-4o.

    Returns: {"silabas": "...", "guia_fonetico": "...", "audio_path": None}
    """
    fallback = {"silabas": "", "guia_fonetico": "", "audio_path": None}

    user_prompt = (
        f'Dá a pronúncia da palavra "{word}".\n\n'
        "NUNCA usa inglês. Responde em JSON com:\n"
        '- "silabas": separação silábica (ex: "ba-ru-lho")\n'
        '- "guia_fonetico": guia usando palavras conhecidas '
        '(ex: "ba como \'bala\', ru como \'rua\', lho como \'olho\'")\n\n'
        "Responde SOMENTE em JSON."
    )

    result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result is None:
        return fallback

    return {
        "silabas": result.get("silabas", ""),
        "guia_fonetico": result.get("guia_fonetico", ""),
        "audio_path": None,  # filled later when TTS is generated
    }


# ── 5. get_expressions ─────────────────────────────────────────────

def get_expressions(word):
    """Generate related expressions via GPT-4o.

    Returns list of: {"expressao": "...", "significado": "...", "exemplo": "..."}
    All in Portuguese, Baiano flavor.
    """
    user_prompt = (
        f'Lista expressões, gírias baianas, e colocações que usam a palavra "{word}" '
        "ou têm relação com ela.\n\n"
        "NUNCA usa inglês. Usa estilo baiano.\n\n"
        'Responde em JSON com a chave "expressoes" contendo uma lista. '
        "Cada item tem:\n"
        '- "expressao": a expressão ou gíria\n'
        '- "significado": o que significa em português simples\n'
        '- "exemplo": uma frase de exemplo usando a expressão\n\n'
        "Responde SOMENTE em JSON."
    )

    result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result is None:
        return []

    expressions = result.get("expressoes", [])
    return [
        {
            "expressao": ex.get("expressao", ""),
            "significado": ex.get("significado", ""),
            "exemplo": ex.get("exemplo", ""),
        }
        for ex in expressions
        if isinstance(ex, dict)
    ]


# ── 6. get_conjugation ─────────────────────────────────────────────

def get_conjugation(word, db_path=DB_PATH):
    """Generate verb conjugation tables via GPT-4o.

    Returns conjugation data for 6 tenses with all 6 persons each,
    using Baiano conjugation patterns. If the word is not a verb,
    returns {"is_verb": false}.
    """
    fallback = {"is_verb": False}

    user_prompt = (
        f'A palavra "{word}" é um verbo? Se NÃO for verbo, responde '
        '{"is_verb": false} e nada mais.\n\n'
        "Se FOR verbo, conjuga nos 6 tempos abaixo usando padrões baianos "
        "(ex: \"tu vai\" em vez de \"tu vais\", \"a gente fala\" em vez de \"nós falamos\").\n\n"
        "Tempos:\n"
        "1. presente\n"
        "2. preterito_perfeito (passado simples)\n"
        "3. preterito_imperfeito\n"
        "4. futuro_informal (forma \"vou + infinitivo\" — como baiano fala de verdade)\n"
        "5. subjuntivo_presente\n"
        "6. imperativo\n\n"
        "Para cada tempo, dá as 6 pessoas: eu, voce, ele, a_gente, voces, eles.\n\n"
        "NUNCA usa inglês. Responde em JSON com:\n"
        '- "is_verb": true\n'
        '- "infinitivo": o infinitivo do verbo\n'
        '- "irregular": true/false\n'
        '- "tenses": objeto com os 6 tempos, cada um com as 6 pessoas\n\n'
        "Responde SOMENTE em JSON."
    )

    result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result is None:
        return fallback

    if not result.get("is_verb", False):
        return {"is_verb": False}

    return {
        "is_verb": True,
        "infinitivo": result.get("infinitivo", word),
        "irregular": result.get("irregular", False),
        "tenses": result.get("tenses", {}),
    }


# ── 7. get_synonyms ──────────────────────────────────────────────

def get_synonyms(word, db_path=DB_PATH):
    """Generate synonyms and thesaurus data via GPT-4o.

    Returns sinonimos, antonimos, palavras_relacionadas, and registro.
    Each synonym includes usage notes and Bahia prevalence.
    """
    fallback = {
        "sinonimos": [],
        "antonimos": [],
        "palavras_relacionadas": [],
        "registro": "",
    }

    user_prompt = (
        f'Dá sinônimos e informações de tesauro para a palavra "{word}".\n\n'
        "NUNCA usa inglês. Responde em JSON com:\n"
        '- "sinonimos": lista de 5-8 sinônimos. Cada item tem:\n'
        '  - "palavra": o sinônimo\n'
        '  - "nota": diferença de uso em poucas palavras\n'
        '  - "baiano": true se é mais comum na Bahia, false se não\n'
        '- "antonimos": lista de 2-3 antônimos. Cada item tem:\n'
        '  - "palavra": o antônimo\n'
        '  - "nota": breve explicação\n'
        '- "palavras_relacionadas": lista de 3-5 palavras semanticamente '
        "relacionadas. Cada item tem:\n"
        '  - "palavra": a palavra\n'
        '  - "relacao": tipo de relação (ex: substantivo derivado, verbo base)\n'
        '- "registro": nível de formalidade da palavra original '
        "(formal/informal/gíria/técnico)\n\n"
        "Responde SOMENTE em JSON."
    )

    result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result is None:
        return fallback

    return {
        "sinonimos": result.get("sinonimos", []),
        "antonimos": result.get("antonimos", []),
        "palavras_relacionadas": result.get("palavras_relacionadas", []),
        "registro": result.get("registro", ""),
    }


# ── 8. get_word_chunks ───────────────────────────────────────────

def get_word_chunks(word, db_path=DB_PATH):
    """Get high-frequency chunks and collocations containing this word.

    First queries chunk_families + chunk_variants tables for existing
    chunks, then generates additional ones via GPT-4o if fewer than 8
    found in the DB.
    """
    chunks_from_db = []

    try:
        conn = get_connection(db_path)
        # Search chunk_families for root forms containing the word
        rows = conn.execute(
            "SELECT cf.root_form, cf.composite_rank, cv.source "
            "FROM chunk_families cf "
            "JOIN chunk_variants cv ON cv.family_id = cf.id "
            "WHERE LOWER(cf.root_form) LIKE ? OR LOWER(cv.variant_form) LIKE ? "
            "GROUP BY cf.id "
            "ORDER BY cf.composite_rank DESC "
            "LIMIT 10",
            ("%" + word.lower() + "%", "%" + word.lower() + "%"),
        ).fetchall()
        conn.close()

        for r in rows:
            chunks_from_db.append({
                "chunk": r["root_form"],
                "frequency": round(r["composite_rank"], 2),
                "source": r["source"],
            })
    except Exception:
        pass  # tables may not exist or be empty yet

    chunks_generated = []
    if len(chunks_from_db) < 8:
        needed = 8 - len(chunks_from_db)
        user_prompt = (
            f'Gera {needed} chunks/colocações de ALTA FREQUÊNCIA que usam a palavra "{word}".\n\n'
            "REGRAS IMPORTANTES:\n"
            "- SÓ chunks que um brasileiro ouviria TODO DIA em conversa normal\n"
            "- Prioriza uso baiano/soteropolitano — como se fosse alguém de Salvador falando\n"
            "- NUNCA gera chunks raros, formais, ou literários\n"
            "- Cada chunk deve ter 2-4 palavras (curto e natural)\n"
            "- NUNCA usa inglês\n\n"
            'Responde em JSON com a chave "chunks" contendo uma lista. '
            "Cada item tem:\n"
            '- "chunk": o chunk/colocação (2-4 palavras, alta frequência)\n'
            '- "tipo": "colocação", "expressão", ou "phrasal"\n'
            '- "frequencia": DEVE ser "alta" — só gera chunks de alta frequência\n\n'
            "Responde SOMENTE em JSON."
        )

        result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
        if result and "chunks" in result:
            for ch in result["chunks"]:
                if isinstance(ch, dict):
                    chunks_generated.append({
                        "chunk": ch.get("chunk", ""),
                        "tipo": ch.get("tipo", ""),
                        "frequencia": ch.get("frequencia", ""),
                    })

    return {
        "chunks_from_db": chunks_from_db,
        "chunks_generated": chunks_generated,
    }


# ── Cached wrappers ───────────────────────────────────────────────

def get_definition_cached(word_id, word, db_path=DB_PATH):
    """Cache-through wrapper for get_definition."""
    return _cached_call(word_id, "definition", lambda: get_definition(word, db_path), db_path)


def get_examples_cached(word_id, word, db_path=DB_PATH):
    """Cache-through wrapper for get_examples."""
    return _cached_call(word_id, "examples", lambda: get_examples(word, db_path), db_path)


def get_pronunciation_cached(word_id, word, db_path=DB_PATH):
    """Cache-through wrapper for get_pronunciation_data."""
    return _cached_call(word_id, "pronunciation", lambda: get_pronunciation_data(word), db_path)


def get_expressions_cached(word_id, word, db_path=DB_PATH):
    """Cache-through wrapper for get_expressions."""
    return _cached_call(word_id, "expressions", lambda: get_expressions(word), db_path)


def get_conjugation_cached(word_id, word, db_path=DB_PATH):
    """Cache-through wrapper for get_conjugation."""
    return _cached_call(word_id, "conjugation", lambda: get_conjugation(word, db_path), db_path)


def get_synonyms_cached(word_id, word, db_path=DB_PATH):
    """Cache-through wrapper for get_synonyms."""
    return _cached_call(word_id, "synonyms", lambda: get_synonyms(word, db_path), db_path)


def get_word_chunks_cached(word_id, word, db_path=DB_PATH):
    """Cache-through wrapper for get_word_chunks."""
    return _cached_call(word_id, "chunks", lambda: get_word_chunks(word, db_path), db_path)


# ── 9. get_audio_for_word ────────────────────────────────────────

def get_audio_for_word(word):
    """Generate TTS audio for the word itself.

    Uses the existing generate_tts() function. Returns the filename
    or None if generation fails.
    """
    return generate_tts(word)


# ── 10. get_full_word_data ─────────────────────────────────────────

def get_full_word_data(word_id, db_path=DB_PATH):
    """Combine all dictionary data into one response for the API.

    Looks up the word by ID, then calls get_definition, get_examples,
    get_pronunciation_data, and get_expressions. Also generates TTS
    audio for the word and caches it.
    """
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT id, word, frequency_rank, difficulty_tier FROM word_bank WHERE id = ?",
        (word_id,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    word = row["word"]

    definition = get_definition_cached(word_id, word, db_path)
    examples = get_examples_cached(word_id, word, db_path)
    pronunciation = get_pronunciation_cached(word_id, word, db_path)
    expressions = get_expressions_cached(word_id, word, db_path)
    conjugation = get_conjugation_cached(word_id, word, db_path)
    synonyms = get_synonyms_cached(word_id, word, db_path)
    chunks = get_word_chunks_cached(word_id, word, db_path)

    # Generate TTS audio for the word itself and cache it
    audio_fname = generate_tts(word)
    if audio_fname:
        pronunciation["audio_path"] = audio_fname

    # If we got a valid example chunk from the definition, add it to the SRS queue
    exemplo_chunk = definition.get("exemplo_chunk", "")
    if exemplo_chunk:
        carrier = f"Oxe, {exemplo_chunk} — tá ligado?"
        add_chunk(word_id, exemplo_chunk, carrier, "dictionary", db_path)

    return {
        "word_id": row["id"],
        "word": word,
        "frequency_rank": row["frequency_rank"],
        "difficulty_tier": row["difficulty_tier"],
        "definicao": definition,
        "exemplos": examples,
        "pronuncia": pronunciation,
        "expressoes": expressions,
        "conjugacao": conjugation,
        "sinonimos": synonyms,
        "chunks": chunks,
    }


# ── 11. log_search ─────────────────────────────────────────────────

def log_search(query, word_id=None, chunk_id=None, db_path=DB_PATH):
    """Insert a record into the search_history table."""
    try:
        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO search_history (query, word_id, chunk_id) VALUES (?, ?, ?)",
            (query, word_id, chunk_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # never crash the server for a logging failure
