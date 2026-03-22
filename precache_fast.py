#!/usr/bin/env python3
"""Fast parallel dictionary precache — 1 API call per word for all 7 tabs."""
import sqlite3, sys, os, time, json
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
import openai

DB = os.path.join(os.path.dirname(__file__), "voca_20k.db")
WORKERS = 50

# Reuse a single client with connection pooling
_client = None
def get_client():
    global _client
    if _client is None:
        _client = openai.OpenAI(max_retries=2, timeout=30)
    return _client

SYSTEM = (
    "Você é um dicionário de português brasileiro. "
    "Responde SOMENTE em JSON, NUNCA em inglês. "
    "Use linguagem simples e natural."
)

PROMPT_TEMPLATE = '''Gere dados de dicionário para a palavra "{word}".

Responde em JSON com EXATAMENTE estas 7 chaves:

"definition": {{"definicao": "explicação curta", "uso_regional": "baiano" ou "geral", "exemplo_chunk": "frase curta"}}
"examples": {{"exemplos": ["frase 1", "frase 2", "frase 3"], "contextos": ["formal", "informal", "coloquial"]}}
"pronunciation": {{"silabas": "sí-la-bas", "guia_fonetico": "guia simples", "audio_path": null}}
"expressions": {{"expressoes": [{{"expressao": "...", "significado": "...", "exemplo": "..."}}]}}
"conjugation": {{"presente": {{"eu": "...", "tu": "...", "ele": "...", "nos": "...", "eles": "..."}}, "passado": {{"eu": "...", "tu": "...", "ele": "...", "nos": "...", "eles": "..."}}, "futuro": {{"eu": "...", "tu": "...", "ele": "...", "nos": "...", "eles": "..."}}}}
"synonyms": {{"sinonimos": ["..."], "antonimos": ["..."], "palavras_relacionadas": ["..."]}}
"chunks": {{"chunks": [{{"chunk": "...", "significado": "...", "exemplo": "..."}}]}}

Se a palavra não conjuga (não é verbo), retorna conjugation como {{"nota": "não é verbo"}}.
Responde SOMENTE em JSON válido.'''


def get_uncached_words():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT wb.id, wb.word
        FROM word_bank wb
        LEFT JOIN dictionary_cache dc ON dc.word_id = wb.id
        GROUP BY wb.id
        HAVING COUNT(dc.tab_name) < 7
        ORDER BY wb.frequency_rank ASC
    """).fetchall()
    conn.close()
    return [(r['id'], r['word']) for r in rows]


def get_cached_tabs(word_id):
    conn = sqlite3.connect(DB)
    cached = set(r[0] for r in conn.execute(
        'SELECT tab_name FROM dictionary_cache WHERE word_id=?', (word_id,)
    ).fetchall())
    conn.close()
    return cached


def cache_word_bulk(word_id, word):
    """Single API call to get all 7 tabs. Returns (tabs_cached, tabs_failed)."""
    already = get_cached_tabs(word_id)
    if len(already) >= 7:
        return (0, 0)

    try:
        client = get_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": PROMPT_TEMPLATE.format(word=word)},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception:
        return (0, 1)

    tabs_map = {
        'definition': data.get('definition'),
        'examples': data.get('examples'),
        'pronunciation': data.get('pronunciation'),
        'expressions': data.get('expressions'),
        'conjugation': data.get('conjugation'),
        'synonyms': data.get('synonyms'),
        'chunks': data.get('chunks'),
    }

    cached = 0
    failed = 0
    conn = sqlite3.connect(DB)
    for tab_name, tab_data in tabs_map.items():
        if tab_name in already:
            continue
        if tab_data is None:
            failed += 1
            continue
        try:
            conn.execute(
                "INSERT OR REPLACE INTO dictionary_cache (word_id, tab_name, data_json) VALUES (?, ?, ?)",
                (word_id, tab_name, json.dumps(tab_data, ensure_ascii=False))
            )
            cached += 1
        except Exception:
            failed += 1
    conn.commit()
    conn.close()
    return (cached, failed)


def main():
    print("[precache_fast] Scanning...")
    words = get_uncached_words()
    total = len(words)
    print(f"[precache_fast] {total} words need caching. {WORKERS} workers, 1 API call per word.\n")

    if total == 0:
        print("All done!")
        return

    done = 0
    ok = 0
    fail = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(cache_word_bulk, wid, w): w for wid, w in words}

        for f in as_completed(futures):
            try:
                c, f2 = f.result()
                ok += c
                fail += f2
            except Exception:
                fail += 1

            done += 1
            if done % 100 == 0 or done == total:
                elapsed = time.time() - start
                rate = done / elapsed
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  [{done}/{total}] {rate:.1f} words/sec | "
                      f"{ok} tabs ok, {fail} fail | ETA: {eta/3600:.1f}h")

    elapsed = time.time() - start
    print(f"\n[precache_fast] Done: {elapsed/3600:.1f}h, {done} words, {ok} tabs, {fail} failed")


if __name__ == "__main__":
    main()
