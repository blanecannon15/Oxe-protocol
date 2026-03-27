"""
search_index_builder.py — Populate and query the search_index table.

Builds a normalized index from word_bank, chunk_families, and chunk_variants
for instant prefix-match search across the entire vocabulary.
"""

import unicodedata

from srs_engine import DB_PATH, get_connection


def normalize(text):
    """Strip accents and lowercase for search matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def build_word_index(db_path=DB_PATH):
    """Index all words from word_bank."""
    conn = get_connection(db_path)
    words = conn.execute("SELECT id, word FROM word_bank").fetchall()
    inserted = 0
    for w in words:
        term = w["word"]
        norm = normalize(term)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO search_index
                   (term, normalized, item_type, item_id, source, priority)
                   VALUES (?, ?, 'word', ?, 'lemma', ?)""",
                (term, norm, w["id"], 100 - min(w["id"], 99)),
            )
            inserted += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return inserted


def build_chunk_index(db_path=DB_PATH):
    """Index chunk families and their variants."""
    conn = get_connection(db_path)
    inserted = 0

    # Index chunk_families root forms
    try:
        families = conn.execute("SELECT id, root_form FROM chunk_families").fetchall()
    except Exception:
        families = []

    for f in families:
        term = f["root_form"]
        norm = normalize(term)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO search_index
                   (term, normalized, item_type, item_id, source, priority)
                   VALUES (?, ?, 'chunk', ?, 'root', 80)""",
                (term, norm, f["id"]),
            )
            inserted += 1
        except Exception:
            pass

    # Index chunk_variants
    try:
        variants = conn.execute(
            "SELECT id, family_id, variant_form FROM chunk_variants"
        ).fetchall()
    except Exception:
        variants = []

    for v in variants:
        term = v["variant_form"]
        norm = normalize(term)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO search_index
                   (term, normalized, item_type, item_id, source, priority)
                   VALUES (?, ?, 'variant', ?, 'variant', 60)""",
                (term, norm, v["id"]),
            )
            inserted += 1
        except Exception:
            pass

    conn.commit()
    conn.close()
    return inserted


def build_full_index(db_path=DB_PATH):
    """Build complete search index from all sources."""
    w = build_word_index(db_path)
    c = build_chunk_index(db_path)
    return {"words_indexed": w, "chunks_indexed": c, "total": w + c}


def search(query, limit=20, item_type=None, db_path=DB_PATH):
    """Search the index by prefix match on normalized form."""
    conn = get_connection(db_path)
    norm_q = normalize(query)

    sql = """
        SELECT si.term, si.normalized, si.item_type, si.item_id, si.source, si.priority
        FROM search_index si
        WHERE si.normalized LIKE ?
    """
    params = [f"{norm_q}%"]

    if item_type:
        sql += " AND si.item_type = ?"
        params.append(item_type)

    sql += " ORDER BY si.priority DESC, si.normalized ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def unified_search(query, limit=20, db_path=DB_PATH):
    """Search across words, chunks, and variants — return grouped results."""
    conn = get_connection(db_path)
    norm_q = normalize(query)

    # Words
    word_rows = conn.execute("""
        SELECT si.item_id, si.term, wb.frequency_rank, wb.difficulty_tier,
               wb.mastery_level
        FROM search_index si
        JOIN word_bank wb ON wb.id = si.item_id
        WHERE si.normalized LIKE ? AND si.item_type = 'word'
        ORDER BY si.priority DESC, wb.frequency_rank ASC
        LIMIT ?
    """, (f"{norm_q}%", limit)).fetchall()

    # Chunks
    chunk_rows = conn.execute("""
        SELECT si.item_id, si.term, cf.composite_rank, cf.frequency_score
        FROM search_index si
        JOIN chunk_families cf ON cf.id = si.item_id
        WHERE si.normalized LIKE ? AND si.item_type = 'chunk'
        ORDER BY si.priority DESC, cf.composite_rank DESC
        LIMIT ?
    """, (f"{norm_q}%", limit)).fetchall()

    conn.close()
    return {
        "query": query,
        "words": [dict(r) for r in word_rows],
        "chunks": [dict(r) for r in chunk_rows],
    }
