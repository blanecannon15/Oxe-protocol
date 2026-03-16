"""
podcast_gen.py — Podcast episode generator for the Oxe Protocol.

Generates ~6000 word podcast episodes in 12 segments (~500 words each),
all in Soteropolitano (Baiano) Portuguese. Episodes are cached in voca_20k.db.

Usage:
    source ~/.profile && python3 podcast_gen.py --generate
    source ~/.profile && python3 podcast_gen.py --generate --difficulty 60
    source ~/.profile && python3 podcast_gen.py --list
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import openai

from srs_engine import DB_PATH, get_connection


# ── System Prompt ────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "Tu é um podcaster soteropolitano de Salvador, Bahia. "
    "Cria episódios longos de podcast em português baiano natural — "
    "como se tivesse gravando pro povo de Salvador ouvir caminhando.\n\n"
    "REGRAS OBRIGATÓRIAS:\n"
    "- NUNCA usa inglês. Nem uma palavra. Português baiano puro.\n"
    "- Sotaque baiano natural: oxe, vixe, mainha, painho, barril, arretado, "
    "massa, é mermo, lá ele, visse, tu + verbo na terceira pessoa.\n"
    "- Contrações naturais: cê, tô, tá, pra, pro, dum, duma, num, numa.\n"
    "- Cenário: Salvador, Bahia — bairros reais, comidas, festas, cultura baiana.\n"
    "- Fala como baiano fala, não como livro didático.\n"
    "- Usa chunks naturais — frases e colocações, não palavras soltas.\n"
    "- Cada segmento deve fluir pro próximo como um podcast de verdade.\n"
    "- Mistura histórias, reflexões, dicas culturais e humor baiano."
)


# ── Podcast Generation ───────────────────────────────────────────

def generate_podcast(difficulty=80, focus_words=None, db_path=DB_PATH):
    """Generate a ~6000 word podcast episode in 12 segments.

    Args:
        difficulty: 60 = Tier 1-2 vocab, slow pace;
                    80 = Tier 1-4, natural;
                    100 = all tiers, natural gíria.
        focus_words: list of words to weave into the episode (3-5 per segment).
        db_path: path to the SQLite database.

    Returns:
        dict with keys: title, difficulty, segments, word_count
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set.")
        return None

    if focus_words is None:
        focus_words = []

    # Map difficulty to vocabulary description
    if difficulty <= 60:
        vocab_desc = (
            "Usa APENAS vocabulário simples — as 2000 palavras mais frequentes "
            "do português brasileiro (Tiers 1-2). Frases curtas, ritmo lento, "
            "muita repetição. Ideal pra quem tá começando."
        )
    elif difficulty <= 80:
        vocab_desc = (
            "Usa vocabulário intermediário — Tiers 1-4 do português brasileiro. "
            "Ritmo natural mas acessível. Mistura palavras comuns com algumas "
            "menos frequentes, sempre com contexto pra entender."
        )
    else:
        vocab_desc = (
            "Usa vocabulário avançado — todos os tiers, incluindo gíria pesada, "
            "expressões idiomáticas baianas e linguagem coloquial de rua. "
            "Ritmo rápido e natural como um baiano falando com outro baiano."
        )

    focus_str = ", ".join(focus_words) if focus_words else "(nenhuma palavra específica)"

    user_prompt = f"""Cria um episódio COMPLETO de podcast com ~6000 palavras, dividido em EXATAMENTE 12 segmentos.

DIFICULDADE: {difficulty}/100
{vocab_desc}

PALAVRAS DE FOCO (tenta usar 3-5 por segmento, de forma natural): {focus_str}

ESTRUTURA DO EPISÓDIO (12 segmentos, ~500 palavras cada):
1. ABERTURA — se apresenta, conta o que vai rolar no episódio
2. HISTÓRIA PRINCIPAL PT.1 — começa contando uma história de Salvador
3. HISTÓRIA PRINCIPAL PT.2 — desenvolve a história com detalhes sensoriais
4. PAUSA CULTURAL — explica algum aspecto da cultura baiana relacionado à história
5. HISTÓRIA PRINCIPAL PT.3 — complicação, conflito, algo inesperado
6. REFLEXÃO — o que isso significa, conecta com a vida em Salvador
7. HISTÓRIA SECUNDÁRIA — conta outra história relacionada, mais curta
8. DICA DE VOCABULÁRIO — explica 3-4 gírias/expressões baianas de forma natural
9. HISTÓRIA PRINCIPAL PT.4 — resolução da história principal
10. INTERAÇÃO — faz perguntas retóricas ao ouvinte, convida a pensar
11. RESUMO E LIÇÃO — o que aprendeu, o que muda daqui pra frente
12. ENCERRAMENTO — despede, convida pra próximo episódio

CADA SEGMENTO deve ter ~500 palavras. O episódio todo DEVE ter pelo menos 5500 palavras.

Responde EXATAMENTE neste formato JSON:
{{
  "title": "título criativo do episódio",
  "segments": [
    {{"text": "texto completo do segmento 1 (~500 palavras)", "focus_words": ["palavras", "usadas", "aqui"]}},
    {{"text": "texto completo do segmento 2 (~500 palavras)", "focus_words": ["palavras", "usadas", "aqui"]}},
    ... (12 segmentos total)
  ]
}}

IMPORTANTE: Cada segmento DEVE ter pelo menos 400 palavras. Desenvolve com calma, com descrições ricas, diálogos internos, sensações. Conta como se tivesse falando com um amigo.

Responde APENAS o JSON, nada mais."""

    client = openai.OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.9,
        max_tokens=16384,
    )

    raw = response.choices[0].message.content.strip()

    # Parse JSON from response
    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            if raw.endswith("```"):
                raw = raw[:-3]
            elif "```" in raw:
                raw = raw[:raw.rfind("```")]
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"ERROR: Failed to parse podcast JSON. Raw output:\n{raw[:500]}")
        return None

    title = data.get("title", "Sem título")
    segments = data.get("segments", [])

    # Calculate total word count
    total_words = sum(len(seg.get("text", "").split()) for seg in segments)

    result = {
        "title": title,
        "difficulty": difficulty,
        "segments": segments,
        "word_count": total_words,
    }

    print(f'  Podcast: "{title}" — {total_words} palavras, {len(segments)} segmentos')
    return result


# ── Database Operations ──────────────────────────────────────────

def _get_conn(db_path=DB_PATH):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def save_podcast(podcast_data, db_path=DB_PATH):
    """Insert a podcast episode into the podcast_library table.

    Returns:
        podcast_id (int)
    """
    conn = _get_conn(db_path)

    # Collect all focus words from segments
    all_focus = []
    for seg in podcast_data.get("segments", []):
        all_focus.extend(seg.get("focus_words", []))
    unique_focus = list(set(all_focus))

    # Serialize segments into body (full text) and keep segments as audio_segments
    body_parts = [seg.get("text", "") for seg in podcast_data.get("segments", [])]
    body = "\n\n---\n\n".join(body_parts)

    conn.execute(
        """INSERT INTO podcast_library
           (title, difficulty, total_segments, body, focus_words, word_count, audio_segments)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            podcast_data.get("title", "Sem título"),
            podcast_data.get("difficulty", 80),
            len(podcast_data.get("segments", [])),
            body,
            json.dumps(unique_focus, ensure_ascii=False),
            podcast_data.get("word_count", 0),
            json.dumps(podcast_data.get("segments", []), ensure_ascii=False),
        ),
    )
    conn.commit()
    podcast_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    print(f"  Saved podcast ID: {podcast_id}")
    return podcast_id


def get_podcast(podcast_id, db_path=DB_PATH):
    """Fetch a single podcast episode from the database.

    Returns:
        dict with podcast data, or None if not found.
    """
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM podcast_library WHERE id = ?", (podcast_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    segments = []
    if row["audio_segments"]:
        try:
            segments = json.loads(row["audio_segments"])
        except json.JSONDecodeError:
            segments = []

    return {
        "id": row["id"],
        "title": row["title"],
        "difficulty": row["difficulty"],
        "total_segments": row["total_segments"],
        "body": row["body"],
        "focus_words": json.loads(row["focus_words"]) if row["focus_words"] else [],
        "word_count": row["word_count"],
        "segments": segments,
        "times_played": row["times_played"],
        "last_played": row["last_played"],
        "created_at": row["created_at"],
    }


def list_podcasts(db_path=DB_PATH):
    """List all podcast episodes ordered by created_at DESC.

    Returns:
        list of dicts with podcast summaries.
    """
    conn = _get_conn(db_path)
    rows = conn.execute(
        """SELECT id, title, difficulty, total_segments, word_count,
                  times_played, created_at
           FROM podcast_library ORDER BY created_at DESC"""
    ).fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "difficulty": r["difficulty"],
            "total_segments": r["total_segments"],
            "word_count": r["word_count"],
            "times_played": r["times_played"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ── CLI ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return

    if "--list" in sys.argv:
        podcasts = list_podcasts()
        if not podcasts:
            print("Nenhum podcast ainda. Gera com: python3 podcast_gen.py --generate")
            return
        print(f"\n  {'ID':<5} {'Diff':<6} {'Segs':<6} {'Words':<7} {'Played':<8} Título")
        print(f"  {'─'*65}")
        for p in podcasts:
            print(f"  {p['id']:<5} {p['difficulty']:<6} {p['total_segments']:<6} "
                  f"{p['word_count']:<7} {p['times_played']:<8} {p['title']}")
        print()
        return

    if "--generate" in sys.argv:
        difficulty = 80
        if "--difficulty" in sys.argv:
            idx = sys.argv.index("--difficulty")
            if idx + 1 < len(sys.argv):
                difficulty = int(sys.argv[idx + 1])

        focus = None
        if "--focus" in sys.argv:
            idx = sys.argv.index("--focus")
            if idx + 1 < len(sys.argv):
                focus = sys.argv[idx + 1].split(",")

        print(f"\nGerando podcast (dificuldade {difficulty})...\n")
        podcast_data = generate_podcast(difficulty=difficulty, focus_words=focus)
        if podcast_data:
            podcast_id = save_podcast(podcast_data)
            print(f"\nPronto! Podcast ID: {podcast_id}")
        else:
            print("\nErro ao gerar podcast.")
        return

    print(__doc__.strip())


if __name__ == "__main__":
    main()
