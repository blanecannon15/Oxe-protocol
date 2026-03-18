"""
story_gen.py — Graded story generator for the Oxe Protocol.

Generates first-person Soteropolitano narratives constrained to the
learner's vocabulary tiers. Stories are cached in voca_20k.db.

Usage:
    source ~/.profile && python3 story_gen.py --init
    source ~/.profile && python3 story_gen.py --generate --level A1 --count 3
    source ~/.profile && python3 story_gen.py --generate-all --count 3
    source ~/.profile && python3 story_gen.py --list
"""

import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from srs_engine import DB_PATH, get_connection, get_unlocked_tier, get_due_words

# Lazy imports for post-generation hooks (chunk extraction & classification)
# Actual imports happen inside _run_post_generation_hooks to avoid circular deps.

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ── Comprehension Levels ──────────────────────────────────────────

STORY_WORD_TARGET = (1100, 1300)  # ~1200 words = ~10 minutes audio
QUESTIONS_PER_STORY = 5

LEVELS = {
    "P1": {
        "label": "Primeiro Passo",
        "description": "Top 50 words — very simple, repetitive",
        "tiers": [1],
        "known_pct": 1.00,
        "stretch_tiers": [],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 1,
        "questions": QUESTIONS_PER_STORY,
        "freq_cap": 50,
    },
    "P2": {
        "label": "Primeiras Palavras",
        "description": "Top 100 words — basic daily situations",
        "tiers": [1],
        "known_pct": 1.00,
        "stretch_tiers": [],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 1,
        "questions": QUESTIONS_PER_STORY,
        "freq_cap": 100,
    },
    "P3": {
        "label": "Começando",
        "description": "Top 300 words — simple stories with context",
        "tiers": [1],
        "known_pct": 1.00,
        "stretch_tiers": [],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 1,
        "questions": QUESTIONS_PER_STORY,
        "freq_cap": 300,
    },
    "A1": {
        "label": "Tudo Tranquilo",
        "description": "100% Tier 1 — simple daily life",
        "tiers": [1],
        "known_pct": 1.00,
        "stretch_tiers": [],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 1,
        "questions": QUESTIONS_PER_STORY,
    },
    "A2": {
        "label": "Quase Lá",
        "description": "95% Tiers 1-2, 5% Tier 3",
        "tiers": [1, 2],
        "known_pct": 0.95,
        "stretch_tiers": [3],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 1,
        "questions": QUESTIONS_PER_STORY,
    },
    "B1": {
        "label": "No Pique",
        "description": "85% Tiers 1-3, 15% Tier 4",
        "tiers": [1, 2, 3],
        "known_pct": 0.85,
        "stretch_tiers": [4],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 2,
        "questions": QUESTIONS_PER_STORY,
    },
    "B2": {
        "label": "Desenrolado",
        "description": "75% Tiers 1-4, 25% Tier 5",
        "tiers": [1, 2, 3, 4],
        "known_pct": 0.75,
        "stretch_tiers": [5],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 3,
        "questions": QUESTIONS_PER_STORY,
    },
    "C1": {
        "label": "Quase Nativo",
        "description": "60% Tiers 1-5, 40% Tier 6",
        "tiers": [1, 2, 3, 4, 5],
        "known_pct": 0.60,
        "stretch_tiers": [6],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 4,
        "questions": QUESTIONS_PER_STORY,
    },
    "C2": {
        "label": "Soteropolitano",
        "description": "50% known + 50% any tier — full dialect",
        "tiers": [1, 2, 3, 4, 5, 6],
        "known_pct": 0.50,
        "stretch_tiers": [],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 5,
        "questions": QUESTIONS_PER_STORY,
    },
    "A3": {
        "label": "Entendendo",
        "description": "90% Tiers 1-3 — moderate Baiano, some contractions",
        "tiers": [1, 2, 3],
        "known_pct": 0.90,
        "stretch_tiers": [4],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 1,
        "questions": QUESTIONS_PER_STORY,
        "style_prompt": "Baiano moderado. Usa contrações naturais (tô, tá, pra, pro, cê) mas mantém clareza. Ritmo de fala médio, sem correr. Sotaque baiano presente mas controlado.",
    },
    "A4": {
        "label": "Fluindo",
        "description": "88% Tiers 1-4 — natural Baiano, connected speech",
        "tiers": [1, 2, 3, 4],
        "known_pct": 0.88,
        "stretch_tiers": [5],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 2,
        "questions": QUESTIONS_PER_STORY,
        "style_prompt": "Baiano natural e fluido. Fala conectada — palavras se juntam como na fala real. Usa gírias comuns (barril, massa, arretado). Contrações e elisões naturais. Ritmo como conversa entre amigos.",
    },
    "NATIVE_CLEAR": {
        "label": "Nativo Claro",
        "description": "80% Tiers 1-5 — full Baiano but clear articulation",
        "tiers": [1, 2, 3, 4, 5],
        "known_pct": 0.80,
        "stretch_tiers": [6],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 3,
        "questions": QUESTIONS_PER_STORY,
        "style_prompt": "Baiano completo com articulação clara. Sotaque forte mas cada palavra é distinguível. Usa todas as gírias e expressões baianas naturalmente. Fala como um soteropolitano educado conversando com alguém de fora.",
    },
    "NATIVE_CASUAL": {
        "label": "Nativo Casual",
        "description": "70% all tiers — casual speech, gírias, elisions",
        "tiers": [1, 2, 3, 4, 5, 6],
        "known_pct": 0.70,
        "stretch_tiers": [],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 4,
        "questions": QUESTIONS_PER_STORY,
        "style_prompt": "Baiano casual total. Gírias pesadas, elisões constantes (falano, comeno, fazeno). Palavras engolidas, ritmo rápido. Fala como dois baianos num bar — sem filtro. Expressões idiomáticas de Salvador sem explicação.",
    },
    "NATIVE_CHAOTIC": {
        "label": "Nativo Caótico",
        "description": "50% all tiers — fast overlapping speech, heavy slang, real street Baiano",
        "tiers": [1, 2, 3, 4, 5, 6],
        "known_pct": 0.50,
        "stretch_tiers": [],
        "word_count": STORY_WORD_TARGET,
        "min_tier": 5,
        "questions": QUESTIONS_PER_STORY,
        "style_prompt": "Baiano caótico de rua. Velocidade máxima, gírias obscuras, calão pesado. Frases interrompidas, pensamentos sobrepostos, mudança de assunto sem aviso. Referências culturais profundas de Salvador. Fala como vendedor na Feira de São Joaquim num sábado lotado. Elisões extremas, palavras cortadas, ritmo frenético.",
    },
}

THEMES = [
    ("vida_de_rua", "Street life — bus, praia, walking through Salvador"),
    ("comida", "Food — acarajé, moqueca, vatapá, cooking with mainha"),
    ("festas", "Festivals — Carnaval, Lavagem do Bonfim, Yemanjá, São João"),
    ("trabalho", "Work — hustling, small business, market, day jobs"),
    ("familia", "Family — mainha, painho, vizinha, comadre, home life"),
    ("rolo", "Drama — romantic, neighborhood gossip, small conflicts"),
]

SETTINGS = [
    "Pelourinho", "Rio Vermelho", "Barra", "Itapuã", "Candeal",
    "Comércio", "Ribeira", "Pituba", "Campo Grande", "Liberdade",
    "Bonfim", "Feira de São Joaquim", "Mercado Modelo", "Elevador Lacerda",
    "Praia do Farol da Barra", "Dique do Tororó", "Cidade Baixa",
]


# ── Database ──────────────────────────────────────────────────────

def init_story_db():
    """Create story_library table."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS story_library (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            level           TEXT    NOT NULL,
            title           TEXT    NOT NULL,
            body            TEXT    NOT NULL,
            focus_words     TEXT    NOT NULL DEFAULT '[]',
            setting         TEXT,
            theme           TEXT,
            word_count      INTEGER,
            questions       TEXT    NOT NULL DEFAULT '[]',
            audio_chunks    TEXT,
            times_played    INTEGER NOT NULL DEFAULT 0,
            last_played     TEXT,
            comprehension_scores TEXT NOT NULL DEFAULT '[]',
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    conn.commit()
    print("story_library table ready.")


def get_story_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ── Vocabulary extraction ─────────────────────────────────────────

def get_tier_words(tiers, freq_cap=None):
    """Get words from specified tiers, optionally capped by frequency rank."""
    conn = get_story_connection()
    if freq_cap:
        rows = conn.execute(
            "SELECT word FROM word_bank WHERE frequency_rank <= ? ORDER BY frequency_rank",
            (freq_cap,),
        ).fetchall()
    else:
        placeholders = ",".join("?" * len(tiers))
        rows = conn.execute(
            f"SELECT word FROM word_bank WHERE difficulty_tier IN ({placeholders})",
            tiers,
        ).fetchall()
    conn.close()
    return [r["word"] for r in rows]


def get_focus_words(count=6):
    """Get words currently due for review — to weave into stories."""
    due = get_due_words()
    due_list = list(due)
    if not due_list:
        return []
    sample = random.sample(due_list, min(count, len(due_list)))
    return [r["word"] for r in sample]


# ── Story generation via OpenAI ───────────────────────────────────

SYSTEM_PROMPT = """Tu é um escritor soteropolitano de Salvador, Bahia. Escreve histórias em primeira pessoa com sotaque baiano natural.

REGRAS OBRIGATÓRIAS:
- Primeira pessoa (eu) SEMPRE
- Sotaque baiano: usa oxe, vixe, mainha, painho, tu + verbo na terceira pessoa, tá (não "está"), pra (não "para"), barril, arretado, massa, é mermo, lá ele, visse
- Cenário: Salvador, Bahia — menciona bairros reais, comidas, festas, cultura baiana
- SEM inglês. SEM português de Portugal. Brasileiro baiano puro.
- SEM narração em terceira pessoa. SEM diálogos com aspas (monólogo interno, conta a história como se tivesse conversando com alguém)
- Naturalidade: escreve como um baiano fala, não como um livro didático
- Usa contrações naturais: cê, tô, tá, pra, pro, dum, duma, num, numa"""

def build_generation_prompt(level_key, theme_name, theme_desc, setting, known_words_sample, focus_words, word_range):
    level = LEVELS[level_key]
    known_pct = int(level["known_pct"] * 100)
    min_words, max_words = word_range
    freq_cap = level.get("freq_cap")

    focus_str = ", ".join(focus_words) if focus_words else "(nenhuma)"

    # Starter levels get a strict word list
    if freq_cap:
        words_list = ", ".join(known_words_sample)
        vocab_block = f"""VOCABULÁRIO — RESTRIÇÃO RÍGIDA:
Usa APENAS estas palavras (as {freq_cap} mais frequentes do português brasileiro): {words_list}
- Pode conjugar verbos e usar formas gramaticais dessas palavras
- Pode usar preposições, artigos, pronomes básicos mesmo que não estejam na lista
- NÃO introduz palavras novas fora da lista. Se precisar de um conceito, descreve usando as palavras da lista
- Repete palavras e estruturas — isso é PROPOSITAL para um iniciante
- Frases curtas e simples. Sujeito + verbo + complemento.
- Palavras de foco (DEVEM aparecer muitas vezes): {focus_str}"""
    else:
        vocab_block = f"""VOCABULÁRIO:
- {known_pct}% das palavras devem ser simples e comuns (frequência alta no português brasileiro)
- Palavras de foco (DEVEM aparecer naturalmente na história, mais de uma vez): {focus_str}"""

    # Style prompt for levels that specify speech/accent characteristics
    style_prompt = level.get("style_prompt", "")
    style_block = f"\nESTILO DE FALA: {style_prompt}" if style_prompt else ""

    return f"""Escreve uma história LONGA em primeira pessoa. Isso é pra ouvir durante 10 minutos caminhando.

NÍVEL: {level_key} ({level['label']})
TEMA: {theme_desc}
CENÁRIO: {setting}, Salvador, Bahia
TAMANHO: {min_words}-{max_words} palavras — OBRIGATÓRIO atingir pelo menos {min_words} palavras. Isso é uma história completa, não um resumo.

{vocab_block}{style_block}

ESTRUTURA (história rica, não resumo):
1. ABERTURA — me situa: onde eu tô, que horas são, o que tá acontecendo ao meu redor, sensações (cheiro, barulho, calor)
2. CONTEXTO — conta a história de fundo, quem são as pessoas envolvidas, por que isso importa pra mim
3. DESENVOLVIMENTO — a coisa acontece aos poucos, com detalhes, diálogos internos, observações sobre as pessoas e os lugares
4. COMPLICAÇÃO — algo muda, um problema aparece, uma surpresa, um conflito
5. RESOLUÇÃO — como eu resolvi (ou não), o que aprendi, como isso me mudou
6. REFLEXÃO — pensamento final, conexão com a vida em Salvador, algo filosófico ou engraçado

Seja CRIATIVO. Inventa personagens com nomes baianos. Descreve os lugares com detalhes sensoriais (cheiro de acarajé, som do mar, calor do meio-dia). Usa gíria naturalmente. Conta a história como se tivesse falando com um amigo num bar em Rio Vermelho.

DEPOIS DA HISTÓRIA, gera {level['questions']} perguntas de compreensão.
Cada pergunta tem 4 opções (a, b, c, d) e uma resposta correta.
Mistura tipos: fatos da história, inferência, vocabulário no contexto, sentimento do narrador, detalhes culturais.

Responde EXATAMENTE neste formato JSON:
{{
  "title": "título criativo da história",
  "body": "texto COMPLETO da história com {min_words}-{max_words} palavras",
  "questions": [
    {{
      "question": "pergunta aqui?",
      "options": ["a) opção", "b) opção", "c) opção", "d) opção"],
      "correct": 0
    }}
  ]
}}

IMPORTANTE: A história DEVE ter pelo menos {min_words} palavras. Conta com calma, com detalhes, com descrições ricas. Se a história tiver menos de {min_words} palavras, está INCOMPLETA. Desenvolve cada parte da estrutura com pelo menos 3-4 parágrafos.

Responde APENAS o JSON, nada mais."""


def generate_story(level_key, theme=None, setting=None):
    """Generate a single story using OpenAI."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set.")
        return None

    level = LEVELS[level_key]

    # Pick theme and setting
    if theme is None:
        theme_name, theme_desc = random.choice(THEMES)
    else:
        theme_name, theme_desc = next(
            ((n, d) for n, d in THEMES if n == theme), random.choice(THEMES)
        )
    if setting is None:
        setting = random.choice(SETTINGS)

    # Get vocabulary
    freq_cap = level.get("freq_cap")
    known_words = get_tier_words(level["tiers"], freq_cap=freq_cap)
    known_sample = random.sample(known_words, min(80, len(known_words)))
    focus = get_focus_words(6)

    prompt = build_generation_prompt(
        level_key, theme_name, theme_desc, setting,
        known_sample, focus, level["word_count"],
    )

    import openai
    client = openai.OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.9,
        max_tokens=4096,
    )

    raw = response.choices[0].message.content.strip()

    # Parse JSON from response
    try:
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            if raw.endswith("```"):
                raw = raw[:-3]
            elif "```" in raw:
                raw = raw[:raw.rfind("```")]
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"ERROR: Failed to parse story JSON. Raw output:\n{raw[:500]}")
        return None

    title = data.get("title", "Sem título")
    body = data.get("body", "")
    questions = data.get("questions", [])
    word_count = len(body.split())

    # Save to database
    conn = get_story_connection()
    conn.execute(
        """INSERT INTO story_library
           (level, title, body, focus_words, setting, theme, word_count, questions)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            level_key,
            title,
            body,
            json.dumps(focus, ensure_ascii=False),
            setting,
            theme_name,
            word_count,
            json.dumps(questions, ensure_ascii=False),
        ),
    )
    conn.commit()
    story_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    print(f"  [{level_key}] \"{title}\" — {word_count} words, {len(questions)} questions (ID: {story_id})")

    # Post-generation hooks: chunk extraction & difficulty classification
    _run_post_generation_hooks(story_id)

    return story_id


def _run_post_generation_hooks(story_id):
    """Extract chunks and classify difficulty after story generation.

    Both operations are wrapped in try/except so they never block
    the story generation pipeline.
    """
    # Chunk extraction
    try:
        from chunk_engine import extract_chunks_from_story
        added = extract_chunks_from_story(story_id, DB_PATH)
        print(f"    -> Extracted {added} chunks from story {story_id}")
    except Exception as e:
        print(f"    -> Chunk extraction skipped: {e}")

    # Difficulty classification
    try:
        from content_ladder import classify_content
        level = classify_content("story", story_id, DB_PATH)
        print(f"    -> Classified story {story_id} as {level}")
    except Exception as e:
        print(f"    -> Classification skipped: {e}")


# ── TTS chunking ──────────────────────────────────────────────────

def chunk_text(text, max_chars=450):
    """Split text into chunks of 2-3 sentences, each under max_chars."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""

    for sent in sentences:
        if len(current) + len(sent) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = sent
        else:
            current = (current + " " + sent).strip()

    if current:
        chunks.append(current.strip())

    return chunks


def generate_story_audio(story_id):
    """Generate TTS audio for all chunks of a story."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set.")
        return None

    from elevenlabs import ElevenLabs
    client = ElevenLabs(api_key=api_key)

    conn = get_story_connection()
    row = conn.execute(
        "SELECT body, questions FROM story_library WHERE id = ?", (story_id,)
    ).fetchone()

    if not row:
        print(f"ERROR: Story {story_id} not found.")
        return None

    body = row["body"]
    questions = json.loads(row["questions"])

    # Chunk the story body
    chunks = chunk_text(body)
    audio_files = []

    print(f"  Generating audio for story {story_id} ({len(chunks)} chunks)...")

    for i, chunk in enumerate(chunks):
        fname = f"story_{story_id}_chunk_{i}.mp3"
        outpath = AUDIO_DIR / fname

        if outpath.exists():
            audio_files.append(fname)
            continue

        for attempt in range(5):
            try:
                audio_iter = client.text_to_speech.convert(
                    text=chunk,
                    voice_id="ELBrtmIkk40wCZ5YnlwM",  # Thiago — native Brazilian male, warm and inviting
                    model_id="eleven_multilingual_v2",
                    output_format="mp3_44100_128",
                    voice_settings={
                        "stability": 0.55,
                        "similarity_boost": 0.90,
                        "style": 0.45,
                        "use_speaker_boost": True,
                    },
                )

                with open(outpath, "wb") as f:
                    for audio_chunk in audio_iter:
                        f.write(audio_chunk)
                break
            except Exception as e:
                if "quota_exceeded" in str(e) or "quota" in str(e).lower():
                    print(f"    ⚠ ElevenLabs quota exceeded — skipping remaining audio")
                    # Save what we have so far
                    all_audio = {"story_chunks": audio_files, "question_audio": [], "chunk_texts": chunks}
                    conn.execute(
                        "UPDATE story_library SET audio_chunks = ? WHERE id = ?",
                        (json.dumps(all_audio), story_id),
                    )
                    conn.commit()
                    conn.close()
                    print(f"  Audio partial: {len(audio_files)} chunks saved (quota hit)")
                    return
                if "429" in str(e) or "rate" in str(e).lower():
                    wait = 2 ** attempt * 3
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        audio_files.append(fname)
        print(f"    Chunk {i+1}/{len(chunks)}: {fname}")

    # Generate question audio
    question_files = []
    for qi, q in enumerate(questions):
        fname = f"story_{story_id}_q_{qi}.mp3"
        outpath = AUDIO_DIR / fname

        if outpath.exists():
            question_files.append(fname)
            continue

        q_text = q["question"]
        for attempt in range(5):
            try:
                audio_iter = client.text_to_speech.convert(
                    text=q_text,
                    voice_id="ELBrtmIkk40wCZ5YnlwM",  # Thiago — native Brazilian male, warm and inviting
                    model_id="eleven_multilingual_v2",
                    output_format="mp3_44100_128",
                    voice_settings={
                        "stability": 0.55,
                        "similarity_boost": 0.90,
                        "style": 0.45,
                        "use_speaker_boost": True,
                    },
                )

                with open(outpath, "wb") as f:
                    for audio_chunk in audio_iter:
                        f.write(audio_chunk)
                break
            except Exception as e:
                if "quota_exceeded" in str(e) or "quota" in str(e).lower():
                    print(f"    ⚠ ElevenLabs quota exceeded — skipping remaining question audio")
                    break
                if "429" in str(e) or "rate" in str(e).lower():
                    wait = 2 ** attempt * 3
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        question_files.append(fname)
        print(f"    Question {qi+1}/{len(questions)}: {fname}")

    # Save audio file list + chunk text segments to DB
    all_audio = {"story_chunks": audio_files, "question_audio": question_files, "chunk_texts": chunks}
    conn.execute(
        "UPDATE story_library SET audio_chunks = ? WHERE id = ?",
        (json.dumps(all_audio), story_id),
    )
    conn.commit()
    conn.close()

    print(f"  Audio complete: {len(audio_files)} chunks + {len(question_files)} questions")
    return all_audio


# ── Listing ───────────────────────────────────────────────────────

def list_stories():
    conn = get_story_connection()
    rows = conn.execute(
        "SELECT id, level, title, word_count, times_played, created_at FROM story_library ORDER BY level, id"
    ).fetchall()
    conn.close()

    if not rows:
        print("No stories yet. Run: python3 story_gen.py --generate --level A1 --count 3")
        return

    print(f"\n  {'ID':<5} {'Level':<5} {'Words':<7} {'Played':<8} Title")
    print(f"  {'─'*60}")
    for r in rows:
        print(f"  {r['id']:<5} {r['level']:<5} {r['word_count']:<7} {r['times_played']:<8} {r['title']}")
    print()


# ── CLI ───────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return

    if "--init" in sys.argv:
        init_story_db()
        return

    if "--list" in sys.argv:
        list_stories()
        return

    if "--generate" in sys.argv:
        init_story_db()
        level = "A1"
        count = 3
        if "--level" in sys.argv:
            idx = sys.argv.index("--level")
            if idx + 1 < len(sys.argv):
                level = sys.argv[idx + 1].upper()
        if "--count" in sys.argv:
            idx = sys.argv.index("--count")
            if idx + 1 < len(sys.argv):
                count = int(sys.argv[idx + 1])

        if level not in LEVELS:
            print(f"ERROR: Unknown level '{level}'. Valid: {', '.join(LEVELS.keys())}")
            return

        text_only = "--text-only" in sys.argv
        print(f"\nGenerating {count} stories at level {level} ({LEVELS[level]['label']}){'  [text only]' if text_only else ''}...\n")
        for i in range(count):
            story_id = generate_story(level)
            if story_id and not text_only:
                generate_story_audio(story_id)
        print("\nDone.")
        return

    if "--generate-all" in sys.argv:
        init_story_db()
        count = 3
        if "--count" in sys.argv:
            idx = sys.argv.index("--count")
            if idx + 1 < len(sys.argv):
                count = int(sys.argv[idx + 1])

        for level_key in LEVELS:
            print(f"\n{'='*50}")
            print(f"  Level {level_key} ({LEVELS[level_key]['label']})")
            print(f"{'='*50}")
            for i in range(count):
                story_id = generate_story(level_key)
                if story_id:
                    generate_story_audio(story_id)
        print("\nAll levels done.")
        return

    if "--audio" in sys.argv:
        if "--id" in sys.argv:
            idx = sys.argv.index("--id")
            if idx + 1 < len(sys.argv):
                generate_story_audio(int(sys.argv[idx + 1]))
                return
        print("Usage: python3 story_gen.py --audio --id <story_id>")
        return

    print(__doc__.strip())


if __name__ == "__main__":
    main()
