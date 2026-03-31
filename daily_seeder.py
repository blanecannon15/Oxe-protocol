"""
daily_seeder.py — Automatic daily chunk seeder for Oxe Protocol.

Runs as a background thread on the server. Every 24 hours:
1. Finds the next batch of words by frequency rank that don't have chunks
2. Generates chunks + carrier sentences via GPT
3. Adds them to chunk_queue, prioritized by native speech frequency

Usage:
    from daily_seeder import start_daily_seeder
    start_daily_seeder()  # call from oxe_server.py main()
"""

import json
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone

import openai

from srs_engine import DB_PATH, get_connection, add_chunk


DAILY_BATCH_SIZE = 30  # words per day
SEED_INTERVAL = 86400  # 24 hours


def get_unseeded_words(limit=30, db_path=DB_PATH):
    """Find words by frequency rank that have NO chunks in chunk_queue."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT wb.id, wb.word, wb.frequency_rank, wb.difficulty_tier
           FROM word_bank wb
           WHERE wb.id NOT IN (
               SELECT DISTINCT cq.word_id FROM chunk_queue cq WHERE cq.word_id IS NOT NULL
           )
           ORDER BY wb.frequency_rank ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def generate_chunks_for_words(words):
    """Generate chunk + carrier sentence for each word via GPT.

    Args:
        words: List of dicts with 'word' key.

    Returns:
        List of dicts: {"word", "word_id", "chunk", "carrier"}
    """
    if not words:
        return []

    client = openai.OpenAI()
    all_results = []

    # Process in batches of 15
    for i in range(0, len(words), 15):
        batch = words[i:i + 15]
        word_list = ", ".join(w["word"] for w in batch)

        prompt = (
            f"Gera chunks (collocações naturais) e frases-carregadoras pra cada palavra.\n"
            f"Regras:\n"
            f"- Chunk = 2-4 palavras, collocação natural do dia a dia no Brasil\n"
            f"- Frase-carregadora = 15-30 palavras, conversacional, com gírias e elisões\n"
            f"  (tá, cê, pra, num, né, vamo, oxe, mano, etc)\n"
            f"- Pode ser 2-3 frases conectadas se ficar mais natural\n"
            f"- O chunk TEM que aparecer na frase exatamente como escrito\n"
            f"- Prioriza chunks de alta frequência na fala nativa\n"
            f"- Sem inglês, sem formalidade\n\n"
            f"Palavras: {word_list}\n\n"
            f"Responde em JSON array:\n"
            f'[{{"word": "...", "chunk": "...", "carrier": "..."}}]\n\n'
            f"Gera 1 chunk por palavra. Só o JSON, sem explicação."
        )

        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=3000,
            )
            text = resp.choices[0].message.content.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            batch_results = json.loads(text)

            # Attach word_id from input
            word_id_map = {w["word"].lower(): w["id"] for w in batch}
            for r in batch_results:
                r["word_id"] = word_id_map.get(r.get("word", "").lower())
            all_results.extend(batch_results)
        except Exception as e:
            print(f"  [daily-seeder] GPT batch error: {e}")

    return all_results


def seed_daily_batch(db_path=DB_PATH):
    """Find unseeded words and add chunks to queue. Returns count added."""
    words = get_unseeded_words(DAILY_BATCH_SIZE, db_path)
    if not words:
        print("  [daily-seeder] No unseeded words found — word bank fully seeded!")
        return 0

    print(f"  [daily-seeder] Generating chunks for {len(words)} words "
          f"(ranks {words[0]['frequency_rank']}-{words[-1]['frequency_rank']})...")

    chunks = generate_chunks_for_words(words)

    added = 0
    for c in chunks:
        chunk_text = c.get("chunk", "")
        carrier = c.get("carrier", "")
        word_id = c.get("word_id")
        if not chunk_text or not carrier:
            continue

        result = add_chunk(word_id, chunk_text, carrier, "corpus", db_path)
        if result is not None:
            added += 1

    print(f"  [daily-seeder] Added {added} chunks to queue "
          f"(from {len(words)} words, {len(chunks)} generated)")

    # Log the seeding event
    try:
        conn = get_connection(db_path)
        conn.execute(
            """INSERT INTO session_events (date, event_type, event_data)
               VALUES (?, 'daily_seed', ?)""",
            (
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                json.dumps({
                    "words_checked": len(words),
                    "chunks_generated": len(chunks),
                    "chunks_added": added,
                    "rank_range": [words[0]["frequency_rank"], words[-1]["frequency_rank"]],
                }),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return added


def _seeder_loop():
    """Background loop: seed once on startup, then every 24 hours."""
    time.sleep(30)  # let server finish starting

    while True:
        try:
            # Check if already seeded today
            conn = get_connection(DB_PATH)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            already = conn.execute(
                "SELECT id FROM session_events WHERE date = ? AND event_type = 'daily_seed'",
                (today,),
            ).fetchone()
            conn.close()

            if already:
                print(f"  [daily-seeder] Already seeded today ({today}), skipping")
            else:
                added = seed_daily_batch()
                print(f"  [daily-seeder] Daily seed complete: {added} new chunks ({today})")
        except Exception as e:
            print(f"  [daily-seeder] Error: {e}")
            traceback.print_exc()

        time.sleep(SEED_INTERVAL)


def start_daily_seeder():
    """Start the background daily seeder thread."""
    t = threading.Thread(target=_seeder_loop, daemon=True, name="daily-seeder")
    t.start()
    print("  [daily-seeder] Background seeder started (30 words/day by frequency)")
    return t
