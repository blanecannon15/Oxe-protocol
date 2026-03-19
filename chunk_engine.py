"""
chunk_engine.py — Chunk extraction, family grouping, and ranking engine for the Oxe Protocol.

Extracts multiword chunks from stories and podcasts via GPT-4o,
groups them into families (root_form + variants), ranks by composite score,
and feeds top-ranked families into the SRS chunk_queue for review.

Usage:
    from chunk_engine import extract_chunks_from_story, rank_chunk_families, get_next_chunks_for_srs
"""

import json
import os
import re
import sqlite3

import openai

from srs_engine import DB_PATH, get_connection, add_chunk


# ---------------------------------------------------------------------------
# GPT-4o chunk extraction
# ---------------------------------------------------------------------------

def extract_chunks_from_text(text, min_words=2, max_words=6, exclude_roots=None):
    """Use GPT-4o to extract multiword chunks from a text passage.

    Args:
        text: The source text (Portuguese) to extract chunks from.
        min_words: Minimum number of words per chunk.
        max_words: Maximum number of words per chunk.
        exclude_roots: Optional set of root_forms to exclude from results.

    Returns:
        List of dicts, each with keys: chunk, root_form, word_count, is_baiano, bahia_relevance.
        Returns empty list on failure.
    """
    client = openai.OpenAI()

    system_prompt = (
        "Tu é um linguista computacional baiano especialista em aquisição de língua. "
        "Extrai TODAS as colocações e chunks (unidades multipalavra) do texto. "
        "NUNCA usa inglês."
    )

    exclude_block = ""
    if exclude_roots:
        sample = list(exclude_roots)[:50]
        exclude_block = (
            f"\n\nJÁ CONHEÇO ESSES — NÃO repita nenhum deles:\n"
            + ", ".join(f'"{r}"' for r in sample) + "\n"
        )

    user_prompt = (
        f"Extrai TODOS os chunks (colocações, expressões, frases feitas, verbos com complemento, "
        f"preposições fixas) do texto abaixo.\n"
        f"Cada chunk deve ter entre {min_words} e {max_words} palavras.\n\n"
        f"TIPOS que quero:\n"
        f"- Colocações verbais: 'dar um rolê', 'tomar um banho', 'fazer questão'\n"
        f"- Expressões populares: 'por conta de', 'na hora de', 'do jeito que'\n"
        f"- Gírias/slang baiano: 'massa demais', 'barril dobrado', 'tá ligado'\n"
        f"- Frases de transição: 'aí depois', 'quando eu vi', 'no final das contas'\n"
        f"- Verbo + preposição fixa: 'pensar em', 'gostar de', 'lidar com'\n"
        f"- Quantificadores/hedges: 'um bocado de', 'um monte de', 'mais ou menos'\n\n"
        f"REGRAS:\n"
        f"- MÍNIMO 15 chunks por texto (se o texto tiver material suficiente)\n"
        f"- Prioriza colocações naturais do dia a dia\n"
        f"- IGNORA nomes próprios isolados (mas aceita 'ir pro Pelô', 'lá na Barra')\n"
        f"- NUNCA retorna chunks de 1 palavra\n"
        f"- Cada chunk deve ser algo que um aprendiz de português PRECISA saber como unidade\n"
        f"{exclude_block}\n"
        f"Retorna SOMENTE um JSON array. Cada elemento deve ter:\n"
        f'- "chunk": a forma exata encontrada no texto\n'
        f'- "root_form": a forma canônica/base (ex: "por causa de", "tá ligado")\n'
        f'- "word_count": número de palavras\n'
        f'- "is_baiano": true se o chunk é típico do dialeto baiano/soteropolitano\n'
        f'- "bahia_relevance": 0-100 score de quão relevante o chunk é pro falar baiano/soteropolitano (100 = gíria exclusiva de Salvador, 0 = português genérico sem cor regional)\n\n'
        f"Texto:\n{text}"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        # Handle both {"chunks": [...]} and bare [...]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("chunks", "resultado", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # If dict has the expected keys, wrap it
            if "chunk" in data:
                return [data]
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Database helpers — chunk families & variants
# ---------------------------------------------------------------------------

def _upsert_chunk_family(root_form, word_count, is_baiano, bahia_relevance=None, db_path=DB_PATH):
    """Insert or get an existing chunk_family by root_form.

    Args:
        root_form: Canonical/base form of the chunk.
        word_count: Number of words in the chunk.
        is_baiano: Whether the chunk is Baiano-specific.
        bahia_relevance: Optional 0-100 score from GPT. Falls back to 80/20 heuristic.
        db_path: Path to the SQLite database.

    Returns:
        The family_id (integer).
    """
    conn = get_connection(db_path)
    if bahia_relevance is None:
        bahia_relevance = 80.0 if is_baiano else 20.0

    row = conn.execute(
        "SELECT id FROM chunk_families WHERE root_form = ?", (root_form,)
    ).fetchone()

    if row:
        family_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO chunk_families (root_form, word_count, bahia_relevance)
               VALUES (?, ?, ?)""",
            (root_form, word_count, bahia_relevance),
        )
        family_id = cur.lastrowid

    # Link component words to family
    words = root_form.lower().split()
    for w in words:
        word_row = conn.execute(
            "SELECT id FROM word_bank WHERE LOWER(word) = ?", (w,)
        ).fetchone()
        if word_row:
            wid = word_row[0] if not isinstance(word_row, dict) else word_row["id"]
            conn.execute(
                "INSERT OR IGNORE INTO chunk_family_words (family_id, word_id) VALUES (?, ?)",
                (family_id, wid),
            )

    conn.commit()
    conn.close()
    return family_id


def _upsert_chunk_variant(family_id, variant_form, source, source_id=None, db_path=DB_PATH):
    """Insert or increment occurrence_count for a chunk variant.

    Args:
        family_id: The parent chunk_family ID.
        variant_form: The exact surface form of the variant.
        source: One of 'story', 'podcast', 'conversation', 'corpus', 'manual', 'llm'.
        source_id: Optional ID of the source item (story or podcast ID).
        db_path: Path to the SQLite database.
    """
    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO chunk_variants (family_id, variant_form, source, source_id)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(family_id, variant_form)
           DO UPDATE SET occurrence_count = occurrence_count + 1""",
        (family_id, variant_form, source, source_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Content extraction — stories & podcasts
# ---------------------------------------------------------------------------

def extract_chunks_from_story(story_id, db_path=DB_PATH):
    """Extract chunks from a story and upsert families/variants.

    Args:
        story_id: The story_library.id to extract from.
        db_path: Path to the SQLite database.

    Returns:
        Count of new variants added.
    """
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT body FROM story_library WHERE id = ?", (story_id,)
    ).fetchone()

    # Get existing root_forms to pass as exclusion list
    existing = conn.execute("SELECT root_form FROM chunk_families").fetchall()
    exclude_roots = {r["root_form"] for r in existing}
    conn.close()

    if not row:
        return 0

    chunks = extract_chunks_from_text(row["body"], exclude_roots=exclude_roots)
    added = 0

    for chunk_data in chunks:
        root_form = chunk_data.get("root_form", chunk_data.get("chunk", ""))
        variant_form = chunk_data.get("chunk", root_form)
        word_count = chunk_data.get("word_count", len(root_form.split()))
        is_baiano = chunk_data.get("is_baiano", False)
        bahia_rel = chunk_data.get("bahia_relevance")

        family_id = _upsert_chunk_family(root_form, word_count, is_baiano, bahia_relevance=bahia_rel, db_path=db_path)
        _upsert_chunk_variant(family_id, variant_form, "story", story_id, db_path)
        added += 1

    return added


def extract_chunks_from_podcast(podcast_id, db_path=DB_PATH):
    """Extract chunks from a podcast and upsert families/variants.

    Args:
        podcast_id: The podcast_library.id to extract from.
        db_path: Path to the SQLite database.

    Returns:
        Count of new variants added.
    """
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT body FROM podcast_library WHERE id = ?", (podcast_id,)
    ).fetchone()
    conn.close()

    if not row:
        return 0

    chunks = extract_chunks_from_text(row["body"])
    added = 0

    for chunk_data in chunks:
        root_form = chunk_data.get("root_form", chunk_data.get("chunk", ""))
        variant_form = chunk_data.get("chunk", root_form)
        word_count = chunk_data.get("word_count", len(root_form.split()))
        is_baiano = chunk_data.get("is_baiano", False)
        bahia_rel = chunk_data.get("bahia_relevance")

        family_id = _upsert_chunk_family(root_form, word_count, is_baiano, bahia_relevance=bahia_rel, db_path=db_path)
        _upsert_chunk_variant(family_id, variant_form, "podcast", podcast_id, db_path)
        added += 1

    return added


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_chunk_families(db_path=DB_PATH):
    """Recompute composite_rank for ALL chunk families.

    Composite formula:
        0.40 * normalized_frequency
      + 0.25 * (naturalness_score / 100)
      + 0.20 * (bahia_relevance / 100)
      + 0.15 * min(variant_count / 5, 1.0)

    Updates chunk_families.frequency_score and chunk_families.composite_rank.
    """
    conn = get_connection(db_path)

    # Step 1: compute max occurrence across all families (for normalization)
    max_row = conn.execute(
        """SELECT MAX(total_occ) AS max_occ FROM (
               SELECT SUM(cv.occurrence_count) AS total_occ
               FROM chunk_variants cv
               GROUP BY cv.family_id
           )"""
    ).fetchone()
    max_freq = max_row["max_occ"] if max_row and max_row["max_occ"] else 0

    # Step 2: compute per-family scores
    families = conn.execute(
        """SELECT cf.id, cf.naturalness_score, cf.bahia_relevance,
                  COALESCE(SUM(cv.occurrence_count), 0) AS frequency_score,
                  COUNT(cv.id) AS variant_count
           FROM chunk_families cf
           LEFT JOIN chunk_variants cv ON cv.family_id = cf.id
           GROUP BY cf.id"""
    ).fetchall()

    for fam in families:
        frequency_score = fam["frequency_score"]
        normalized_freq = frequency_score / max_freq if max_freq > 0 else 1.0
        variant_count = fam["variant_count"]
        naturalness = fam["naturalness_score"]
        bahia = fam["bahia_relevance"]

        composite_rank = (
            0.40 * normalized_freq
            + 0.25 * (naturalness / 100.0)
            + 0.20 * (bahia / 100.0)
            + 0.15 * min(variant_count / 5.0, 1.0)
        )

        conn.execute(
            """UPDATE chunk_families
               SET frequency_score = ?, composite_rank = ?
               WHERE id = ?""",
            (frequency_score, composite_rank, fam["id"]),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# SRS queue feeding
# ---------------------------------------------------------------------------

def get_next_chunks_for_srs(limit=10, db_path=DB_PATH):
    """Return top-ranked chunk families not yet in the chunk_queue.

    Excludes families whose root_form already appears as target_chunk in chunk_queue.
    For each family, picks the variant with the highest occurrence_count.

    Args:
        limit: Maximum number of families to return.
        db_path: Path to the SQLite database.

    Returns:
        List of dicts: {"family_id", "root_form", "best_variant", "composite_rank"}.
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT cf.id AS family_id, cf.root_form, cf.composite_rank,
                  (SELECT cv.variant_form
                   FROM chunk_variants cv
                   WHERE cv.family_id = cf.id
                   ORDER BY cv.occurrence_count DESC
                   LIMIT 1) AS best_variant
           FROM chunk_families cf
           WHERE cf.root_form NOT IN (SELECT target_chunk FROM chunk_queue)
           ORDER BY cf.composite_rank DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()

    return [
        {
            "family_id": r["family_id"],
            "root_form": r["root_form"],
            "best_variant": r["best_variant"],
            "composite_rank": r["composite_rank"],
        }
        for r in rows
    ]


def _generate_carrier_sentences(chunks, batch_size=20):
    """Generate natural Baiano carrier sentences for a batch of chunks via GPT.

    Args:
        chunks: List of chunk strings.
        batch_size: Max chunks per GPT call.

    Returns:
        Dict mapping chunk string → carrier sentence.
    """
    client = openai.OpenAI()
    results = {}

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {c}" for j, c in enumerate(batch))

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": (
                        "Tu é um baiano de Salvador. Gera frases naturais do dia a dia "
                        "usando cada chunk dado. As frases devem soar como conversa real "
                        "de rua em Salvador — usa gírias, interjeições (oxe, eita, vixe, "
                        "meu rei, véi), locais de Salvador. Cada frase 8-15 palavras. "
                        "NUNCA usa inglês."
                    )},
                    {"role": "user", "content": (
                        f"Gera UMA frase natural pra cada chunk. O chunk DEVE aparecer "
                        f"na frase exatamente como está.\n\n"
                        f"Retorna JSON: {{\"sentences\": [\"frase1\", \"frase2\", ...]}}\n\n"
                        f"Chunks:\n{numbered}"
                    )},
                ],
                temperature=0.8,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            sentences = data.get("sentences", [])
            for j, chunk_str in enumerate(batch):
                if j < len(sentences):
                    results[chunk_str] = sentences[j]
                else:
                    results[chunk_str] = f"Oxe, {chunk_str} — tá ligado?"
        except Exception:
            for chunk_str in batch:
                results[chunk_str] = f"Oxe, {chunk_str} — tá ligado?"

    return results


def add_chunks_to_queue(chunk_list, db_path=DB_PATH):
    """Add chunks to the SRS chunk_queue with GPT-generated carrier sentences.

    For each chunk, finds a matching word_id from word_bank and generates
    a natural Baiano carrier sentence via GPT.

    Args:
        chunk_list: List of dicts from get_next_chunks_for_srs().
        db_path: Path to the SQLite database.

    Returns:
        Count of chunks successfully added to the queue.
    """
    if not chunk_list:
        return 0

    conn = get_connection(db_path)

    # Collect chunk strings for batch carrier generation
    chunk_strings = []
    chunk_meta = []
    for chunk in chunk_list:
        variant = chunk.get("best_variant") or chunk.get("root_form", "")
        root_form = chunk.get("root_form", variant)

        # Find matching word_id from root_form words
        word_id = None
        for token in root_form.split():
            row = conn.execute(
                "SELECT id FROM word_bank WHERE LOWER(word) = ? LIMIT 1", (token.lower(),)
            ).fetchone()
            if row:
                word_id = row["id"]
                break

        chunk_strings.append(variant)
        chunk_meta.append({"variant": variant, "word_id": word_id})

    conn.close()

    # Generate carrier sentences in batch
    carriers = _generate_carrier_sentences(chunk_strings)

    added = 0
    for meta in chunk_meta:
        carrier = carriers.get(meta["variant"], f"Oxe, {meta['variant']} — tá ligado?")
        result = add_chunk(meta["word_id"], meta["variant"], carrier, "corpus", db_path)
        if result is not None:
            added += 1

    return added


# ---------------------------------------------------------------------------
# Variant queries
# ---------------------------------------------------------------------------

def get_family_variants(family_id, db_path=DB_PATH):
    """Return all chunk_variants for a family, ordered by occurrence_count DESC.

    Args:
        family_id: The chunk_family ID.
        db_path: Path to the SQLite database.

    Returns:
        List of dicts with variant details.
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT id, family_id, variant_form, source, source_id,
                  occurrence_count, created_at
           FROM chunk_variants
           WHERE family_id = ?
           ORDER BY occurrence_count DESC""",
        (family_id,),
    ).fetchall()
    conn.close()

    return [dict(r) for r in rows]


def get_chunks_for_word(word_id, db_path=DB_PATH):
    """Return chunk families that contain the given word.

    Args:
        word_id: The word_bank id.
        db_path: Path to the SQLite database.

    Returns:
        List of dicts with family_id, root_form, composite_rank.
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT cf.id, cf.root_form, cf.composite_rank
           FROM chunk_family_words cfw
           JOIN chunk_families cf ON cf.id = cfw.family_id
           WHERE cfw.word_id = ?
           ORDER BY cf.composite_rank DESC""",
        (word_id,),
    ).fetchall()
    conn.close()
    return [{"family_id": r[0], "root_form": r[1], "composite_rank": r[2]} for r in rows]
