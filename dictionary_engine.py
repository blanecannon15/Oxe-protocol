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
    "- Se a palavra tem uso especial na Bahia, menciona isso.\n"
    "- Gírias, interjeições e palavrões: SEMPRE define com riqueza. "
    "Oxe, massa, arretado, vixe, lá ele — tudo tem definição. "
    "NUNCA retorna campos vazios. Se a palavra é gíria, explica como gíria.\n"
    "- GÍRIAS E COLOQUIALISMOS são PRIORIDADE: saca (entendeu?), bagulho (coisa/parada), "
    "véi/vei (amigo/mano), trampo (trabalho), mano, parada, da hora, suave, "
    "zoar, zueira, firmeza, é nóis — SEMPRE inclui o significado gírio PRIMEIRO, "
    "depois o sentido formal se tiver.\n"
    "- Se a palavra tem MÚLTIPLOS sentidos (formal + gíria), lista TODOS. "
    "Exemplo: 'saca' = 1) gíria: 'entende?/sabe?' 2) saco grande de material.\n"
    "- Expressões baianas SEMPRE têm exemplos reais de uso na rua."
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
            model="gpt-4o-mini",
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

def _baiano_tts_text(text):
    """Wrap short text with a Baiano carrier to nudge pronunciation toward
    Soteropolitano.  Carrier sentences (4+ words) already carry enough
    regional context, so they pass through unchanged."""
    words = text.strip().split()
    if len(words) <= 3:
        # Short text (single word / small chunk) — prepend "Oxe, " so the
        # model picks up Baiano cadence and open vowels.
        return f"Oxe, {text}"
    return text


def generate_tts(text, raw=False):
    """Generate ElevenLabs TTS audio with Bahian voice. Returns the filename or None.
    If raw=True, skip the Baiano carrier wrapper (for isolated word playback)."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("[TTS] WARNING: ELEVENLABS_API_KEY not set — no audio will be generated")
        return None
    try:
        from elevenlabs import ElevenLabs
        client = ElevenLabs(api_key=api_key)
        tts_text = text if raw else _baiano_tts_text(text)
        audio_iter = client.text_to_speech.convert(
            text=tts_text,
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
    except Exception as e:
        print(f"[TTS] Error generating audio for '{text[:50]}': {e}")
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


def refresh_word_cache(word_id, word, tabs=None, db_path=DB_PATH):
    """Delete cached entries and regenerate them with the current prompt.

    Args:
        word_id: word_bank ID
        word: the word string
        tabs: list of tab names to refresh, or None for all 7
    """
    all_tabs = ['definition', 'examples', 'pronunciation', 'expressions',
                'conjugation', 'synonyms', 'chunks']
    to_refresh = tabs or all_tabs

    conn = get_connection(db_path)
    for tab in to_refresh:
        conn.execute(
            "DELETE FROM dictionary_cache WHERE word_id = ? AND tab_name = ?",
            (word_id, tab),
        )
    conn.commit()
    conn.close()

    # Regenerate using the cached wrappers (they'll miss cache and call GPT)
    tab_fns = {
        'definition': lambda: get_definition_cached(word_id, word, db_path),
        'examples': lambda: get_examples_cached(word_id, word, db_path),
        'pronunciation': lambda: get_pronunciation_cached(word_id, word, db_path),
        'expressions': lambda: get_expressions_cached(word_id, word, db_path),
        'conjugation': lambda: get_conjugation_cached(word_id, word, db_path),
        'synonyms': lambda: get_synonyms_cached(word_id, word, db_path),
        'chunks': lambda: get_word_chunks_cached(word_id, word, db_path),
    }
    results = {}
    for tab in to_refresh:
        if tab in tab_fns:
            try:
                results[tab] = tab_fns[tab]()
            except Exception as e:
                print(f"[REFRESH] {tab} for {word}: {e}")
    return results


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


# ── 1b. search_chunks ─────────────────────────────────────────────

def search_chunks(query, db_path=DB_PATH):
    """Search chunk_families and chunk_variants for chunks matching query.

    Tries exact -> prefix -> contains (like search_word).
    Returns top 15 results sorted by composite_rank DESC.
    """
    if not query or not query.strip():
        return []

    query = query.strip().lower()
    conn = get_connection(db_path)
    results = []
    seen_ids = set()

    def _fetch_chunks(where_clause, params, limit):
        """Helper to fetch chunk families matching a WHERE clause."""
        rows = conn.execute(
            "SELECT cf.id AS family_id, cf.root_form, cf.word_count, cf.composite_rank, "
            "(SELECT COUNT(*) FROM chunk_variants cv WHERE cv.family_id = cf.id) AS variant_count "
            "FROM chunk_families cf "
            "LEFT JOIN chunk_variants cv ON cv.family_id = cf.id "
            f"WHERE {where_clause} "
            "GROUP BY cf.id "
            "ORDER BY cf.composite_rank DESC "
            f"LIMIT ?",
            (*params, limit),
        ).fetchall()
        return rows

    # Exact match on root_form or variant_form
    rows = _fetch_chunks(
        "LOWER(cf.root_form) = ? OR LOWER(cv.variant_form) = ?",
        (query, query), 15,
    )
    for r in rows:
        if r["family_id"] not in seen_ids:
            seen_ids.add(r["family_id"])
            results.append(dict(r))

    # Prefix match
    if len(results) < 15:
        exclude = ",".join(str(i) for i in seen_ids) if seen_ids else "0"
        rows = _fetch_chunks(
            "(LOWER(cf.root_form) LIKE ? OR LOWER(cv.variant_form) LIKE ?) "
            f"AND cf.id NOT IN ({exclude})",
            (query + "%", query + "%"), 15 - len(results),
        )
        for r in rows:
            if r["family_id"] not in seen_ids:
                seen_ids.add(r["family_id"])
                results.append(dict(r))

    # Contains match
    if len(results) < 15:
        exclude = ",".join(str(i) for i in seen_ids) if seen_ids else "0"
        rows = _fetch_chunks(
            "(LOWER(cf.root_form) LIKE ? OR LOWER(cv.variant_form) LIKE ?) "
            f"AND cf.id NOT IN ({exclude})",
            ("%" + query + "%", "%" + query + "%"), 15 - len(results),
        )
        for r in rows:
            if r["family_id"] not in seen_ids:
                seen_ids.add(r["family_id"])
                results.append(dict(r))

    conn.close()

    return [
        {
            "family_id": r["family_id"],
            "root_form": r["root_form"],
            "word_count": r["word_count"],
            "composite_rank": round(r["composite_rank"], 4) if r["composite_rank"] else 0,
            "variant_count": r["variant_count"] or 0,
        }
        for r in results
    ][:15]


# ── 1c. get_chunk_detail_cached ──────────────────────────────────

CHUNK_SYSTEM_PROMPT = (
    "Tu é um dicionário baiano de chunks e colocações. "
    "Tua função é explicar chunks (frases curtas / colocações) "
    "usando português simples — só as 1000 palavras mais comuns.\n\n"
    "REGRAS:\n"
    "- NUNCA usa inglês. Nem uma palavra.\n"
    "- Explica como um colega de Salvador explicaria.\n"
    "- Se o chunk tem uso especial na Bahia, menciona isso."
)


def _generate_chunk_detail(family_id, root_form, db_path=DB_PATH):
    """Generate full chunk detail: variants from DB + GPT-generated content."""
    detail = {
        "family_id": family_id,
        "root_form": root_form,
        "variants": [],
        "definicao": "",
        "exemplos": [],
        "chunks_relacionados": [],
        "pronuncia": "",
    }

    # Fetch variants from DB
    try:
        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT variant_form, source, occurrence_count "
            "FROM chunk_variants WHERE family_id = ? ORDER BY occurrence_count DESC",
            (family_id,),
        ).fetchall()
        conn.close()
        detail["variants"] = [
            {
                "variant_form": r["variant_form"],
                "source": r["source"],
                "occurrence_count": r["occurrence_count"],
            }
            for r in rows
        ]
    except Exception:
        pass

    # Fetch related words from DB
    try:
        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT wb.id, wb.word FROM chunk_family_words cfw "
            "JOIN word_bank wb ON wb.id = cfw.word_id "
            "WHERE cfw.family_id = ?",
            (family_id,),
        ).fetchall()
        conn.close()
        detail["component_words"] = [
            {"word_id": r["id"], "word": r["word"]} for r in rows
        ]
    except Exception:
        detail["component_words"] = []

    # Call GPT for definition, examples, related chunks, pronunciation
    user_prompt = (
        f'Explica o chunk/colocação "{root_form}".\n\n'
        "NUNCA usa inglês. Responde em JSON com:\n"
        '- "definicao": explicação do significado do chunk inteiro (não palavra por palavra)\n'
        '- "exemplos": lista de 5 frases de exemplo usando o chunk naturalmente. '
        'Cada item: {{"texto": "frase", "contexto": "situação onde se usa"}}\n'
        '- "chunks_relacionados": lista de 3-5 chunks parecidos ou que se usam junto. '
        'Cada item: {{"chunk": "...", "relacao": "sinônimo/variação/complemento"}}\n'
        '- "pronuncia": guia de pronúncia do chunk usando palavras conhecidas\n\n'
        "Responde SOMENTE em JSON."
    )

    result = _chat(CHUNK_SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result:
        detail["definicao"] = result.get("definicao", "")
        detail["pronuncia"] = result.get("pronuncia", "")

        exemplos = result.get("exemplos", [])
        detail["exemplos"] = [
            {
                "texto": ex.get("texto", ""),
                "contexto": ex.get("contexto", ""),
            }
            for ex in exemplos
            if isinstance(ex, dict)
        ][:5]

        relacionados = result.get("chunks_relacionados", [])
        detail["chunks_relacionados"] = [
            {
                "chunk": ch.get("chunk", ""),
                "relacao": ch.get("relacao", ""),
            }
            for ch in relacionados
            if isinstance(ch, dict)
        ][:5]

    return detail


def get_chunk_detail_cached(family_id, root_form, db_path=DB_PATH):
    """Cache-through wrapper for chunk detail.

    Uses dictionary_cache with a 'chunk_detail' tab_name.
    Uses negative family_id to avoid collisions with word_bank IDs.
    """
    cache_id = -family_id  # negative to avoid collision with word_bank ids
    return _cached_call(
        cache_id,
        "chunks",  # reuse existing allowed tab_name
        lambda: _generate_chunk_detail(family_id, root_form, db_path),
        db_path,
    )


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
        f'Dá a pronúncia SOTEROPOLITANA (baiana, de Salvador) da palavra "{word}".\n\n'
        "NUNCA usa inglês. Responde em JSON com:\n"
        '- "silabas": separação silábica (ex: "ba-ru-io")\n'
        '- "guia_fonetico": guia fonético usando a pronúncia REAL de Salvador, '
        "não o português padrão. Regras importantes:\n"
        '  • "lh" em Salvador soa como [j] (semivogal "i"): '
        '"ilha" → "ía", "barulho" → "baruio", "olho" → "oio", "trabalho" → "trabaio"\n'
        '  • Vogais abertas características do baiano: '
        '"e" e "o" pré-tônicos são abertos (ex: "pegar" → "pégar", "correr" → "córrer")\n'
        '  • Ritmo silábico (cada sílaba tem duração parecida), não acentual\n'
        '  • "r" final é aspirado [h]: "falar" → "falah"\n'
        '  • "s" final é [s] (não chiado como no Rio): "mas" → "mas", não "mash"\n\n'
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
            rank = r["composite_rank"] or 0
            freq_label = "alta" if rank >= 0.7 else ("media" if rank >= 0.3 else "baixa")
            chunks_from_db.append({
                "text": r["root_form"],
                "frequencia": freq_label,
                "frequency": round(rank, 2),
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
                        "text": ch.get("chunk", ""),
                        "tipo": ch.get("tipo", ""),
                        "frequencia": ch.get("frequencia", ""),
                    })

    return {
        "chunks_from_db": chunks_from_db,
        "chunks_generated": chunks_generated,
    }


# ── Bulk generation (all 7 tabs in one call) ─────────────────────

def get_all_tabs(word, db_path=DB_PATH):
    """Generate all 7 dictionary tabs in a SINGLE GPT call.

    Returns a dict with keys: definition, examples, pronunciation,
    expressions, conjugation, synonyms, chunks.
    Much faster than 7 separate calls for bulk precaching.
    """
    user_prompt = (
        f'Gera TODOS os dados de dicionário para a palavra "{word}".\n\n'
        "NUNCA usa inglês. Usa estilo baiano de Salvador.\n\n"
        "Responde em JSON com EXATAMENTE estas 7 chaves:\n\n"
        '1. "definition": {{\n'
        '   "definicao": explicação curta usando palavras comuns,\n'
        '   "uso_regional": "baiano" ou "geral",\n'
        '   "exemplo_chunk": frase curta natural usando a palavra\n'
        '}}\n\n'
        '2. "examples": lista de 5 objetos, cada um com:\n'
        '   "texto": frase completa, "chunk_destaque": chunk que contém a palavra\n\n'
        '3. "pronunciation": {{\n'
        '   "silabas": separação silábica com pronúncia SOTEROPOLITANA '
        '(ex: "ba-ru-io", não "ba-ru-lho"),\n'
        '   "guia_fonetico": guia usando a pronúncia REAL de Salvador — '
        '"lh" soa [j] ("ilha"→"ía", "barulho"→"baruio", "olho"→"oio"), '
        'vogais pré-tônicas abertas, "r" final aspirado [h], '
        '"s" final é [s] (não chiado)\n'
        '}}\n\n'
        '4. "expressions": lista de expressões/gírias baianas. Cada:\n'
        '   "expressao", "significado", "exemplo"\n\n'
        '5. "conjugation": se for verbo: {{"is_verb": true, "infinitivo": "...", '
        '"irregular": true/false, "tenses": {{presente, preterito_perfeito, '
        'preterito_imperfeito, futuro_informal, subjuntivo_presente, imperativo}} '
        'cada tempo com: eu, voce, ele, a_gente, voces, eles}}.\n'
        '   Se NÃO for verbo: {{"is_verb": false}}\n\n'
        '6. "synonyms": {{\n'
        '   "sinonimos": lista de 5-8 com "palavra", "nota", "baiano" (bool),\n'
        '   "antonimos": lista de 2-3 com "palavra", "nota",\n'
        '   "palavras_relacionadas": lista de 3-5 com "palavra", "relacao",\n'
        '   "registro": formal/informal/gíria/técnico\n'
        '}}\n\n'
        '7. "chunks": {{\n'
        '   "chunks_generated": lista de 8 chunks de ALTA FREQUÊNCIA (2-4 palavras, '
        'uso diário), cada com "chunk", "tipo", "frequencia": "alta"\n'
        '}}\n\n'
        "Responde SOMENTE em JSON."
    )

    result = _chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
    if result is None:
        return None

    # Normalize the result to match individual function output formats
    tabs = {}

    # Definition
    d = result.get("definition", {})
    tabs["definition"] = {
        "definicao": d.get("definicao", ""),
        "uso_regional": d.get("uso_regional", ""),
        "exemplo_chunk": d.get("exemplo_chunk", ""),
    }

    # Examples
    exs = result.get("examples", [])
    if isinstance(exs, dict):
        exs = exs.get("exemplos", [])
    examples = []
    for ex in exs[:5]:
        if isinstance(ex, dict):
            examples.append({
                "texto": ex.get("texto", ""),
                "chunk_destaque": ex.get("chunk_destaque", ""),
            })
    while len(examples) < 5:
        examples.append({"texto": "", "chunk_destaque": ""})
    tabs["examples"] = examples

    # Pronunciation
    p = result.get("pronunciation", {})
    tabs["pronunciation"] = {
        "silabas": p.get("silabas", ""),
        "guia_fonetico": p.get("guia_fonetico", ""),
        "audio_path": None,
    }

    # Expressions
    exprs = result.get("expressions", [])
    if isinstance(exprs, dict):
        exprs = exprs.get("expressoes", [])
    tabs["expressions"] = [
        {
            "expressao": ex.get("expressao", ""),
            "significado": ex.get("significado", ""),
            "exemplo": ex.get("exemplo", ""),
        }
        for ex in exprs if isinstance(ex, dict)
    ]

    # Conjugation
    conj = result.get("conjugation", {})
    if conj.get("is_verb", False):
        tabs["conjugation"] = {
            "is_verb": True,
            "infinitivo": conj.get("infinitivo", word),
            "irregular": conj.get("irregular", False),
            "tenses": conj.get("tenses", {}),
        }
    else:
        tabs["conjugation"] = {"is_verb": False}

    # Synonyms
    s = result.get("synonyms", {})
    tabs["synonyms"] = {
        "sinonimos": s.get("sinonimos", []),
        "antonimos": s.get("antonimos", []),
        "palavras_relacionadas": s.get("palavras_relacionadas", []),
        "registro": s.get("registro", ""),
    }

    # Chunks
    ch = result.get("chunks", {})
    chunks_gen = ch.get("chunks_generated", ch.get("chunks", []))
    if isinstance(chunks_gen, dict):
        chunks_gen = chunks_gen.get("chunks", [])
    tabs["chunks"] = {
        "chunks_from_db": [],
        "chunks_generated": [
            {
                "chunk": c.get("chunk", ""),
                "tipo": c.get("tipo", ""),
                "frequencia": c.get("frequencia", "alta"),
            }
            for c in chunks_gen if isinstance(c, dict)
        ],
    }

    return tabs


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
    return generate_tts(word, raw=True)


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
