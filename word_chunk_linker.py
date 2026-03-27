"""
word_chunk_linker.py — Auto-link words to chunks.

When a word is added to SRS, this module finds or generates a chunk
containing that word and creates a word_chunk_links record.
"""

import json
import os

from srs_engine import DB_PATH, get_connection


def link_word_to_existing_chunks(word_id, db_path=DB_PATH):
    """Find existing chunks containing this word and create links."""
    conn = get_connection(db_path)
    word_row = conn.execute("SELECT word FROM word_bank WHERE id=?", (word_id,)).fetchone()
    if not word_row:
        conn.close()
        return 0

    word = word_row["word"].lower()
    linked = 0

    # Search chunk_queue for chunks containing this word
    try:
        chunks = conn.execute(
            "SELECT id, target_chunk FROM chunk_queue WHERE LOWER(target_chunk) LIKE ?",
            (f"%{word}%",),
        ).fetchall()
    except Exception:
        chunks = []

    for c in chunks:
        # Verify it's a real word match (not substring)
        tokens = c["target_chunk"].lower().split()
        if word not in tokens:
            continue
        try:
            conn.execute(
                """INSERT OR IGNORE INTO word_chunk_links
                   (word_id, chunk_id, link_type)
                   VALUES (?, ?, 'auto')""",
                (word_id, c["id"]),
            )
            linked += 1
        except Exception:
            pass

    # Also search chunk_families
    try:
        families = conn.execute(
            "SELECT id, root_form FROM chunk_families WHERE LOWER(root_form) LIKE ?",
            (f"%{word}%",),
        ).fetchall()
    except Exception:
        families = []

    for f in families:
        tokens = f["root_form"].lower().split()
        if word not in tokens:
            continue
        try:
            conn.execute(
                """INSERT OR IGNORE INTO word_chunk_links
                   (word_id, family_id, link_type)
                   VALUES (?, ?, 'extracted')""",
                (word_id, f["id"]),
            )
            linked += 1
        except Exception:
            pass

    conn.commit()
    conn.close()
    return linked


def auto_generate_chunk_for_word(word_id, db_path=DB_PATH):
    """Use GPT-4o to generate a natural chunk containing this word, then link it."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    conn = get_connection(db_path)
    word_row = conn.execute("SELECT word FROM word_bank WHERE id=?", (word_id,)).fetchone()
    if not word_row:
        conn.close()
        return None

    word = word_row["word"]

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        resp = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.7,
            messages=[
                {"role": "system", "content": (
                    "Você é um linguista brasileiro. Gere 1 chunk natural (2-4 palavras) "
                    "que contenha a palavra alvo. O chunk deve ser uma colocação ou expressão "
                    "frequente no português brasileiro falado. Também gere 1 frase-veículo curta "
                    "(carrier sentence) usando o chunk. Responda em JSON:\n"
                    '{"chunk": "...", "carrier_sentence": "..."}'
                )},
                {"role": "user", "content": f"Palavra alvo: {word}"},
            ],
        )

        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fence if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        chunk_text = data.get("chunk", "")
        carrier = data.get("carrier_sentence", "")

        if not chunk_text:
            conn.close()
            return None

        # Insert into chunk_queue if not exists
        existing = conn.execute(
            "SELECT id FROM chunk_queue WHERE target_chunk = ?", (chunk_text,)
        ).fetchone()

        if existing:
            chunk_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO chunk_queue (word_id, target_chunk, carrier_sentence, source)
                   VALUES (?, ?, ?, 'auto')""",
                (word_id, chunk_text, carrier),
            )
            chunk_id = cur.lastrowid

        # Create link
        conn.execute(
            """INSERT OR IGNORE INTO word_chunk_links
               (word_id, chunk_id, link_type)
               VALUES (?, ?, 'auto')""",
            (word_id, chunk_id),
        )
        conn.commit()
        conn.close()
        return {"chunk": chunk_text, "carrier_sentence": carrier, "chunk_id": chunk_id}

    except Exception as e:
        print(f"[word_chunk_linker] Error: {e}")
        conn.close()
        return None


def link_word(word_id, db_path=DB_PATH):
    """Full linkage: try existing chunks first, then generate if none found."""
    linked = link_word_to_existing_chunks(word_id, db_path)
    if linked == 0:
        result = auto_generate_chunk_for_word(word_id, db_path)
        if result:
            linked = 1
    return linked


def get_word_chunks(word_id, db_path=DB_PATH):
    """Return all chunks linked to a word."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT wcl.*, cq.target_chunk, cq.carrier_sentence, cf.root_form
        FROM word_chunk_links wcl
        LEFT JOIN chunk_queue cq ON cq.id = wcl.chunk_id
        LEFT JOIN chunk_families cf ON cf.id = wcl.family_id
        WHERE wcl.word_id = ?
    """, (word_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bulk_link_unlinked(limit=100, db_path=DB_PATH):
    """Link words that have no chunk links yet. Returns count linked."""
    conn = get_connection(db_path)
    unlinked = conn.execute("""
        SELECT wb.id FROM word_bank wb
        LEFT JOIN word_chunk_links wcl ON wcl.word_id = wb.id
        WHERE wcl.id IS NULL
        ORDER BY wb.frequency_rank ASC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    linked = 0
    for row in unlinked:
        n = link_word_to_existing_chunks(row["id"], db_path)
        if n > 0:
            linked += 1
    return linked
