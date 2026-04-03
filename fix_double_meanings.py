#!/usr/bin/env python3
"""
Scrape all words in word_bank for multiple meanings.
Updates the definition tab in dictionary_cache to include ALL meanings.

Phase 1: Screen words in batches to find polysemous ones.
Phase 2: Regenerate definition tab for polysemous words with all meanings.

Usage:
    python3 fix_double_meanings.py                  # Full run
    python3 fix_double_meanings.py --phase1-only     # Just identify polysemous words
    python3 fix_double_meanings.py --phase2-only     # Just update definitions (requires phase1 output)
    python3 fix_double_meanings.py --dry-run          # Preview without DB writes
    python3 fix_double_meanings.py --batch-size 100   # Adjust batch size
"""
import sqlite3, sys, os, time, json, argparse, gzip, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import openai

DB = os.path.join(os.path.dirname(__file__), "voca_20k.db")
POLYSEMY_FILE = os.path.join(os.path.dirname(__file__), "polysemous_words.json")
RAILWAY_URL = "https://oxe-protocol-production.up.railway.app"
WORKERS = 30  # parallel threads
SCREEN_BATCH = 80  # words per screening call
UPDATE_WORKERS = 40  # parallel threads for definition updates
RAILWAY_PUSH_BATCH = 500  # rows per Railway push request

_client = None
def get_client():
    global _client
    if _client is None:
        _client = openai.OpenAI(max_retries=3, timeout=60)
    return _client


def push_to_railway(word_ids):
    """Push updated definition tabs for given word_ids to Railway production."""
    if not word_ids:
        return 0, 0

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        f"SELECT word_id, tab_name, data_json FROM dictionary_cache "
        f"WHERE tab_name = 'definition' AND word_id IN ({','.join('?' * len(word_ids))})",
        word_ids
    ).fetchall()
    conn.close()

    if not rows:
        return 0, 0

    pushed = 0
    errors = 0

    # Send in sub-batches of RAILWAY_PUSH_BATCH
    for i in range(0, len(rows), RAILWAY_PUSH_BATCH):
        batch = rows[i:i + RAILWAY_PUSH_BATCH]
        payload = {
            "rows": [
                {"word_id": r[0], "tab_name": r[1], "data_json": r[2]}
                for r in batch
            ]
        }
        data = json.dumps(payload).encode("utf-8")
        compressed = gzip.compress(data)

        try:
            req = urllib.request.Request(
                f"{RAILWAY_URL}/api/cache/bulk",
                data=compressed,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=60)
            result = json.loads(resp.read().decode())
            pushed += result.get("inserted", 0)
        except Exception as e:
            errors += 1
            print(f"  [RAILWAY ERROR] {e}")
            time.sleep(1)

    return pushed, errors


# ─── Phase 1: Screen words for multiple meanings ────────────────────────

SCREEN_SYSTEM = (
    "Você é um linguista de português brasileiro. "
    "Sua tarefa é identificar palavras que têm MÚLTIPLOS significados distintos. "
    "Responde SOMENTE em JSON válido, NUNCA em inglês."
)

SCREEN_PROMPT = '''Analise estas palavras do português brasileiro e identifique TODAS que têm 2 ou mais significados DISTINTOS.

Inclua TODOS os tipos de significado múltiplo:
- Substantivo vs verbo (ex: "banco" = instituição financeira / assento / verbo bancar)
- Significados diferentes como substantivo (ex: "manga" = fruta / parte da camisa)
- Sentido literal vs figurado (ex: "pena" = pluma / sentimento de pena / punição legal)
- Gíria/coloquial vs formal (ex: "saca" = entende? / saco grande)
- Formas verbais que também são adjetivos/substantivos (ex: "leve" = peso leve / imperativo de levar)
- Regionalismos baianos com significado diferente do padrão
- Duplo sentido, calão, significado técnico, etc.

Palavras para analisar:
{words}

Responda em JSON assim:
{{
  "polysemous": [
    {{
      "word": "banco",
      "meanings_count": 3,
      "meanings_brief": ["instituição financeira", "assento/banco de praça", "forma do verbo bancar"]
    }}
  ]
}}

Inclua SOMENTE palavras que realmente têm 2+ significados distintos. Não inclua palavras com apenas 1 significado.'''


def get_all_words():
    """Get all words from word_bank."""
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, word FROM word_bank ORDER BY frequency_rank ASC"
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def screen_batch(words_batch):
    """Screen a batch of words for polysemy. Returns list of polysemous word dicts."""
    word_list = "\n".join(f"- {w}" for _, w in words_batch)
    word_id_map = {w: wid for wid, w in words_batch}

    try:
        client = get_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SCREEN_SYSTEM},
                {"role": "user", "content": SCREEN_PROMPT.format(words=word_list)},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        data = json.loads(resp.choices[0].message.content)
        results = []
        for item in data.get("polysemous", []):
            word = item.get("word", "")
            if word in word_id_map:
                item["word_id"] = word_id_map[word]
                results.append(item)
        return results
    except Exception as e:
        print(f"  [screen_batch ERROR] {e}")
        return []


def phase1_screen(all_words, batch_size=SCREEN_BATCH):
    """Screen all words in batches to find polysemous ones."""
    print(f"\n[Phase 1] Screening {len(all_words)} words for multiple meanings...")
    print(f"  Batch size: {batch_size}, Workers: {WORKERS}")

    # Create batches
    batches = []
    for i in range(0, len(all_words), batch_size):
        batches.append(all_words[i:i + batch_size])

    total_batches = len(batches)
    print(f"  Total batches: {total_batches}\n")

    all_polysemous = []
    done = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(screen_batch, batch): i for i, batch in enumerate(batches)}

        for f in as_completed(futures):
            try:
                results = f.result()
                all_polysemous.extend(results)
            except Exception as e:
                print(f"  [batch ERROR] {e}")

            done += 1
            if done % 20 == 0 or done == total_batches:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total_batches - done) / rate if rate > 0 else 0
                print(f"  [{done}/{total_batches}] {rate:.1f} batches/sec | "
                      f"Found {len(all_polysemous)} polysemous words | ETA: {eta/60:.1f}m")

    # Save results
    with open(POLYSEMY_FILE, "w") as f:
        json.dump(all_polysemous, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - start
    print(f"\n[Phase 1] Done: {elapsed/60:.1f}m")
    print(f"  Found {len(all_polysemous)} polysemous words")
    print(f"  Saved to {POLYSEMY_FILE}")

    return all_polysemous


# ─── Phase 2: Update definitions with ALL meanings ──────────────────────

UPDATE_SYSTEM = (
    "Você é um dicionário completo de português brasileiro com foco no sotaque baiano/soteropolitano. "
    "Responde SOMENTE em JSON válido. Use linguagem simples e natural. "
    "Use elições faladas naturais: tá, cê, pra, num, etc."
)

UPDATE_PROMPT = '''A palavra "{word}" tem múltiplos significados. Gere uma definição COMPLETA com TODOS os significados.

Significados conhecidos: {meanings}

Gere o JSON assim:
{{
  "definicao": "1) [primeiro significado]. 2) [segundo significado]. 3) [terceiro, se houver].",
  "uso_regional": "baiano" ou "geral",
  "exemplo_chunk": "frase curta usando o significado mais comum",
  "todos_significados": [
    {{
      "numero": 1,
      "significado": "explicação clara do primeiro significado",
      "classe_gramatical": "substantivo/verbo/adjetivo/advérbio/gíria/etc",
      "exemplo": "frase curta de exemplo",
      "registro": "formal/informal/coloquial/gíria/técnico"
    }},
    {{
      "numero": 2,
      "significado": "explicação clara do segundo significado",
      "classe_gramatical": "...",
      "exemplo": "frase curta de exemplo",
      "registro": "..."
    }}
  ]
}}

IMPORTANTE:
- Inclua TODOS os significados, inclusive gíria, calão, regionalismos baianos, e formas verbais
- Use linguagem falada natural (tá, cê, pra, num, etc.)
- Exemplos devem soar como alguém de Salvador falando
- Se a palavra tem significado diferente na Bahia vs resto do Brasil, mencione'''


def update_word_definition(word_id, word, meanings_brief, dry_run=False):
    """Regenerate the definition tab for a polysemous word with ALL meanings."""
    meanings_str = ", ".join(meanings_brief) if meanings_brief else "múltiplos"

    try:
        client = get_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": UPDATE_SYSTEM},
                {"role": "user", "content": UPDATE_PROMPT.format(
                    word=word, meanings=meanings_str
                )},
            ],
            response_format={"type": "json_object"},
            temperature=0.5,
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception as e:
        return (word, False, str(e))

    if not data.get("definicao"):
        return (word, False, "no definicao in response")

    if dry_run:
        return (word, True, data.get("definicao", "")[:120])

    try:
        conn = sqlite3.connect(DB)
        conn.execute(
            "INSERT OR REPLACE INTO dictionary_cache (word_id, tab_name, data_json) VALUES (?, 'definition', ?)",
            (word_id, json.dumps(data, ensure_ascii=False))
        )
        conn.commit()
        conn.close()
        return (word, True, data.get("definicao", "")[:120])
    except Exception as e:
        return (word, False, str(e))


def phase2_update(polysemous_words, dry_run=False):
    """Update definition tab for all polysemous words, pushing to Railway after each batch."""
    total = len(polysemous_words)
    BATCH_SIZE = 200  # process in batches, push to Railway after each
    print(f"\n[Phase 2] Updating definitions for {total} polysemous words...")
    print(f"  Batch size: {BATCH_SIZE} (push to Railway after each batch)")
    if dry_run:
        print("  *** DRY RUN — no DB writes, no Railway push ***")
    print(f"  Workers: {UPDATE_WORKERS}\n")

    # Check Railway reachability once
    if not dry_run:
        try:
            req = urllib.request.Request(f"{RAILWAY_URL}/api/search?q=teste")
            urllib.request.urlopen(req, timeout=10)
            print("  Railway is reachable ✓\n")
        except Exception as e:
            print(f"  WARNING: Railway unreachable ({e}) — will retry per batch\n")

    done = 0
    ok = 0
    fail = 0
    total_pushed = 0
    total_push_errors = 0
    start = time.time()

    # Process in batches
    for batch_start in range(0, total, BATCH_SIZE):
        batch = polysemous_words[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  --- Batch {batch_num}/{total_batches} ({len(batch)} words) ---")

        batch_updated_ids = []

        with ThreadPoolExecutor(max_workers=UPDATE_WORKERS) as pool:
            futures = {}
            for item in batch:
                word_id = item.get("word_id")
                word = item.get("word")
                meanings = item.get("meanings_brief", [])
                if word_id and word:
                    fut = pool.submit(update_word_definition, word_id, word, meanings, dry_run)
                    futures[fut] = (word, word_id)

            for f in as_completed(futures):
                word, word_id = futures[f]
                try:
                    w, success, detail = f.result()
                    if success:
                        ok += 1
                        batch_updated_ids.append(word_id)
                    else:
                        fail += 1
                        if fail <= 10:
                            print(f"    [FAIL] {w}: {detail}")
                except Exception as e:
                    fail += 1

                done += 1

        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"    Updated {len(batch_updated_ids)}/{len(batch)} words | "
              f"Total: [{done}/{total}] {rate:.1f} w/s | ETA: {eta/60:.1f}m")

        # Push this batch to Railway
        if batch_updated_ids and not dry_run:
            print(f"    Pushing {len(batch_updated_ids)} definitions to Railway...", end=" ", flush=True)
            pushed, push_errors = push_to_railway(batch_updated_ids)
            total_pushed += pushed
            total_push_errors += push_errors
            print(f"done ({pushed} pushed, {push_errors} errors)")
        elif dry_run:
            print(f"    [DRY RUN] Would push {len(batch_updated_ids)} definitions to Railway")

    elapsed = time.time() - start
    print(f"\n[Phase 2] Done: {elapsed/60:.1f}m")
    print(f"  Definitions updated: {ok}, Failed: {fail}")
    print(f"  Railway pushed: {total_pushed}, Push errors: {total_push_errors}")


def main():
    parser = argparse.ArgumentParser(description="Fix double meanings in dictionary cache")
    parser.add_argument("--phase1-only", action="store_true", help="Only screen for polysemous words")
    parser.add_argument("--phase2-only", action="store_true", help="Only update definitions (needs phase1 output)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--batch-size", type=int, default=SCREEN_BATCH, help="Words per screening batch")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of words to process (0=all)")
    args = parser.parse_args()

    all_words = get_all_words()
    if args.limit > 0:
        all_words = all_words[:args.limit]

    print(f"[fix_double_meanings] Database: {DB}")
    print(f"[fix_double_meanings] Total words: {len(all_words)}")

    if args.phase2_only:
        # Load phase1 results
        if not os.path.exists(POLYSEMY_FILE):
            print(f"ERROR: {POLYSEMY_FILE} not found. Run phase1 first.")
            sys.exit(1)
        with open(POLYSEMY_FILE) as f:
            polysemous = json.load(f)
        print(f"[fix_double_meanings] Loaded {len(polysemous)} polysemous words from cache")
        phase2_update(polysemous, dry_run=args.dry_run)
    elif args.phase1_only:
        phase1_screen(all_words, batch_size=args.batch_size)
    else:
        # Full run
        polysemous = phase1_screen(all_words, batch_size=args.batch_size)
        if polysemous:
            phase2_update(polysemous, dry_run=args.dry_run)
        else:
            print("\nNo polysemous words found (unlikely — check for errors).")


if __name__ == "__main__":
    main()
