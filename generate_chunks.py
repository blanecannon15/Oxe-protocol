"""
generate_chunks.py — Generate high-frequency PT-BR chunks from word_bank via GPT.

Instead of extracting from stories (limited overlap), this generates common
collocations, verb phrases, and expressions using top-frequency words as seeds.

Usage:
    python3 generate_chunks.py                       # generate from top 500 words
    python3 generate_chunks.py --word-count 1000     # generate from top 1000
    python3 generate_chunks.py --tier 1              # only Tier 1 words
    python3 generate_chunks.py --status              # show chunk stats
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import openai

from chunk_engine import (
    _upsert_chunk_family,
    _upsert_chunk_variant,
    rank_chunk_families,
    get_next_chunks_for_srs,
    add_chunks_to_queue,
)
from srs_engine import DB_PATH, get_connection


def generate_chunks_for_words(words, existing_roots, batch_size=20):
    """Generate chunks for a batch of seed words via GPT-4o-mini.

    Args:
        words: List of seed word strings.
        existing_roots: Set of root_forms already in DB (for dedup).
        batch_size: Words per GPT call.

    Returns:
        List of chunk dicts.
    """
    client = openai.OpenAI()
    all_chunks = []

    for i in range(0, len(words), batch_size):
        batch = words[i:i + batch_size]
        word_list = ", ".join(batch)

        # Sample of existing to exclude
        exclude_sample = list(existing_roots)[:80]
        exclude_block = ""
        if exclude_sample:
            exclude_block = (
                "\n\nJÁ TENHO ESSES — NÃO repita:\n"
                + ", ".join(f'"{r}"' for r in exclude_sample) + "\n"
            )

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": (
                        "Tu é um linguista baiano especialista em colocações e chunks "
                        "do português brasileiro FALADO. Gera chunks de alta frequência que "
                        "um aprendiz PRECISA saber como unidades inseparáveis. "
                        "OBRIGATÓRIO usar elisões e contrações nativas da fala real: "
                        "tá, tô, tava, tão, cê, ocê, né, pro, pra, prum, pros, pras, "
                        "num, numa, dum, duma, cum, vamo, bora, vambora, ó, ói, "
                        "memo, mermo, dimais, mai, tamém, daí, aí, sacou, ligou, "
                        "tipo, tipo assim, sei lá, faz tempo, já era, de boas, suave, "
                        "pode crer, fala aí, manda ver, tô nem aí, tanto faz, "
                        "peraí, belê, cadê, que isso, imagina, e aí, qual é, "
                        "deixa eu, fica de boa, sossegado, que nada, acho que. "
                        "NUNCA escreva formas completas como 'está', 'estou', 'estava', "
                        "'estão', 'você', 'para o', 'para a', 'também', 'mesmo', "
                        "'demais', 'vamos'. Sempre a forma falada. NUNCA usa inglês."
                    )},
                    {"role": "user", "content": (
                        f"Pra cada palavra abaixo, gera 3-5 chunks (colocações naturais) "
                        f"que um brasileiro usa TODO DIA. Cada chunk 2-6 palavras.\n\n"
                        f"TIPOS:\n"
                        f"- Verbo + complemento: 'dar um rolê', 'tomar banho', 'fazer questão'\n"
                        f"- Expressões fixas: 'por causa de', 'na hora de', 'em vez de'\n"
                        f"- Gírias baianas: 'massa dimais', 'é mermo', 'tá de boa', 'fica suave'\n"
                        f"- Marcadores discursivos: 'tipo assim', 'sabe como é', 'aí né', 'sacou'\n"
                        f"- Verbo + preposição: 'pensar em', 'gostar de', 'lidar cum'\n"
                        f"- Filler/conectores: 'sei lá', 'pode crer', 'tô nem aí', 'tanto faz'\n"
                        f"- Contrações: 'vamo lá', 'bora', 'peraí', 'cadê', 'fala aí'\n\n"
                        f"REGRAS:\n"
                        f"- SÓ chunks que existem como UNIDADE na fala real\n"
                        f"- NUNCA chunks de 1 palavra\n"
                        f"- Prioriza uso informal/falado, não literário\n"
                        f"- Inclui variantes baianas quando existem\n"
                        f"{exclude_block}\n"
                        f"Palavras: {word_list}\n\n"
                        f"Retorna JSON: {{\"chunks\": [{{\"chunk\": \"...\", \"root_form\": \"...\", "
                        f"\"word_count\": N, \"seed_word\": \"...\", \"is_baiano\": bool, "
                        f"\"bahia_relevance\": 0-100}}]}}"
                    )},
                ],
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            chunks = data.get("chunks", [])

            # Filter out single-word and already-known
            for c in chunks:
                root = c.get("root_form", c.get("chunk", ""))
                wc = c.get("word_count", len(root.split()))
                if wc >= 2 and root.lower() not in {r.lower() for r in existing_roots}:
                    c["word_count"] = wc
                    all_chunks.append(c)
                    existing_roots.add(root)

        except Exception as e:
            print(f"  [ERROR] Batch {i}-{i+batch_size}: {e}", flush=True)

        time.sleep(0.3)

    return all_chunks


def main():
    parser = argparse.ArgumentParser(
        description="Generate high-frequency PT-BR chunks from word_bank seeds."
    )
    parser.add_argument("--word-count", type=int, default=500,
                        help="Number of top-frequency words to use as seeds (default: 500)")
    parser.add_argument("--tier", type=int, default=None,
                        help="Limit to specific tier (default: all unlocked)")
    parser.add_argument("--seed-count", type=int, default=200,
                        help="Number of top chunks to seed into SRS queue (default: 200)")
    parser.add_argument("--status", action="store_true",
                        help="Show chunk stats and exit")
    parser.add_argument("--seed-only", action="store_true",
                        help="Skip generation, just rank and seed")

    args = parser.parse_args()

    if args.status:
        from extract_all_chunks import show_status
        show_status()
        return

    conn = get_connection()

    if not args.seed_only:
        # Get seed words
        query = "SELECT word FROM word_bank"
        params = []
        if args.tier:
            query += " WHERE difficulty_tier = ?"
            params.append(args.tier)
        query += " ORDER BY frequency_rank ASC LIMIT ?"
        params.append(args.word_count)

        words = [r["word"] for r in conn.execute(query, params).fetchall()]

        # Get existing roots for dedup
        existing = conn.execute("SELECT root_form FROM chunk_families").fetchall()
        existing_roots = {r["root_form"] for r in existing}
        conn.close()

        print(f"Generating chunks from top {len(words)} words...")
        print(f"Existing families: {len(existing_roots)}")
        print("=" * 60)

        start = time.time()

        # Process in parallel batches
        batch_size = 20
        total_new = 0
        all_chunks = []

        # Split words into groups of batch_size for GPT calls
        # Use 3 parallel GPT calls at a time
        word_batches = [words[i:i+batch_size] for i in range(0, len(words), batch_size)]

        for batch_idx in range(0, len(word_batches), 3):
            parallel_batches = word_batches[batch_idx:batch_idx + 3]

            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(generate_chunks_for_words, wb, existing_roots, batch_size): wb
                    for wb in parallel_batches
                }
                for future in as_completed(futures):
                    try:
                        chunks = future.result()
                        all_chunks.extend(chunks)
                        total_new += len(chunks)
                    except Exception as e:
                        print(f"  [ERROR]: {e}", flush=True)

            processed = min((batch_idx + 3) * batch_size, len(words))
            elapsed = time.time() - start
            rate = processed / elapsed * 60 if elapsed > 0 else 0
            print(f"  Words {processed}/{len(words)} — {total_new} new chunks "
                  f"| {rate:.0f} words/min", flush=True)

        # Insert all new chunks into DB
        print(f"\nInserting {len(all_chunks)} new chunk families...")
        inserted = 0
        for c in all_chunks:
            root = c.get("root_form", c.get("chunk", ""))
            variant = c.get("chunk", root)
            wc = c.get("word_count", len(root.split()))
            is_baiano = c.get("is_baiano", False)
            bahia_rel = c.get("bahia_relevance", 20)

            try:
                family_id = _upsert_chunk_family(root, wc, is_baiano, bahia_relevance=bahia_rel)
                _upsert_chunk_variant(family_id, variant, "llm")
                inserted += 1
            except Exception as e:
                print(f"  [SKIP] {root}: {e}")

        elapsed = time.time() - start
        print(f"Inserted {inserted} families in {elapsed:.0f}s")
    else:
        conn.close()

    # Rank all families
    print("\nRanking chunk families...")
    rank_chunk_families()

    # Seed top chunks into queue
    print(f"\nSeeding top {args.seed_count} chunks into SRS queue...")
    top_chunks = get_next_chunks_for_srs(limit=args.seed_count)
    if not top_chunks:
        print("No new chunks to seed.")
    else:
        print(f"Generating Baiano carrier sentences for {len(top_chunks)} chunks...")
        added = add_chunks_to_queue(top_chunks)
        print(f"Seeded {added} chunks into SRS queue.")

    # Final status
    from extract_all_chunks import show_status
    show_status()


if __name__ == "__main__":
    main()
