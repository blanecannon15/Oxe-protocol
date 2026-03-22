#!/usr/bin/env python3
"""
expand_wordbank.py — Continuously expand the Oxe Protocol word bank.

Generates batches of practical Brazilian Portuguese words, slang, and regional
expressions using GPT-4o-mini, inserts them into word_bank, then caches all
7 dictionary tabs via precache_fast.

Covers all regions: Bahia, São Paulo, Rio, Minas, Nordeste, Sul, Norte, Centro-Oeste.

Usage:
    source ~/.profile && python3 -u expand_wordbank.py                  # default 500 words
    source ~/.profile && python3 -u expand_wordbank.py --count 1000     # 1000 words
    source ~/.profile && python3 -u expand_wordbank.py --loop           # continuous mode
"""

import json
import os
import sqlite3
import sys
import time

import openai

DB = os.path.join(os.path.dirname(__file__), "voca_20k.db")

CATEGORIES = [
    {
        "name": "Gírias baianas",
        "prompt": "gírias, expressões e palavras do dia-a-dia de Salvador e Bahia. Inclui termos de rua, festas, comida baiana, candomblé, capoeira.",
        "tier": 5,
    },
    {
        "name": "Gírias paulistas",
        "prompt": "gírias e expressões de São Paulo — mano, trampo, rolê, balada, vocabulário da periferia e do centro.",
        "tier": 5,
    },
    {
        "name": "Gírias cariocas",
        "prompt": "gírias e expressões do Rio de Janeiro — caô, parada, mermão, vocabulário de praia, funk, favela.",
        "tier": 5,
    },
    {
        "name": "Expressões mineiras",
        "prompt": "expressões e palavras de Minas Gerais — uai, trem, sô, nó, vocabulário mineiro do interior e BH.",
        "tier": 5,
    },
    {
        "name": "Expressões nordestinas",
        "prompt": "gírias e expressões do Nordeste (Pernambuco, Ceará, Maranhão, Paraíba, etc) — arretado, oxente, mainha, vocabulário regional.",
        "tier": 5,
    },
    {
        "name": "Expressões gaúchas e sulistas",
        "prompt": "gírias e expressões do Sul do Brasil (RS, SC, PR) — bah, tchê, guri, tri, vocabulário gaúcho e sulista.",
        "tier": 5,
    },
    {
        "name": "Expressões do Norte",
        "prompt": "gírias e expressões da Amazônia e Norte do Brasil (Pará, Amazonas, etc) — égua, mano, vocabulário ribeirinho e urbano.",
        "tier": 5,
    },
    {
        "name": "Finanças e negócios",
        "prompt": "vocabulário prático de finanças pessoais, investimentos, empreendedorismo, compra de negócios, imóveis, crédito, economia brasileira.",
        "tier": 4,
    },
    {
        "name": "Relacionamentos e namoro",
        "prompt": "vocabulário de namoro, relacionamentos, paquera, casamento, família, sentimentos, termos usados no Tinder/apps de encontro no Brasil.",
        "tier": 4,
    },
    {
        "name": "Comida e culinária brasileira",
        "prompt": "ingredientes, pratos, técnicas culinárias, comida de rua, restaurantes — de todas as regiões do Brasil.",
        "tier": 4,
    },
    {
        "name": "Tecnologia e internet",
        "prompt": "vocabulário de tecnologia, internet, redes sociais, apps, programação, termos que brasileiros usam no dia-a-dia digital.",
        "tier": 4,
    },
    {
        "name": "Saúde e corpo",
        "prompt": "vocabulário de saúde, corpo humano, academia, esportes, bem-estar, SUS, farmácia, doenças comuns.",
        "tier": 3,
    },
    {
        "name": "Trabalho e profissões",
        "prompt": "vocabulário de trabalho, CLT, freelancer, profissões brasileiras, entrevista de emprego, escritório, home office.",
        "tier": 3,
    },
    {
        "name": "Transporte e cidade",
        "prompt": "vocabulário de transporte urbano, ônibus, metrô, Uber, trânsito, bairros, vocabulário de morar na cidade brasileira.",
        "tier": 3,
    },
    {
        "name": "Expressões idiomáticas",
        "prompt": "expressões idiomáticas brasileiras de uso comum — meter o pé, dar um jeitinho, ficar de boa, pagar mico, etc. Frases feitas do dia-a-dia.",
        "tier": 4,
    },
    {
        "name": "Palavrões e xingamentos (uso real)",
        "prompt": "palavrões, xingamentos e expressões vulgares do português brasileiro — uso real na fala cotidiana. Inclui versões regionais.",
        "tier": 6,
    },
    {
        "name": "Verbos práticos conjugados",
        "prompt": "verbos brasileiros do dia-a-dia que não aparecem em livros didáticos — zoar, curtir, rolar, trampar, mandar ver, meter bronca, dar mole, etc.",
        "tier": 3,
    },
    {
        "name": "Música e cultura pop",
        "prompt": "vocabulário de música brasileira (samba, pagode, forró, sertanejo, funk, MPB), novelas, memes, cultura pop.",
        "tier": 5,
    },
]

SYSTEM = (
    "Tu é um linguista especialista em português brasileiro falado. "
    "Gera listas de palavras e expressões REAIS usadas no dia-a-dia. "
    "NUNCA inventa palavras. Todas devem ser usadas por brasileiros de verdade. "
    "Responde SOMENTE em JSON."
)


def get_existing_words():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT LOWER(word) FROM word_bank").fetchall()
    conn.close()
    return set(r[0] for r in rows)


def get_max_rank():
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT MAX(frequency_rank) FROM word_bank").fetchone()
    conn.close()
    return row[0] or 67092


def generate_batch(category, existing, batch_size=50):
    """Generate a batch of new words for a category via GPT-4o-mini."""
    client = openai.OpenAI()

    # Sample some existing words to help GPT avoid duplicates
    sample = list(existing)[:200]
    exclude_str = ", ".join(sample[:100])

    prompt = f"""Gera uma lista de EXATAMENTE {batch_size} palavras/expressões de português brasileiro.

CATEGORIA: {category['name']}
FOCO: {category['prompt']}

REGRAS:
- Cada item deve ser uma palavra ou expressão REAL usada por brasileiros
- Inclui tanto palavras simples quanto expressões de 2-4 palavras
- Mistura formal e informal, gíria e padrão
- NUNCA inclui palavras em inglês (exceto estrangeirismos já incorporados tipo "shopping", "delivery")
- NÃO repete nenhuma destas palavras já no banco: {exclude_str}

Responde em JSON:
{{"words": [
  {{"word": "palavra ou expressão", "definition": "definição curta em português"}},
  ...
]}}

SOMENTE JSON, nada mais."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.9,
            max_tokens=4096,
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("words", [])
    except Exception as e:
        print(f"  [ERROR] GPT call failed: {e}")
        return []


def insert_words(words, tier, existing):
    """Insert new words into word_bank. Returns count inserted."""
    conn = sqlite3.connect(DB)
    max_rank = get_max_rank()
    inserted = 0
    new_card = json.dumps({
        "due": "2000-01-01T00:00:00+00:00",
        "stability": 0.0,
        "difficulty": 0.0,
        "elapsed_days": 0,
        "scheduled_days": 0,
        "reps": 0,
        "lapses": 0,
        "state": 0,
        "last_review": None,
    })

    for w in words:
        word = w.get("word", "").strip().lower()
        if not word or word in existing:
            continue

        max_rank += 1
        try:
            conn.execute(
                """INSERT OR IGNORE INTO word_bank
                   (word, frequency_rank, frequency_count, difficulty_tier, srs_state)
                   VALUES (?, ?, 0, ?, ?)""",
                (word, max_rank, tier, new_card),
            )
            existing.add(word)
            inserted += 1
        except Exception:
            pass

    conn.commit()
    conn.close()
    return inserted


def cache_new_words():
    """Cache dictionary tabs for any uncached words."""
    from precache_fast import get_uncached_words, cache_word_bulk
    from concurrent.futures import ThreadPoolExecutor, as_completed

    words = get_uncached_words()
    if not words:
        print("  All words already cached.")
        return 0

    print(f"  Caching {len(words)} new words (50 workers)...")
    ok = 0
    fail = 0
    done = 0
    total = len(words)

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(cache_word_bulk, wid, w): w for wid, w in words}
        for f in as_completed(futures):
            try:
                c, f2 = f.result()
                ok += c
                fail += f2
            except Exception:
                fail += 1
            done += 1
            if done % 50 == 0 or done == total:
                print(f"    [{done}/{total}] {ok} tabs ok, {fail} fail")

    print(f"  Cache done: {ok} tabs, {fail} failed")
    return ok


def main():
    count = 500
    loop = False

    if "--count" in sys.argv:
        idx = sys.argv.index("--count")
        if idx + 1 < len(sys.argv):
            count = int(sys.argv[idx + 1])

    if "--loop" in sys.argv:
        loop = True

    while True:
        existing = get_existing_words()
        total_before = len(existing)
        print(f"\n[expand] Word bank: {total_before} words. Target: +{count}")

        total_inserted = 0
        for cat in CATEGORIES:
            if total_inserted >= count:
                break

            batch_size = min(50, count - total_inserted)
            print(f"\n  [{cat['name']}] Generating {batch_size} words...")
            words = generate_batch(cat, existing, batch_size)
            if words:
                inserted = insert_words(words, cat["tier"], existing)
                total_inserted += inserted
                print(f"  [{cat['name']}] +{inserted} new words ({len(words)} generated, {len(words)-inserted} duplicates)")
            else:
                print(f"  [{cat['name']}] No words generated")

        print(f"\n[expand] Total inserted: {total_inserted}")

        if total_inserted > 0:
            cache_new_words()

        total_after = len(get_existing_words())
        print(f"\n[expand] Word bank: {total_before} → {total_after} (+{total_after - total_before})")

        if not loop:
            break

        print(f"\n[expand] Sleeping 60s before next round...")
        time.sleep(60)


if __name__ == "__main__":
    main()
