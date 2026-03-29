"""
drill_server.py — Autonomous mobile drill server for the Oxe Protocol.

Runs a web server that serves a full 1+T drill UI to your phone.
No Claude Code interaction needed — just start the server and open
the URL on your phone.

Workflow per cycle:
  1. Query SRS for next due word
  2. Build Baiano chunk + carrier sentence
  3. Generate ElevenLabs TTS
  4. Phone plays audio automatically
  5. You tap after shadowing → latency measured
  6. FSRS review logged, next word served

Usage:
    source ~/.profile && python3 drill_server.py              # port 7777
    source ~/.profile && python3 drill_server.py --port 9000  # custom port
    source ~/.profile && python3 drill_server.py --count 20   # 20-word session
"""

import http.server
import json
import os
import random
import socket
import sys
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fsrs import Rating

from srs_engine import (
    get_next_word, get_due_words, record_review,
    get_unlocked_tier, tier_progress, TIER_LABELS,
    get_next_chunk, get_due_chunks, update_chunk_pass,
    record_chunk_review, get_chunk_by_id, add_chunk,
)
from training_modes import select_mode_for_item, get_drill_config, TRAINING_MODES
from daily_router import get_next_block, record_block_completion
from acquisition_engine import update_state_after_review, check_replay_reinforcement
from fatigue_monitor import record_review_event

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
IMAGE_DIR = Path(__file__).parent / "voca_vault" / "images"
LOG_DIR = Path(__file__).parent / "voca_vault" / "logs"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

LATENCY_THRESHOLD_MS = 1000
TRAP_PROBABILITY = 0.15
TRAP_LATENCY_MS = 800

# ── Carrier sentence building ─────────────────────────────────────

INTERJECTIONS = [
    "Oxe,", "Vixe,", "Rapaz,", "Eita,", "Meu irmão,",
    "Ave Maria,", "Ô xente,", "Ô meu,", "Ei,", "Pô,",
]
TAGS = [
    "viu!", "visse.", "tá ligado?", "rapaz.", "né?",
    "é mermo.", "sabe como é.", "acredita?", "entendeu?",
    "tá vendo?", "meu rei.", "ó.", "bora?",
]
LOCATIONS = [
    "no Pelourinho", "lá no Rio Vermelho", "na Barra", "em Itapuã",
    "no Candeal", "no Comércio", "na Ribeira", "na Pituba",
    "ali no Farol", "lá no Subúrbio", "na Cidade Baixa",
]
CARRIERS = [
    "{intj} cê sabe o que é {word}? {tag}",
    "{intj} ontem eu vi um negócio de {word} {loc}, {tag}",
    "Eu tava pensando em {word} agora mesmo, {tag}",
    "{intj} {word} é uma coisa que todo baiano conhece, {tag}",
    "Cê já ouviu falar de {word}? {tag}",
    "A gente sempre fala de {word} {loc}, {tag}",
    "{intj} sem {word} num dá pra viver, {tag}",
    "{intj} {word} é barril demais, {tag}",
    "Tô ligado em {word} desde moleque, {tag}",
    "{intj} o cara tava falando de {word} {loc}, {tag}",
    "Cê num sabe o que é {word}? {tag}",
    "{intj} eu tô doido pra saber mais de {word}, {tag}",
    "Mano, {word} é tipo coisa daqui memo, {tag}",
    "{intj} peguei {word} lá {loc}, {tag}",
    "Ó, {word} aqui é diferente, {tag}",
    "Eu tava lá {loc} e pensei em {word}, {tag}",
    "{intj} cê precisa ver {word}, {tag}",
    "Num tem como falar de {loc} sem falar de {word}, {tag}",
]

# ── Spoken-form contractions (formal → Baiano oral) ──────────
# Applied to carrier sentences to match real Brazilian speech
_SPOKEN_FORMS = [
    # Order matters — longer/more-specific patterns first
    (r'\bcom você\b', 'contigo'),
    (r'\bvocê\b', 'cê'),
    (r'\bestou\b', 'tô'),
    (r'\bestá\b', 'tá'),
    (r'\bestava\b', 'tava'),
    (r'\bestavam\b', 'tavam'),
    (r'\bestamos\b', 'tamo'),
    (r'\bpara o\b', 'pro'),
    (r'\bpara a\b', 'pra'),
    (r'\bpara os\b', 'pros'),
    (r'\bpara as\b', 'pras'),
    # "para" as preposition before verbs/pronouns — NOT the verb "parar"
    (r'\bpara (mim|ti|nós|eles|elas|eu|tu|ele|ela|onde|quem|que|quando)\b', r'pra \1'),
    # Tag question patterns → né
    (r'\bnão é não\b', 'né'),
    (r'\bnão é\s*\?', 'né?'),
    (r'\bnão é,', 'né,'),
    (r', não\?', ', né?'),
    (r', não$', ', né'),
    (r'\bnão\b', 'num'),
    (r'\bem um\b', 'num'),
    (r'\bem uma\b', 'numa'),
    # "vamos" only at sentence start (imperative) — not after nós/não/que/etc.
    (r'^vamos\b', 'bora'),
    (r'(?<=[,!])\s*vamos\b', ' bora'),
    (r'\bdepois\b', 'dipois'),
    (r'\bmesmo\b', 'memo'),
    (r'\bmenino\b', 'moleque'),
    (r'\bdinheiro\b', 'grana'),
]
import re as _carrier_re

def _to_spoken_form(text):
    """Convert formal PT to spoken Baiano contractions."""
    for pattern, replacement in _SPOKEN_FORMS:
        text = _carrier_re.sub(pattern, replacement, text, flags=_carrier_re.IGNORECASE)
    # Fix capitalization at sentence start
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text

TRAP_SENTENCES = [
    ("Eu corro mais que o ônibus na Barra.", "brag",
     "Lá ele! Ninguém corre mais que ônibus."),
    ("O cara falou que come 30 acarajés de uma vez.", "brag",
     "Lá ele! 30 acarajés? Vai explodir."),
    ("Disseram que vai nevar em Salvador amanhã.", "absurd",
     "Oxe! Nevar em Salvador? Tá maluco?"),
    ("Aquele ali disse que nunca suou na vida.", "absurd",
     "Vixe! Em Salvador e nunca suou? Lá ele!"),
    ("Esse celular aqui é original, só 50 reais.", "hustle",
     "Lá ele! Celular original por 50 conto?"),
    ("Investe comigo que triplica em uma semana.", "hustle",
     "Oxente! Triplica em uma semana? Lá ele!"),
    ("Tu quer ver meu negócio grande lá no Comércio?", "malicia",
     "Oxe! Que negócio grande é esse?"),
    ("Ela disse que faz vatapá melhor que a Dinha.", "brag",
     "Oxente! Melhor que a Dinha? Lá ele!"),
]

TRAP_REACTIONS = {"lá ele", "la ele", "oxe", "oxente", "eita", "vixe"}

# Session state
_laranjada_remaining = 0


def build_carrier(word):
    t = random.choice(CARRIERS)
    raw = t.format(
        intj=random.choice(INTERJECTIONS),
        word=word,
        tag=random.choice(TAGS),
        loc=random.choice(LOCATIONS),
    )
    return _to_spoken_form(raw)


def _baiano_tts_text(text):
    """Wrap short text with a Baiano carrier to nudge pronunciation toward
    Soteropolitano.  Carrier sentences (4+ words) already have enough
    regional context, so they pass through unchanged."""
    words = text.strip().split()
    if len(words) <= 3:
        return f"Oxe, {text}"
    return text


def generate_tts(text, raw=False):
    """Generate TTS, return filename. Returns None on any failure (quota, network, etc).

    Args:
        text: Text to speak.
        raw: If True, speak the text exactly as-is (no Baiano interjection prefix).
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("[TTS] WARNING: ELEVENLABS_API_KEY not set — no audio will be generated")
        return None

    try:
        from elevenlabs import ElevenLabs
        client = ElevenLabs(api_key=api_key)

        tts_text = text if raw else _baiano_tts_text(text)
        audio_iter = client.text_to_speech.convert(
            text=tts_text,
            voice_id="ELBrtmIkk40wCZ5YnlwM",  # Thiago — native Brazilian male, warm and inviting
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",
            voice_settings={
                "stability": 0.45,
                "similarity_boost": 0.85,
                "style": 0.55,
                "use_speaker_boost": True,
            },
        )

        fname = f"tts_{int(time.time() * 1000)}.mp3"
        outpath = AUDIO_DIR / fname
        with open(outpath, "wb") as f:
            for chunk in audio_iter:
                f.write(chunk)
        return fname
    except Exception as e:
        print(f"[TTS] Error generating audio for '{text[:50]}': {e}")
        return None


import threading

_image_lock = threading.Lock()
_image_pending = set()


def _image_fname(word, carrier_sentence=None):
    """Build a unique image filename based on word + carrier sentence."""
    safe = "".join(c if c.isalnum() else "_" for c in word)
    if carrier_sentence and len(carrier_sentence) > 10:
        import hashlib
        h = hashlib.md5(carrier_sentence.encode()).hexdigest()[:8]
        return f"img_{safe}_{h}.png"
    return f"img_{safe}.png"


def get_cached_image(word, carrier_sentence=None):
    """Return cached image filename if it exists, else None."""
    fname = _image_fname(word, carrier_sentence)
    cached = IMAGE_DIR / fname
    if cached.exists() and cached.stat().st_size > 0:
        return fname
    # Fallback: check word-only image (legacy)
    safe = "".join(c if c.isalnum() else "_" for c in word)
    legacy = f"img_{safe}.png"
    legacy_path = IMAGE_DIR / legacy
    if legacy_path.exists() and legacy_path.stat().st_size > 0:
        return legacy
    return None


import random as _img_random

_BAHIA_SCENES = [
    "a lively street in Pelourinho with colorful colonial facades",
    "a sunset beach at Porto da Barra with palm trees and sand",
    "a bustling Mercado Modelo market stall with local crafts",
    "a capoeira roda on a cobblestone square in Salvador",
    "a vibrant Carnaval scene with axé dancers in colorful costumes",
    "a fishing boat on the Bay of All Saints at golden hour",
    "a baiana de acarajé cooking at her street cart",
    "a view from Elevador Lacerda looking over the lower city",
    "a terreiro ceremony with candles and flowers",
    "a lively boteco bar with friends sharing petiscos",
    "a Salvador rooftop overlooking the ocean at dusk",
    "a tropical garden courtyard with bougainvillea and mango trees",
]

_STYLES = [
    "warm tropical watercolor illustration, rich saturated colors",
    "bold flat-color digital illustration, vibrant palette",
    "lush painted illustration with golden-hour lighting",
    "colorful graphic novel style with strong outlines",
    "impressionist tropical scene with loose brushstrokes",
]


def _build_image_prompt(word, carrier_sentence=None):
    """Build a diverse, semantically-rich DALL-E prompt for a word/chunk."""
    scene = _img_random.choice(_BAHIA_SCENES)
    style = _img_random.choice(_STYLES)

    if carrier_sentence and len(carrier_sentence) > 10:
        # Use the full carrier sentence for context — much more specific
        prompt = (
            f"Illustrate the scene: \"{carrier_sentence}\" — "
            f"set in {scene}. "
            f"Focus on the action and emotion of the moment. "
            f"No text, no words, no letters in the image. "
            f"{style}."
        )
    else:
        # Fallback for bare words
        prompt = (
            f"A striking illustration showing the meaning of the Portuguese word '{word}' — "
            f"depict a specific moment or action that embodies this word, "
            f"set in {scene}. "
            f"Make the central subject unique and memorable. "
            f"No text, no words, no letters in the image. "
            f"{style}."
        )
    return prompt


def _bg_generate_image(word, carrier_sentence=None):
    """Background thread: generate DALL-E image and cache it."""
    fname = _image_fname(word, carrier_sentence)
    cached = IMAGE_DIR / fname
    try:
        import openai
        client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
        prompt = _build_image_prompt(word, carrier_sentence)
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        import urllib.request
        urllib.request.urlretrieve(resp.data[0].url, str(cached))
    except Exception as e:
        print(f"[DALL-E] Error for '{word}': {e}")
    finally:
        with _image_lock:
            _image_pending.discard(word)


def generate_image(word, carrier_sentence=None):
    """Return cached image or kick off background generation. Non-blocking."""
    cached = get_cached_image(word, carrier_sentence)
    if cached:
        return cached
    # Fire background generation — don't block the response
    key = f"{word}_{carrier_sentence[:20] if carrier_sentence else ''}"
    with _image_lock:
        if key not in _image_pending:
            _image_pending.add(key)
            t = threading.Thread(target=_bg_generate_image, args=(word, carrier_sentence), daemon=True)
            t.start()
    return None


def prefetch_images(words, carriers=None):
    """Pre-generate images for a list of words in background threads (with policy check)."""
    try:
        from image_policy import should_generate_image
    except ImportError:
        should_generate_image = lambda _: True
    for i, w in enumerate(words):
        if not should_generate_image(w):
            continue
        carrier = carriers[i] if carriers and i < len(carriers) else None
        if get_cached_image(w, carrier):
            continue
        key = f"{w}_{carrier[:20] if carrier else ''}"
        with _image_lock:
            if key not in _image_pending:
                _image_pending.add(key)
                t = threading.Thread(target=_bg_generate_image, args=(w, carrier), daemon=True)
                t.start()


def generate_explanation(word):
    """Generate a simple PT explanation of the word using GPT-4o, then TTS it. Returns audio filename or None."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, None

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=120,
            temperature=0.7,
            messages=[{
                "role": "system",
                "content": (
                    "Tu é um parceiro baiano de Salvador. Explica o significado da palavra "
                    "usando só as 1000 palavras mais comuns do português. "
                    "Máximo 2 frases curtas. Fala natural, tipo conversa de rua. "
                    "Usa elisões e contrações reais: tá, tô, tava, cê, né, pro, pra, num, "
                    "peraí, belê. NUNCA escreva 'está', 'você', 'para o'. "
                    "Sem inglês. Sem markdown."
                ),
            }, {
                "role": "user",
                "content": f"Explica o que significa: {word}",
            }],
        )
        explanation = resp.choices[0].message.content.strip()
        audio_fname = generate_tts(explanation)
        return explanation, audio_fname
    except Exception as e:
        print(f"[Explain] Error for '{word}': {e}")
        return None, None


def build_cloze(word, carrier):
    """Replace the target word in the carrier with '...' for cloze drills."""
    import re
    cloze_text = re.sub(re.escape(word), '...', carrier, count=1, flags=re.IGNORECASE)
    return cloze_text, carrier


def score_pronunciation(user_audio_path, native_audio_path):
    """Convert uploaded audio to WAV and score against native using biometric_checker."""
    import subprocess
    import tempfile

    user_wav = user_audio_path
    # Convert to WAV if not already
    if not str(user_audio_path).endswith('.wav'):
        wav_path = str(user_audio_path).rsplit('.', 1)[0] + '.wav'
        try:
            subprocess.run(
                ['afconvert', str(user_audio_path), wav_path, '-d', 'LEI16', '-f', 'WAVE'],
                check=True, capture_output=True, timeout=10,
            )
            user_wav = wav_path
        except Exception as e:
            print(f"[afconvert] Error: {e}")
            user_wav = str(user_audio_path)

    native_wav = native_audio_path
    if not str(native_audio_path).endswith('.wav'):
        native_wav_path = str(native_audio_path).rsplit('.', 1)[0] + '_native.wav'
        try:
            subprocess.run(
                ['afconvert', str(native_audio_path), native_wav_path, '-d', 'LEI16', '-f', 'WAVE'],
                check=True, capture_output=True, timeout=10,
            )
            native_wav = native_wav_path
        except Exception:
            native_wav = str(native_audio_path)

    from biometric_checker import full_analysis
    result = full_analysis(str(user_wav), str(native_wav))
    return result


def log_drill(word_id, word, rating, latency_ms, drill_type="drill"):
    log_file = LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "word_id": word_id,
        "word": word,
        "rating": rating,
        "latency_ms": latency_ms,
        "type": drill_type,
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_mode_for_next_item():
    """Determine the training mode for the next drill item.

    Checks daily_router.get_next_block() first for the block-level mode.
    Falls back to training_modes.select_mode_for_item() for individual items.

    Returns:
        Mode key string (e.g. 'audio_meaning_recognition').
    """
    try:
        block = get_next_block()
        if block and block.get("mode") and block["mode"] in TRAINING_MODES:
            return block["mode"]
        # If block has target items, use the first item to select mode
        if block and block.get("target_items"):
            first = block["target_items"][0]
            item_type = first.get("item_type", "word")
            item_id = first.get("item_id") or first.get("id")
            if item_id:
                return select_mode_for_item(item_type, item_id)
    except Exception:
        pass
    return "audio_meaning_recognition"


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── HTML UI ────────────────────────────────────────────────────────

DRILL_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Oxe Protocol</title>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  @keyframes fadeUp { from{opacity:0;transform:translateY(12px)} to{opacity:1;transform:translateY(0)} }
  @keyframes pulse { 0%,100%{transform:scale(1);opacity:1} 50%{transform:scale(1.25);opacity:0.6} }
  @keyframes repPulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.15);opacity:0.7} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    display: flex; flex-direction: column; min-height: 100vh; min-height: 100dvh;
    overflow: hidden; -webkit-user-select: none; user-select: none;
  }

  /* ── Header ── */
  .header {
    padding: 16px 20px; background: rgba(255,255,255,0.03);
    border-bottom: 2px solid transparent;
    border-image: linear-gradient(90deg, #3B82F6, #7C5CFC) 1;
    display: flex; justify-content: space-between; align-items: center;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .header h1 {
    font-size: 1.1em; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .stats { font-size: 0.75em; color: #6b7280; text-align: right; }

  /* ── Progress Dots ── */
  .progress-dots {
    display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 14px 0 4px;
  }
  .dots-row {
    display: flex; gap: 16px; align-items: center; justify-content: center;
  }
  .dot {
    width: 12px; height: 12px; border-radius: 50%;
    background: rgba(255,255,255,0.1); transition: all 0.3s;
  }
  .dot.completed { background: #34d399; }
  .dot.current { background: #60a5fa; width: 16px; height: 16px; animation: pulse 1.8s infinite; }
  .pass-label {
    font-size: 0.75em; color: #60a5fa; font-weight: 600; letter-spacing: 0.5px;
    text-transform: uppercase; min-height: 1.2em;
  }

  /* ── Main ── */
  .main {
    flex: 1; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 16px 20px; gap: 18px;
  }
  .main > * { animation: fadeUp 0.4s ease-out; }
  .drill-image {
    width: 200px; height: 200px; border-radius: 20px; object-fit: cover;
    border: 1px solid rgba(255,255,255,0.08); display: none;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3), 0 0 40px rgba(59,130,246,0.08);
  }
  .drill-image.visible { display: block; animation: fadeUp 0.4s ease-out; }
  .carrier-text {
    font-size: 1.15em; color: #d1d5db; text-align: center; line-height: 1.5;
    min-height: 1.5em; max-width: 340px; transition: opacity 0.3s;
  }
  .carrier-text .highlight { color: #60a5fa; font-weight: 700; }
  .carrier-text.hidden { opacity: 0; pointer-events: none; height: 0; min-height: 0; overflow: hidden; }
  .pass-instruction {
    font-size: 0.8em; color: #525263; text-align: center; min-height: 1.2em;
  }

  /* ── Rep Counter (Pass 5) ── */
  .rep-counter {
    font-size: 3.5em; font-weight: 800; text-align: center;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    animation: repPulse 1s ease-in-out; min-height: 1.2em;
    display: none;
  }
  .rep-counter.visible { display: block; }

  /* ── Buttons ── */
  .action-area {
    display: flex; gap: 12px; width: 100%; max-width: 340px; justify-content: center;
    min-height: 64px; align-items: center;
  }
  .btn-primary {
    flex: 1; padding: 20px 24px; font-size: 1.25em; font-weight: 700;
    border: none; border-radius: 20px; cursor: pointer;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    transition: all 0.2s; -webkit-tap-highlight-color: transparent;
    box-shadow: 0 0 20px rgba(59,130,246,0.25), 0 4px 12px rgba(0,0,0,0.3);
    letter-spacing: 0.5px;
  }
  .btn-primary:active { transform: scale(0.97); opacity: 0.9; }
  .btn-primary:disabled { background: rgba(255,255,255,0.04); color: #444; box-shadow: none; cursor: default; }
  .btn-secondary {
    padding: 20px 24px; font-size: 1.1em; font-weight: 600;
    border: 1px solid rgba(255,255,255,0.1); border-radius: 20px; cursor: pointer;
    background: rgba(255,255,255,0.04); color: #9ca3af;
    transition: all 0.2s; -webkit-tap-highlight-color: transparent;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  }
  .btn-secondary:active { transform: scale(0.97); background: rgba(255,255,255,0.08); }

  /* ── Rating Feedback ── */
  .rating-feedback {
    font-size: 1.1em; font-weight: 600; text-align: center; min-height: 1.5em;
    color: #34d399; opacity: 0; transition: opacity 0.3s;
  }
  .rating-feedback.visible { opacity: 1; }

  /* ── Footer ── */
  .footer {
    padding: 14px 24px 80px; background: rgba(255,255,255,0.02);
    border-top: 1px solid rgba(255,255,255,0.06);
    display: flex; justify-content: space-between; align-items: center;
    font-size: 0.75em; color: #6b7280; letter-spacing: 0.3px;
  }
  .tier-badge {
    font-size: 0.75em; padding: 4px 14px; border-radius: 999px;
    background: rgba(59,130,246,0.08); color: #60a5fa; display: inline-block;
    border: 1px solid transparent; background-clip: padding-box; position: relative;
  }
  .tier-badge::before {
    content: ''; position: absolute; inset: 0; border-radius: 999px; padding: 1px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude; pointer-events: none;
  }
  .loading-text { color: #525263; font-size: 1.2em; }

  /* ── Mode Banner ── */
  .mode-banner {
    text-align: center; padding: 6px 16px;
    font-size: 0.72em; font-weight: 700; letter-spacing: 1.2px;
    text-transform: uppercase; color: #5E6AD2;
    background: rgba(94,106,210,0.06);
    border-bottom: 1px solid rgba(94,106,210,0.12);
    min-height: 1.6em; transition: opacity 0.3s;
  }
  .mode-banner.hidden { opacity: 0; min-height: 0; padding: 0; overflow: hidden; }

  /* ── Countdown Timer ── */
  .countdown-bar {
    width: 100%; max-width: 340px; height: 4px; border-radius: 2px;
    background: rgba(255,255,255,0.06); overflow: hidden; display: none;
  }
  .countdown-bar.visible { display: block; }
  .countdown-fill {
    height: 100%; background: linear-gradient(90deg, #5E6AD2, #3B82F6);
    border-radius: 2px; transition: width 0.1s linear;
    width: 100%;
  }
  .countdown-label {
    font-size: 0.7em; color: #6b7280; text-align: center;
    min-height: 1em; display: none;
  }
  .countdown-label.visible { display: block; }

  /* ── Biometric Score ── */
  .biometric-score {
    font-size: 0.85em; font-weight: 600; text-align: center;
    color: #5E6AD2; min-height: 1em; display: none;
  }
  .biometric-score.visible { display: block; animation: fadeUp 0.4s ease-out; }

  /* ── Latency Trend ── */
  @keyframes fadeOutDown { from{opacity:1;transform:translateY(0)} to{opacity:0;transform:translateY(8px)} }
  .latency-trend {
    font-size: 0.8em; font-weight: 600; text-align: center; min-height: 1.2em;
    display: none; gap: 6px; align-items: center; justify-content: center;
  }
  .latency-trend.visible { display: flex; animation: fadeUp 0.3s ease-out; }
  .latency-trend.fading { animation: fadeOutDown 0.5s ease-in forwards; }
  .latency-trend .trend-arrow { font-size: 1.2em; }
  .latency-trend .trend-ms { opacity: 0.8; }

  /* ── State Celebration Overlay ── */
  @keyframes celebGlow { 0%{box-shadow:0 0 30px rgba(52,211,153,0.3)} 50%{box-shadow:0 0 60px rgba(52,211,153,0.6)} 100%{box-shadow:0 0 30px rgba(52,211,153,0.3)} }
  @keyframes celebSlideIn { from{opacity:0;transform:translateY(-30px) scale(0.9)} to{opacity:1;transform:translateY(0) scale(1)} }
  @keyframes celebSlideOut { from{opacity:1;transform:translateY(0) scale(1)} to{opacity:0;transform:translateY(30px) scale(0.9)} }
  .state-celebration {
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    padding: 24px 20px; text-align: center;
    background: rgba(52,211,153,0.12);
    border-bottom: 2px solid rgba(52,211,153,0.4);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    animation: celebSlideIn 0.4s ease-out, celebGlow 1.5s ease-in-out infinite;
    display: none;
  }
  .state-celebration.visible { display: block; }
  .state-celebration.dismissing { animation: celebSlideOut 0.5s ease-in forwards; }
  .state-celebration .celeb-state { font-size: 1.4em; font-weight: 800; color: #34d399; }
  .state-celebration .celeb-label { font-size: 0.85em; color: #a7f3d0; margin-top: 4px; }

  /* ── Fragile Tags ── */
  .fragile-tags {
    display: flex; flex-wrap: wrap; gap: 6px; justify-content: center;
    min-height: 0; padding: 0 16px;
  }
  .fragile-tag {
    font-size: 0.68em; font-weight: 600; padding: 3px 10px;
    border-radius: 999px; letter-spacing: 0.3px;
  }
  .fragile-tag.known_but_slow { background: rgba(250,204,21,0.2); color: #fbbf24; }
  .fragile-tag.familiar_but_fragile { background: rgba(251,146,60,0.2); color: #fb923c; }
  .fragile-tag.text_only { background: rgba(248,113,113,0.2); color: #f87171; }
  .fragile-tag.clean_audio_only { background: rgba(96,165,250,0.2); color: #60a5fa; }
  .fragile-tag.blocked_by_prosody { background: rgba(168,85,247,0.2); color: #a855f7; }

  /* ── Prosody Pills ── */
  .prosody-pills {
    display: flex; flex-direction: column; gap: 4px; width: 100%; max-width: 340px;
    margin-top: 6px; display: none;
  }
  .prosody-pills.visible { display: flex; animation: fadeUp 0.3s ease-out; }
  .prosody-pills.fading { animation: fadeOutDown 0.5s ease-in forwards; }
  .prosody-pill {
    font-size: 0.7em; padding: 6px 10px; border-radius: 8px;
    background: rgba(255,255,255,0.04); border-left: 3px solid #5E6AD2;
    color: #d1d5db;
  }
  .prosody-pill .pill-dim { color: #fbbf24; font-weight: 600; }
  .prosody-pill .pill-score { color: #7a7a8e; margin-left: 4px; }

  /* ── Fatigue Banner / Modal ── */
  .fatigue-banner {
    position: fixed; top: 0; left: 0; right: 0; z-index: 90;
    padding: 14px 20px; text-align: center;
    background: rgba(251,191,36,0.12);
    border-bottom: 1px solid rgba(251,191,36,0.3);
    backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    display: none; font-size: 0.85em; color: #fbbf24;
  }
  .fatigue-banner.visible { display: flex; align-items: center; justify-content: center; gap: 10px; animation: fadeUp 0.3s ease-out; }
  .fatigue-banner .fatigue-dismiss {
    background: none; border: 1px solid rgba(251,191,36,0.4); color: #fbbf24;
    padding: 4px 12px; border-radius: 8px; font-size: 0.85em; cursor: pointer;
  }
  .fatigue-banner .fatigue-action {
    background: rgba(251,191,36,0.2); border: none; color: #fbbf24;
    padding: 6px 14px; border-radius: 8px; font-size: 0.85em; font-weight: 600; cursor: pointer;
  }
  .fatigue-modal {
    position: fixed; inset: 0; z-index: 110;
    background: rgba(10,10,11,0.92);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    display: none; flex-direction: column; align-items: center; justify-content: center; gap: 16px;
  }
  .fatigue-modal.visible { display: flex; animation: fadeUp 0.4s ease-out; }
  .fatigue-modal .modal-icon { font-size: 3em; }
  .fatigue-modal .modal-text { font-size: 1.2em; font-weight: 700; color: #fbbf24; text-align: center; }
  .fatigue-modal .modal-sub { font-size: 0.9em; color: #9ca3af; }
  .fatigue-modal .modal-timer { font-size: 2em; font-weight: 800; color: #fbbf24; font-variant-numeric: tabular-nums; }
  .fatigue-modal .modal-btn {
    padding: 14px 32px; font-size: 1.1em; font-weight: 700; border: none; border-radius: 16px;
    background: linear-gradient(135deg, #fbbf24, #f59e0b); color: #0a0a0b; cursor: pointer;
    margin-top: 8px;
  }

  /* ── Listening Layer Pills ── */
  .listening-layers {
    display: flex; gap: 6px; justify-content: center; padding: 8px 16px;
    width: 100%; max-width: 360px; margin: 0 auto;
    display: none;
  }
  .listening-layers.visible { display: flex; animation: fadeUp 0.3s ease-out; }
  .layer-pill {
    flex: 1; padding: 6px 4px; font-size: 0.65em; font-weight: 700;
    text-align: center; border-radius: 12px; cursor: pointer;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.03); color: #6b7280;
    transition: all 0.3s ease; letter-spacing: 0.2px;
    position: relative; overflow: hidden;
    -webkit-tap-highlight-color: transparent;
  }
  .layer-pill::before {
    content: ''; position: absolute; bottom: 0; left: 0; width: 0; height: 3px;
    background: linear-gradient(90deg, #3B82F6, #7C5CFC);
    border-radius: 0 0 12px 12px; transition: width 0.4s ease;
  }
  .layer-pill.active {
    background: rgba(59,130,246,0.12); color: #60a5fa;
    border-color: rgba(59,130,246,0.3);
    box-shadow: 0 0 12px rgba(59,130,246,0.15);
  }
  .layer-pill.active::before { width: 100%; }
  .layer-pill.completed {
    background: rgba(52,211,153,0.08); color: #34d399;
    border-color: rgba(52,211,153,0.25);
  }
  .layer-pill.completed::before {
    width: 100%; background: linear-gradient(90deg, #34d399, #10b981);
  }
  .layer-pill.locked {
    opacity: 0.35; cursor: default;
  }
  .layer-pill .layer-icon {
    display: block; font-size: 1.4em; line-height: 1; margin-bottom: 2px;
  }

  /* ── Tab Bar — injected by TAB_BAR_HTML ── */
</style>
</head><body>

<div class="header">
  <h1>OXE PROTOCOL</h1>
  <div class="stats">
    <div><span id="session-count">0</span> na sess&atilde;o</div>
    <div><span id="session-accuracy">0</span>% acertos</div>
  </div>
</div>

<div class="fatigue-banner" id="fatigue-banner"></div>
<div class="state-celebration" id="state-celebration">
  <div class="celeb-state" id="celeb-state"></div>
  <div class="celeb-label" id="celeb-label"></div>
</div>
<div class="fatigue-modal" id="fatigue-modal">
  <div class="modal-icon" id="modal-icon"></div>
  <div class="modal-text" id="modal-text"></div>
  <div class="modal-sub" id="modal-sub"></div>
  <div class="modal-timer" id="modal-timer"></div>
  <button class="modal-btn" id="modal-btn" style="display:none"></button>
</div>

<div class="mode-banner hidden" id="mode-banner"></div>
<div class="listening-layers" id="listening-layers">
  <div class="layer-pill active" id="layer-clean" data-layer="clean" onclick="selectLayer('clean')">
    <span class="layer-icon">&#x1F50A;</span>Limpo
  </div>
  <div class="layer-pill locked" id="layer-native_clear" data-layer="native_clear" onclick="selectLayer('native_clear')">
    <span class="layer-icon">&#x1F5E3;</span>Nativo
  </div>
  <div class="layer-pill locked" id="layer-native_fast" data-layer="native_fast" onclick="selectLayer('native_fast')">
    <span class="layer-icon">&#x26A1;</span>R&aacute;pido
  </div>
  <div class="layer-pill locked" id="layer-noisy" data-layer="noisy" onclick="selectLayer('noisy')">
    <span class="layer-icon">&#x1F3D9;</span>Barulho
  </div>
</div>
<div class="fragile-tags" id="fragile-tags"></div>

<div class="progress-dots">
  <div class="dots-row">
    <div class="dot" id="dot-1"></div>
    <div class="dot" id="dot-2"></div>
    <div class="dot" id="dot-3"></div>
    <div class="dot" id="dot-4"></div>
    <div class="dot" id="dot-5"></div>
  </div>
  <div class="pass-label" id="pass-label"></div>
</div>

<div class="main">
  <img class="drill-image" id="drill-image" alt="">
  <div class="carrier-text hidden" id="carrier-text"></div>
  <div style="margin:4px 0">
    <button class="text-toggle-btn" id="textToggleBtn" onclick="toggleTextPanel()" style="background:none;border:1px solid rgba(255,255,255,0.12);border-radius:8px;color:rgba(255,255,255,0.5);font-size:12px;padding:5px 12px;cursor:pointer">Mostrar texto</button>
  </div>
  <div class="simple-panel hidden" id="simplePanel" style="padding:10px 16px;margin:0 20px;border-radius:12px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);text-align:center;display:none">
    <div id="simpleSentence" style="font-size:0.95em;line-height:1.5;color:rgba(255,255,255,0.85)"></div>
    <div id="simpleDefinition" style="color:#9ca3af;font-size:0.82em;margin-top:4px"></div>
  </div>
  <div class="pass-instruction" id="pass-instruction"></div>
  <div class="rep-counter" id="rep-counter"></div>
  <div class="rating-feedback" id="rating-feedback"></div>
  <div class="countdown-bar" id="countdown-bar"><div class="countdown-fill" id="countdown-fill"></div></div>
  <div class="countdown-label" id="countdown-label"></div>
  <div class="biometric-score" id="biometric-score"></div>
  <div class="prosody-pills" id="prosody-pills"></div>
  <div class="latency-trend" id="latency-trend">
    <span class="trend-arrow" id="trend-arrow"></span>
    <span class="trend-ms" id="trend-ms"></span>
  </div>

  <div class="action-area" id="action-area"></div>

  <audio id="player" preload="auto"></audio>
</div>

<div class="footer">
  <span class="tier-badge" id="tier-label"></span>
  <span id="due-label"></span>
  <span id="time-label"></span>
</div>

{tab_bar}

<script>
const $ = id => document.getElementById(id);
const player = $('player');

const PASS_NAMES = ['', 'Ouvindo', 'Murmurando', 'Lendo', 'Sombreando', 'Maestria'];
const PASS_INSTRUCTIONS = [
  '',
  'Escuta e entende o significado',
  'Murmura junto, acompanha o ritmo',
  'L\u00EA em voz alta com o \u00E1udio',
  'Sombrea \u2014 repete junto, igualzinho',
  'Sombrea sem texto \u2014 3 repeti\u00E7\u00F5es',
];

let currentChunk = null;
let currentPass = 1;
let masteryReps = 0;
let sessionCount = 0;
let sessionCorrect = 0;
let retries = 0;
let drillStartTime = null;
let againStreak = {};  // track consecutive Again ratings per chunk for 3-failure text reveal

// ── Text toggle for translation panel ──
let textPanelLoaded = false;
function toggleTextPanel() {
  const panel = $('simplePanel');
  if (!panel) return;
  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'block';
  $('textToggleBtn').textContent = visible ? 'Mostrar texto' : 'Esconder texto';
  // Populate on first show
  if (!visible && !textPanelLoaded && currentChunk) {
    textPanelLoaded = true;
    const sentence = currentChunk.carrier_sentence || '';
    const word = currentChunk.target_chunk || currentChunk.word || '';
    if (sentence && word) {
      const re = new RegExp('(' + word.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
      $('simpleSentence').innerHTML = sentence.replace(re, '<span style="background:#5E6AD2;color:#fff;padding:2px 6px;border-radius:4px">$1</span>');
    }
    // Fetch simple definition
    if (word) {
      fetch('/api/dict/lookup?word=' + encodeURIComponent(word))
        .then(r => r.json())
        .then(d => { if (d.definition) $('simpleDefinition').textContent = d.definition; })
        .catch(() => {});
    }
  }
}

// ── New feature state ────────────────────────────────────
let previousState = null;         // track state before drill for transition detection
let fatigueCheckCounter = 0;      // count drills for fatigue check every 5th
let fatigueTimerInterval = null;  // fatigue break countdown

const STATE_ORDER = ['UNKNOWN','RECOGNIZED','CONTEXT_KNOWN','EFFORTFUL_AUDIO','AUTOMATIC_CLEAN','AUTOMATIC_NATIVE','AVAILABLE_OUTPUT'];
const STATE_LABELS_PT = {
  UNKNOWN: 'Desconhecido', RECOGNIZED: 'Reconhecido', CONTEXT_KNOWN: 'Contexto',
  EFFORTFUL_AUDIO: 'Esfor\u00E7o Auditivo', AUTOMATIC_CLEAN: 'Autom\u00E1tico Limpo',
  AUTOMATIC_NATIVE: 'Autom\u00E1tico Nativo', AVAILABLE_OUTPUT: 'Pronto pra Falar',
};
const FRAGILE_LABELS = {
  known_but_slow: '\u26A1 Lento', familiar_but_fragile: '\u26A0\uFE0F Fr\u00E1gil',
  text_only: '\uD83D\uDC41\uFE0F S\u00F3 Texto', clean_audio_only: '\uD83D\uDD0A S\u00F3 Limpo',
  blocked_by_prosody: '\uD83D\uDDE3\uFE0F Pros\u00F3dia',
};
const FRAGILE_CLASSES = {
  known_but_slow: 'known_but_slow', familiar_but_fragile: 'familiar_but_fragile',
  text_only: 'text_only', clean_audio_only: 'clean_audio_only',
  blocked_by_prosody: 'blocked_by_prosody',
};
const PROSODY_TIPS = {
  isochrony: 'Ritmo: mant\u00E9m as s\u00EDlabas no mesmo tempo',
  pitch_contour: 'Entona\u00E7\u00E3o: segue a melodia baiana',
  nasalization: 'Nasaliza\u00E7\u00E3o: exagera o \u00E3o/\u00E3',
  vowel_length: 'Vogais: alonga as t\u00F4nicas',
  speech_rate: 'Velocidade: mais perto do nativo',
  syllable_reduction: 'Redu\u00E7\u00E3o: encurta as \u00E1tonas',
};

// ── Mode awareness ────────────────────────────────────────
let modeConfig = null;       // current drill config from /api/modes/config
let currentBlockId = null;   // block_id from daily plan
let blockItemsDone = 0;      // items completed in current block
let blockItemsTotal = 0;     // total items in current block
let countdownTimer = null;   // interval ID for countdown

// ── Fetch current block info ──────────────────────────────
async function fetchBlockInfo() {
  try {
    const res = await fetch('/api/plan/next-block');
    const block = await res.json();
    if (block && !block.done) {
      currentBlockId = block.block_id;
      blockItemsTotal = (block.target_items || []).length;
      blockItemsDone = 0;
      // Fetch mode config for this block's mode
      if (block.mode && block.mode !== 'break') {
        try {
          const mRes = await fetch('/api/modes/config?mode=' + encodeURIComponent(block.mode));
          modeConfig = await mRes.json();
        } catch (e) { modeConfig = null; }
      }
    }
  } catch (e) {
    // Non-fatal — drill still works without block info
  }
}

// ── Prefetch queue for fast chunk transitions ──────────────
let _prefetchQueue = [];
let _prefetching = false;

function prefetchBatch() {
  if (_prefetching) return;
  _prefetching = true;
  // Warm up next 5 chunks (audio + image gen in background)
  fetch('/api/drill/prefetch?limit=5').catch(()=>{}).finally(()=>{});
  // Also prefetch the actual next drill item
  fetch('/api/drill/next')
    .then(r => r.json())
    .then(data => { if (data && !data.error) _prefetchQueue.push(data); })
    .catch(() => {})
    .finally(() => { _prefetching = false; });
}

// ── Fetch next chunk ──────────────────────────────────────
async function fetchNext() {
  // Use prefetched data if available (instant transition)
  if (_prefetchQueue.length > 0) {
    const data = _prefetchQueue.shift();
    applyChunkData(data);
    return;
  }
  showLoading();
  try {
    const res = await fetch('/api/drill/next');
    const data = await res.json();
    if (data.error) {
      $('pass-label').textContent = '';
      $('pass-instruction').textContent = 'Todas revisadas. Descansa, parceiro.';
      $('action-area').innerHTML = '';
      $('mode-banner').classList.add('hidden');
      return;
    }
    applyChunkData(data);
  } catch (e) {
    $('pass-instruction').textContent = 'Erro: ' + e.message;
    setTimeout(fetchNext, 3000);
  }
}

function applyChunkData(data) {
    // Stop any playing audio before loading new chunk
    player.pause(); player.onended = null; player.onerror = null;
    currentChunk = data;
    currentPass = data.current_pass || 1;
    masteryReps = 0;
    retries = 0;
    drillStartTime = null;
    textPanelLoaded = false;
    const tp = $('simplePanel'); if (tp) tp.style.display = 'none';
    const tb = $('textToggleBtn'); if (tb) tb.textContent = 'Mostrar texto';
    previousState = data.current_state || null;

    if (data.mode_config) {
      modeConfig = data.mode_config;
    }
    applyModeUI();
    showFragileTags(data.fragility_types || []);
    $('tier-label').textContent = data.tier_label || ('Tier ' + data.tier);
    $('due-label').textContent = (data.due_count || 0) + ' pendentes';

    const img = $('drill-image');
    const showImage = !modeConfig || modeConfig.show_image !== false;
    if (data.image_file && showImage) {
      img.src = '/image/' + data.image_file;
      img.classList.add('visible');
    } else {
      img.classList.remove('visible');
    }
    enterPass(currentPass);
    // Prefetch next batch while user works on this chunk
    setTimeout(prefetchBatch, 500);
}

// ── Mode UI Helpers ───────────────────────────────────────
function applyModeUI() {
  const banner = $('mode-banner');
  if (modeConfig && modeConfig.label) {
    banner.textContent = modeConfig.label;
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }
  // Reset countdown and biometric
  stopCountdown();
  $('biometric-score').classList.remove('visible');
}

function startCountdown(maxMs) {
  stopCountdown();
  const bar = $('countdown-bar');
  const fill = $('countdown-fill');
  const label = $('countdown-label');
  bar.classList.add('visible');
  label.classList.add('visible');
  fill.style.width = '100%';

  const startTime = performance.now();
  countdownTimer = setInterval(() => {
    const elapsed = performance.now() - startTime;
    const remaining = Math.max(0, maxMs - elapsed);
    const pct = (remaining / maxMs) * 100;
    fill.style.width = pct + '%';
    label.textContent = (remaining / 1000).toFixed(1) + 's';

    if (pct <= 25) {
      fill.style.background = 'linear-gradient(90deg, #ef4444, #f59e0b)';
    } else if (pct <= 50) {
      fill.style.background = 'linear-gradient(90deg, #f59e0b, #5E6AD2)';
    } else {
      fill.style.background = 'linear-gradient(90deg, #5E6AD2, #3B82F6)';
    }

    if (remaining <= 0) {
      stopCountdown();
    }
  }, 100);
}

function stopCountdown() {
  if (countdownTimer) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
  $('countdown-bar').classList.remove('visible');
  $('countdown-label').classList.remove('visible');
  $('countdown-fill').style.width = '100%';
  $('countdown-fill').style.background = 'linear-gradient(90deg, #5E6AD2, #3B82F6)';
}

function showBiometricScore(score) {
  const el = $('biometric-score');
  if (score != null) {
    el.textContent = 'Biometria: ' + score + '/100';
    el.style.color = score >= 85 ? '#34d399' : score >= 60 ? '#fbbf24' : '#ef4444';
    el.classList.add('visible');
  } else {
    el.classList.remove('visible');
  }
}

// ── Pass State Machine ────────────────────────────────────
function enterPass(passNum) {
  currentPass = passNum;
  updateProgressDots();
  $('pass-label').textContent = PASS_NAMES[passNum];
  $('pass-instruction').textContent = PASS_INSTRUCTIONS[passNum];
  $('rating-feedback').classList.remove('visible');
  $('rep-counter').classList.remove('visible');

  // Carrier text: zero-reading rule — only after 3 consecutive Again ratings
  // OR if mode config explicitly says show_text
  const ct = $('carrier-text');
  const modeShowText = modeConfig && modeConfig.show_text === true;
  const chunkKey = currentChunk ? (currentChunk.chunk_id || currentChunk.word_id) : null;
  const failStreak = chunkKey ? (againStreak[chunkKey] || 0) : 0;
  const textRevealed = failStreak >= 3 || modeShowText;
  if (textRevealed) {
    ct.classList.remove('hidden');
    const sentence = currentChunk.carrier_sentence || '';
    const word = currentChunk.target_chunk || currentChunk.word || '';
    if (word && sentence.toLowerCase().includes(word.toLowerCase())) {
      const re = new RegExp('(' + word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      ct.innerHTML = sentence.replace(re, '<span class="highlight">$1</span>');
    } else {
      ct.textContent = sentence;
    }
  } else {
    ct.classList.add('hidden');
    ct.innerHTML = '';
  }

  // Image: hide if mode says no image
  const img = $('drill-image');
  if (modeConfig && modeConfig.show_image === false) {
    img.classList.remove('visible');
  }

  // Countdown timer: start if mode has max_response_time_ms
  stopCountdown();
  if (modeConfig && modeConfig.max_response_time_ms && passNum >= 2 && passNum <= 4) {
    startCountdown(modeConfig.max_response_time_ms);
  }

  // Buttons per pass
  const area = $('action-area');
  area.innerHTML = '';

  if (passNum === 1) {
    const btnAgain = document.createElement('button');
    btnAgain.className = 'btn-secondary';
    btnAgain.textContent = 'De novo';
    btnAgain.onclick = () => { retries++; playAudio(); };

    const btnOk = document.createElement('button');
    btnOk.className = 'btn-primary';
    btnOk.textContent = 'Entendi';
    btnOk.onclick = advancePass;

    area.appendChild(btnAgain);
    area.appendChild(btnOk);
  } else if (passNum >= 2 && passNum <= 4) {
    const btn = document.createElement('button');
    btn.className = 'btn-primary';
    btn.textContent = 'Pronto';
    btn.onclick = advancePass;
    area.appendChild(btn);
  }
  // Pass 5: no buttons

  if (passNum <= 4) {
    playAudio();
  } else {
    startMasteryLoop();
  }
}

function advancePass() {
  if (currentPass < 5) {
    fetch('/api/drill/advance', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        chunk_id: currentChunk.chunk_id || currentChunk.word_id,
        current_pass: currentPass,
      }),
    }).catch(() => {});
    enterPass(currentPass + 1);
  }
}

// ── Mastery Loop (Pass 5) ─────────────────────────────────
function startMasteryLoop() {
  masteryReps = 0;
  $('action-area').innerHTML = '';
  playMasteryRep();
}

function playMasteryRep() {
  if (!currentChunk || !currentChunk.audio_file) {
    $('pass-instruction').textContent = 'Sem áudio disponível. Verifique a chave ElevenLabs.';
    completeDrill();
    return;
  }
  masteryReps++;
  const rc = $('rep-counter');
  rc.textContent = masteryReps + '/3';
  rc.classList.add('visible');
  rc.style.animation = 'none';
  void rc.offsetWidth;
  rc.style.animation = 'repPulse 0.6s ease-in-out';

  player.src = '/audio/' + currentChunk.audio_file;
  player.onended = () => {
    // Pause for user to shadow, then advance
    setTimeout(() => {
      if (masteryReps < 3) {
        playMasteryRep();
      } else {
        completeDrill();
      }
    }, 2500);
  };
  player.onerror = () => {
    if (masteryReps < 3) playMasteryRep();
    else completeDrill();
  };
  player.play().catch(() => {
    if (masteryReps < 3) setTimeout(playMasteryRep, 1000);
    else completeDrill();
  });
}

// ── Complete Drill ────────────────────────────────────────
async function completeDrill() {
  const latencyMs = drillStartTime ? Math.round(performance.now() - drillStartTime) : 0;
  $('rep-counter').classList.remove('visible');
  $('action-area').innerHTML = '';
  stopCountdown();

  try {
    const res = await fetch('/api/drill/complete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        chunk_id: currentChunk.chunk_id || currentChunk.word_id,
        latency_ms: latencyMs,
        retries: retries,
      }),
    });
    const data = await res.json();

    sessionCount++;
    if (data.rating >= 3) sessionCorrect++;
    updateSessionStats();

    // Track consecutive Again ratings for 3-failure text reveal (zero-reading rule)
    const chunkKey = currentChunk.chunk_id || currentChunk.word_id;
    if (data.rating === 1) { againStreak[chunkKey] = (againStreak[chunkKey] || 0) + 1; }
    else { againStreak[chunkKey] = 0; }

    const ratingNames = {1: 'De novo', 2: 'Dif\u00EDcil', 3: 'Bom', 4: 'F\u00E1cil'};
    const fb = $('rating-feedback');
    fb.textContent = ratingNames[data.rating] || 'Bom';
    fb.style.color = data.rating >= 3 ? '#34d399' : '#fbbf24';
    fb.classList.add('visible');

    // Show biometric score if mode measures biometric
    if (modeConfig && modeConfig.measures_biometric && data.biometric_score != null) {
      showBiometricScore(data.biometric_score);
    }

    // ── Feature 1: Latency Trend Indicator ──
    if (data.state_info) {
      showLatencyTrend(data.state_info.avg_latency_ms, data.state_info.latency_trend);
    }

    // ── Feature 2: State Transition Celebration ──
    if (data.state_info && previousState) {
      const oldIdx = STATE_ORDER.indexOf(previousState);
      const newIdx = STATE_ORDER.indexOf(data.state_info.state);
      if (newIdx > oldIdx && oldIdx >= 0) {
        showStateCelebration(data.state_info.state);
      }
    }

    // ── Feature 4: Prosody Dimension Feedback ──
    if (modeConfig && modeConfig.measures_biometric && data.biometric_score != null) {
      showProsodyFeedback(data);
    }

  } catch (e) {
    sessionCount++;
    updateSessionStats();
  }

  // Track block progress and complete block if all items done
  blockItemsDone++;
  if (currentBlockId && blockItemsTotal > 0 && blockItemsDone >= blockItemsTotal) {
    try {
      await fetch('/api/plan/block/complete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          block_id: currentBlockId,
          actual_data: {
            items_reviewed: blockItemsDone,
            accuracy: sessionCount > 0 ? Math.round(sessionCorrect / sessionCount * 100) : 0,
          },
        }),
      });
      // Fetch next block info for mode config
      await fetchBlockInfo();
    } catch (e) {}
  }

  // ── Feature 5: Fatigue-Reactive Flow (every 5th drill) ──
  fatigueCheckCounter++;
  if (fatigueCheckCounter % 5 === 0) {
    checkFatigueStatus();
  }

  setTimeout(fetchNext, 1800);
}

// ── Audio ─────────────────────────────────────────────────
function startDrillTimer() {
  // Called when audio finishes — latency measured from end of audio playback
  drillStartTime = performance.now();
}

function playAudio() {
  if (!currentChunk || !currentChunk.audio_file) {
    console.warn('[OXE] No audio_file — TTS may have failed');
    $('pass-instruction').textContent = 'Sem áudio — verifique a chave da ElevenLabs';
    // Start timer anyway so drill can proceed
    startDrillTimer();
    return;
  }
  const audioType = modeConfig ? modeConfig.audio_type : 'clean';

  if (audioType === 'both' && currentChunk.native_audio_file) {
    // Play clean first, then native on ended
    player.src = '/audio/' + currentChunk.audio_file;
    player.onended = () => {
      setTimeout(() => {
        player.src = '/audio/' + currentChunk.native_audio_file;
        player.onended = () => { startDrillTimer(); };
        player.onerror = () => { startDrillTimer(); };
        player.play().catch(() => { startDrillTimer(); });
      }, 600);
    };
    player.onerror = () => { startDrillTimer(); };
    player.play().catch(() => { startDrillTimer(); });
  } else if (audioType === 'native' && currentChunk.native_audio_file) {
    player.src = '/audio/' + currentChunk.native_audio_file;
    player.onended = () => { startDrillTimer(); };
    player.onerror = () => { startDrillTimer(); };
    player.play().catch(() => { startDrillTimer(); });
  } else {
    // Default: play clean TTS — timer starts when audio ends
    player.src = '/audio/' + currentChunk.audio_file;
    player.onended = () => { startDrillTimer(); };
    player.onerror = () => { startDrillTimer(); };
    player.play().catch(() => { startDrillTimer(); });
  }
}

// ── UI Helpers ────────────────────────────────────────────
function updateProgressDots() {
  for (let i = 1; i <= 5; i++) {
    const dot = $('dot-' + i);
    dot.className = 'dot';
    if (i < currentPass) dot.classList.add('completed');
    else if (i === currentPass) dot.classList.add('current');
  }
}

function updateSessionStats() {
  $('session-count').textContent = sessionCount;
  const pct = sessionCount > 0 ? Math.round(sessionCorrect / sessionCount * 100) : 0;
  $('session-accuracy').textContent = pct;
}

function showLoading() {
  $('drill-image').classList.remove('visible');
  $('carrier-text').classList.add('hidden');
  $('carrier-text').innerHTML = '';
  $('rep-counter').classList.remove('visible');
  $('rating-feedback').classList.remove('visible');
  $('pass-label').textContent = 'Carregando...';
  $('pass-instruction').textContent = '';
  $('action-area').innerHTML = '<button class="btn-primary" disabled>Carregando...</button>';
  for (let i = 1; i <= 5; i++) $('dot-' + i).className = 'dot';
  stopCountdown();
  $('biometric-score').classList.remove('visible');
  $('latency-trend').classList.remove('visible');
  $('latency-trend').classList.remove('fading');
  $('prosody-pills').classList.remove('visible');
  $('prosody-pills').classList.remove('fading');
  $('prosody-pills').innerHTML = '';
  $('fragile-tags').innerHTML = '';
}

// ── Feature 1: Latency Trend Indicator ───────────────────
function showLatencyTrend(avgMs, trend) {
  const el = $('latency-trend');
  const arrow = $('trend-arrow');
  const ms = $('trend-ms');
  if (avgMs == null) { el.classList.remove('visible'); return; }

  const rounded = Math.round(avgMs);
  ms.textContent = rounded + 'ms';

  if (trend < -0.1) {
    arrow.textContent = '\u2193';
    arrow.style.color = '#34d399';
    ms.style.color = '#34d399';
  } else if (trend > 0.1) {
    arrow.textContent = '\u2191';
    arrow.style.color = '#f87171';
    ms.style.color = '#f87171';
  } else {
    arrow.textContent = '\u2192';
    arrow.style.color = '#7a7a8e';
    ms.style.color = '#7a7a8e';
  }

  el.classList.remove('fading');
  el.classList.add('visible');
  setTimeout(() => { el.classList.add('fading'); }, 2000);
  setTimeout(() => { el.classList.remove('visible'); el.classList.remove('fading'); }, 2500);
}

// ── Feature 2: State Transition Celebration ──────────────
function showStateCelebration(newState) {
  const el = $('state-celebration');
  const label = STATE_LABELS_PT[newState] || newState;
  $('celeb-state').textContent = label + '! \uD83C\uDFAF';
  $('celeb-label').textContent = 'Subiu de n\u00EDvel de aquisi\u00E7\u00E3o';
  el.classList.remove('dismissing');
  el.classList.add('visible');
  setTimeout(() => { el.classList.add('dismissing'); }, 2000);
  setTimeout(() => { el.classList.remove('visible'); el.classList.remove('dismissing'); }, 2500);
}

// ── Feature 3: Fragile Item Tags ─────────────────────────
function showFragileTags(types) {
  const container = $('fragile-tags');
  container.innerHTML = '';
  if (!types || types.length === 0) return;
  types.forEach(t => {
    const tag = document.createElement('span');
    tag.className = 'fragile-tag ' + (FRAGILE_CLASSES[t] || '');
    tag.textContent = FRAGILE_LABELS[t] || t;
    container.appendChild(tag);
  });
}

// ── Feature 4: Prosody Dimension Feedback ────────────────
function showProsodyFeedback(data) {
  const container = $('prosody-pills');
  container.innerHTML = '';
  // Try to get prosody dimensions from the drill score endpoint
  // The biometric data may contain dimension scores
  try {
    fetch('/api/drill/score', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        chunk_id: currentChunk.chunk_id || currentChunk.word_id,
        audio_file: currentChunk.audio_file,
      }),
    }).then(r => r.json()).then(scoreData => {
      if (!scoreData || !scoreData.dimensions) return;
      const dims = scoreData.dimensions;
      // Find dimensions scoring below 60, take worst 3
      const weak = Object.entries(dims)
        .filter(([k, v]) => v < 60 && PROSODY_TIPS[k])
        .sort((a, b) => a[1] - b[1])
        .slice(0, 3);
      if (weak.length === 0) return;
      weak.forEach(([dim, score]) => {
        const pill = document.createElement('div');
        pill.className = 'prosody-pill';
        pill.innerHTML = '<span class="pill-dim">' + PROSODY_TIPS[dim] + '</span><span class="pill-score"> (' + Math.round(score) + '/100)</span>';
        container.appendChild(pill);
      });
      container.classList.add('visible');
      setTimeout(() => { container.classList.add('fading'); }, 4000);
      setTimeout(() => { container.classList.remove('visible'); container.classList.remove('fading'); container.innerHTML = ''; }, 4500);
    }).catch(() => {});
  } catch (e) {}
}

// ── Feature 5: Fatigue-Reactive Flow ─────────────────────
async function checkFatigueStatus() {
  try {
    const res = await fetch('/api/fatigue/status');
    const data = await res.json();
    const banner = $('fatigue-banner');
    const modal = $('fatigue-modal');

    // Clear previous fatigue UI
    banner.classList.remove('visible');
    banner.innerHTML = '';
    modal.classList.remove('visible');
    if (fatigueTimerInterval) { clearInterval(fatigueTimerInterval); fatigueTimerInterval = null; }

    if (data.recommendation === 'switch_mode') {
      banner.innerHTML = '\uD83D\uDCA4 Cansou? Troca pra escuta passiva ' +
        '<button class="fatigue-action" onclick="window.location.href=\'/library\'">Biblioteca</button>' +
        '<button class="fatigue-dismiss" onclick="this.parentElement.classList.remove(\'visible\')">\u2715</button>';
      banner.classList.add('visible');
    } else if (data.recommendation === 'take_break') {
      $('modal-icon').textContent = '\u2615';
      $('modal-text').textContent = 'Pausa de 5 minutos';
      $('modal-sub').textContent = 'Descansa os olhos e volta com tudo';
      $('modal-btn').style.display = 'none';
      modal.classList.add('visible');
      let remaining = 300;
      $('modal-timer').textContent = '5:00';
      fatigueTimerInterval = setInterval(() => {
        remaining--;
        if (remaining <= 0) {
          clearInterval(fatigueTimerInterval);
          fatigueTimerInterval = null;
          modal.classList.remove('visible');
          return;
        }
        const m = Math.floor(remaining / 60);
        const s = remaining % 60;
        $('modal-timer').textContent = m + ':' + (s < 10 ? '0' : '') + s;
      }, 1000);
    } else if (data.recommendation === 'end_session') {
      $('modal-icon').textContent = '\uD83C\uDF19';
      $('modal-text').textContent = 'Melhor parar por hoje';
      $('modal-sub').textContent = (data.minutes_active || 0) + ' minutos de treino';
      $('modal-timer').textContent = '';
      $('modal-btn').textContent = 'Voltar pro in\u00EDcio';
      $('modal-btn').style.display = 'block';
      $('modal-btn').onclick = () => { window.location.href = '/'; };
      modal.classList.add('visible');
    }
    // 'continue' => do nothing, UI already cleared
  } catch (e) {}
}

// ── Feature 6: Listening Difficulty Layers ────────────────
var LISTENING_MODES = ['audio_meaning_recognition', 'native_speed_parsing', 'clean_vs_native_comparison'];
var listeningLayerData = null;   // {chunk_id, layers: [...], current_layer}
var listeningLayerAudios = {};   // {layer: audio_file}
var activeListeningLayer = 'clean';

function isListeningMode() {
  if (!modeConfig) return false;
  return LISTENING_MODES.indexOf(modeConfig.mode) >= 0;
}

function showListeningLayers() {
  var container = $('listening-layers');
  if (!isListeningMode()) {
    container.classList.remove('visible');
    return;
  }
  container.classList.add('visible');
  updateLayerPills();
}

function hideListeningLayers() {
  $('listening-layers').classList.remove('visible');
  listeningLayerData = null;
  listeningLayerAudios = {};
  activeListeningLayer = 'clean';
}

function updateLayerPills() {
  var layers = ['clean', 'native_clear', 'native_fast', 'noisy'];
  var activeIdx = layers.indexOf(activeListeningLayer);

  for (var i = 0; i < layers.length; i++) {
    var pill = $('layer-' + layers[i]);
    if (!pill) continue;
    pill.className = 'layer-pill';
    if (i < activeIdx) {
      pill.classList.add('completed');
    } else if (i === activeIdx) {
      pill.classList.add('active');
    } else {
      pill.classList.add('locked');
    }
  }
}

function selectLayer(layer) {
  var layers = ['clean', 'native_clear', 'native_fast', 'noisy'];
  var activeIdx = layers.indexOf(activeListeningLayer);
  var targetIdx = layers.indexOf(layer);

  // Can only select current or completed layers
  if (targetIdx > activeIdx) return;

  activeListeningLayer = layer;
  updateLayerPills();
  playLayerAudio(layer);
}

function playLayerAudio(layer) {
  var audioFile = listeningLayerAudios[layer];
  if (!audioFile) return;
  player.src = '/audio/' + audioFile;
  player.onended = null;
  player.onerror = null;
  player.play().catch(function() {});
}

async function fetchListeningLayers(chunkId) {
  if (!isListeningMode() || !chunkId) return;
  try {
    var res = await fetch('/api/listening/drill/' + chunkId);
    var data = await res.json();
    if (data.error) return;
    listeningLayerData = data;
    activeListeningLayer = data.current_layer || 'clean';
    listeningLayerAudios = {};
    if (data.layers) {
      for (var i = 0; i < data.layers.length; i++) {
        var l = data.layers[i];
        if (l.audio_file) {
          listeningLayerAudios[l.layer] = l.audio_file;
        }
      }
    }
    showListeningLayers();
  } catch (e) {
    // Non-fatal — drill still works without layers
  }
}

async function advanceListeningLayer(success) {
  if (!isListeningMode() || !currentChunk) return;
  var chunkId = currentChunk.chunk_id || currentChunk.word_id;
  try {
    var res = await fetch('/api/listening/advance', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        chunk_id: chunkId,
        current_layer: activeListeningLayer,
        success: success,
      }),
    });
    var data = await res.json();
    if (data.advanced) {
      activeListeningLayer = data.current_layer;
      updateLayerPills();
      // Auto-play the new layer audio after a brief pause
      setTimeout(function() { playLayerAudio(activeListeningLayer); }, 500);
    } else if (data.completed) {
      // All layers mastered — mark all pills as completed
      var layers = ['clean', 'native_clear', 'native_fast', 'noisy'];
      for (var i = 0; i < layers.length; i++) {
        var pill = $('layer-' + layers[i]);
        if (pill) {
          pill.className = 'layer-pill completed';
        }
      }
    }
  } catch (e) {}
}

// Patch into fetchNext: after chunk loads, fetch listening layers
var _origFetchNext = fetchNext;
fetchNext = async function() {
  hideListeningLayers();
  await _origFetchNext();
  if (currentChunk && isListeningMode()) {
    var cid = currentChunk.chunk_id || currentChunk.word_id;
    fetchListeningLayers(cid);
  }
};

// Patch into completeDrill: auto-advance listening layer on success
var _origCompleteDrill = completeDrill;
completeDrill = async function() {
  var wasListeningMode = isListeningMode();
  await _origCompleteDrill();
  if (wasListeningMode) {
    // Assume success if rating >= 3 (Good or Easy)
    advanceListeningLayer(true);
  }
};

function updateTime() {
  $('time-label').textContent = new Date().toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});
}
setInterval(updateTime, 10000);
updateTime();

// ── Start ─────────────────────────────────────────────────
(async () => {
  await fetchBlockInfo();
  fetchNext();
})();
</script>

</body></html>"""


class DrillHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/drill":
            self._html(DRILL_HTML.replace("{tab_bar}", ""))

        elif path == "/api/next":
            self._next_word()

        elif path == "/api/drill/next":
            self._drill_next_chunk()

        elif path == "/api/plan/next-block":
            block = get_next_block()
            self._json(block if block else {"done": True})

        elif path == "/api/modes/config":
            mode = query.get("mode", ["audio_meaning_recognition"])[0]
            try:
                self._json(get_drill_config(mode))
            except KeyError:
                self._json(get_drill_config("audio_meaning_recognition"))

        elif path.startswith("/audio/"):
            self._serve_audio(path[7:])

        elif path.startswith("/image/"):
            self._serve_image(path[7:])

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/respond":
            self._handle_respond(body)

        elif path == "/api/explain":
            self._handle_explain(body)

        elif path == "/api/trap-respond":
            self._handle_trap_respond(body)

        elif path == "/api/drill/advance":
            self._drill_advance_pass(body)

        elif path == "/api/drill/complete":
            self._drill_complete(body)

        elif path == "/api/plan/block/complete":
            block_id = body.get("block_id", 0)
            actual = body.get("actual_data", {})
            try:
                record_block_completion(block_id, actual)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        else:
            self.send_error(404)

    # ── API handlers ──────────────────────────────────────────

    def _next_word(self):
        global _laranjada_remaining

        # Trap chance
        if random.random() < TRAP_PROBABILITY:
            trap = random.choice(TRAP_SENTENCES)
            sentence, trap_type, expected = trap

            fname = generate_tts(sentence)
            if not fname:
                self._json({"error": "Falha ao gerar áudio"})
                return

            tier = get_unlocked_tier()
            due = get_due_words()
            due_count = len(list(due))

            self._json({
                "type": "trap",
                "trap_sentence": sentence,
                "trap_type": trap_type,
                "expected": expected,
                "audio_file": fname,
                "tier": tier,
                "tier_label": TIER_LABELS[tier],
                "due_count": due_count,
                "mastery": "-",
            })
            return

        word = get_next_word()
        if not word:
            self._json({"error": "nenhuma_palavra_pendente"})
            return

        carrier = build_carrier(word["word"])
        fname = generate_tts(carrier)
        if not fname:
            self._json({"error": "Falha ao gerar áudio"})
            return

        img_fname = generate_image(word["word"])

        # Pre-fetch next 5 due words' images in background
        due_words = list(get_due_words())
        upcoming = [w["word"] for w in due_words[:5] if w["word"] != word["word"]]
        if upcoming:
            prefetch_images(upcoming)

        tier = get_unlocked_tier()

        self._json({
            "type": "drill",
            "word_id": word["id"],
            "word": word["word"],
            "carrier": carrier,
            "audio_file": fname,
            "image_file": img_fname,
            "tier": word["difficulty_tier"],
            "tier_label": TIER_LABELS[word["difficulty_tier"]],
            "mastery": word["mastery_level"],
            "due_count": due_count,
        })

    def _handle_respond(self, body):
        global _laranjada_remaining

        word_id = body["word_id"]
        latency_ms = body["latency_ms"]

        # Determine rating
        if latency_ms <= 600:
            rating = Rating.Easy
        elif latency_ms <= LATENCY_THRESHOLD_MS:
            rating = Rating.Good
        elif latency_ms <= 2000:
            rating = Rating.Hard
        else:
            rating = Rating.Again

        # Laranjada penalty override
        penalty_active = False
        if _laranjada_remaining > 0:
            _laranjada_remaining -= 1
            if rating.value > Rating.Hard.value:
                rating = Rating.Hard
            penalty_active = True

        card, new_mastery, downgraded = record_review(word_id, rating, latency_ms)
        rating_name = {1: "De novo", 2: "Difícil", 3: "Bom", 4: "Fácil"}[rating.value]

        # Log
        log_drill(word_id, str(word_id), rating.value, latency_ms)

        # Tier progress
        tier = get_unlocked_tier()
        progress = tier_progress()
        current_pct = 0
        for t, label, mastered, total, pct in progress:
            if t == tier:
                current_pct = pct
                break

        self._json({
            "rating": rating.value,
            "rating_name": rating_name,
            "new_mastery": new_mastery,
            "penalty_active": penalty_active,
            "tier_progress": round(current_pct, 1),
        })

    def _handle_explain(self, body):
        word = body.get("word", "")
        explanation, audio_fname = generate_explanation(word)
        self._json({
            "explanation": explanation,
            "audio_file": audio_fname,
        })

    def _handle_trap_respond(self, body):
        global _laranjada_remaining

        reaction = body.get("reaction", "").lower().strip()
        latency_ms = body.get("latency_ms", 9999)
        sentence = body.get("sentence", "")

        passed = False
        for valid in TRAP_REACTIONS:
            if valid in reaction:
                passed = True
                break
        if latency_ms > TRAP_LATENCY_MS:
            passed = False

        if not passed:
            _laranjada_remaining = 5

        # Find expected response
        expected = ""
        for s, t, e in TRAP_SENTENCES:
            if s == sentence:
                expected = e
                break

        # Log
        log_file = LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "trap",
            "sentence": sentence,
            "reaction": reaction,
            "latency_ms": latency_ms,
            "passed": passed,
        }
        with open(log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._json({
            "passed": passed,
            "expected": expected,
            "penalty_remaining": _laranjada_remaining,
        })

    def _drill_next_chunk(self):
        """Serve the next chunk for 5-pass drilling, with mode config."""
        chunk = get_next_chunk()
        if chunk is None:
            # Cold start: seed from word_bank Tier 1
            try:
                from srs_engine import get_connection, DB_PATH as _DB
                conn = get_connection(_DB)
                words = conn.execute(
                    "SELECT id, word FROM word_bank WHERE difficulty_tier = 1 "
                    "ORDER BY frequency_rank LIMIT 10"
                ).fetchall()
                conn.close()
                for w in words:
                    carrier = build_carrier(w["word"])
                    add_chunk(w["id"], w["word"], carrier, "corpus")
                chunk = get_next_chunk()
            except Exception:
                pass

        if chunk is None:
            self._json({"error": "Nenhum chunk disponivel"})
            return

        audio_file = generate_tts(chunk["carrier_sentence"])
        image_file = None
        try:
            image_file = generate_image(chunk["word"], chunk.get("carrier_sentence"))
        except Exception:
            pass

        # Get mode config for this block/item
        mode_key = get_mode_for_next_item()
        try:
            mode_config = get_drill_config(mode_key)
        except Exception:
            mode_config = get_drill_config("audio_meaning_recognition")

        due_count = len(get_due_chunks())
        tier = get_unlocked_tier()

        self._json({
            "chunk_id": chunk["id"],
            "word": chunk["word"],
            "word_id": chunk.get("word_id"),
            "target_chunk": chunk["target_chunk"],
            "carrier_sentence": chunk["carrier_sentence"],
            "current_pass": chunk["current_pass"],
            "audio_file": audio_file,
            "image_file": image_file,
            "tier": chunk.get("difficulty_tier", 1),
            "tier_label": TIER_LABELS.get(chunk.get("difficulty_tier", 1), "Tier 1"),
            "due_count": due_count,
            "mode_config": mode_config,
        })

    def _drill_advance_pass(self, body):
        chunk_id = body.get("chunk_id")
        current_pass = body.get("current_pass", 1)
        if not chunk_id:
            self._json({"error": "chunk_id obrigatorio"})
            return
        new_pass = min(current_pass + 1, 5)
        update_chunk_pass(chunk_id, new_pass)
        self._json({"chunk_id": chunk_id, "new_pass": new_pass})

    def _drill_complete(self, body):
        chunk_id = body.get("chunk_id")
        latency_ms = body.get("latency_ms")
        retries = body.get("retries", 0)
        biometric = body.get("biometric_score")

        if not chunk_id:
            self._json({"error": "chunk_id obrigatorio"})
            return

        if retries >= 3:
            rating = Rating.Again
        elif latency_ms and latency_ms > LATENCY_THRESHOLD_MS:
            rating = Rating.Hard
        elif biometric and biometric < 85:
            rating = Rating.Hard
        elif latency_ms and latency_ms <= 600 and retries == 0:
            rating = Rating.Easy
        else:
            rating = Rating.Good

        new_card, mastery, downgraded = record_chunk_review(
            chunk_id, rating, latency_ms, biometric
        )

        # Update acquisition state
        try:
            audio_type = body.get("audio_type", "clean")
            state_result = update_state_after_review(
                'chunk', chunk_id, rating, latency_ms, audio_type, biometric
            )
        except Exception as e:
            state_result = {}
            print(f"[Drill] Acquisition update warning: {e}")

        # Record fatigue event
        try:
            record_review_event(latency_ms or 0, rating.value, retries)
        except Exception as e:
            print(f"[Drill] Fatigue recording warning: {e}")

        # Check replay reinforcement (3+ retries = fragile flag)
        try:
            if retries >= 3:
                check_replay_reinforcement('chunk', chunk_id, retries)
        except Exception as e:
            print(f"[Drill] Replay reinforcement warning: {e}")

        log_drill(chunk_id, str(chunk_id), rating.value, latency_ms, "drill_5pass")

        self._json({
            "rating": rating.value,
            "rating_name": {1: "De novo", 2: "Dificil", 3: "Bom", 4: "Facil"}.get(rating.value, "Bom"),
            "new_mastery": mastery,
            "acquisition_state": state_result.get("new_state", ""),
            "promoted": state_result.get("promoted", False),
            "demoted": state_result.get("demoted", False),
        })

    def _serve_audio(self, filename):
        filepath = AUDIO_DIR / filename
        if not filepath.exists():
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(filepath.stat().st_size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    def _serve_image(self, filename):
        filepath = IMAGE_DIR / filename
        if not filepath.exists():
            self.send_error(404)
            return
        ct = "image/png" if filename.endswith(".png") else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(filepath.stat().st_size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    # ── Helpers ───────────────────────────────────────────────

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def log_message(self, format, *args):
        # Quiet unless error
        if args and str(args[0]).startswith(("4", "5")):
            super().log_message(format, *args)


def main():
    port = 7777
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    ip = get_local_ip()
    server = http.server.HTTPServer(("0.0.0.0", port), DrillHandler)

    tier = get_unlocked_tier()
    due = get_due_words()
    due_count = len(list(due))

    print(f"\n  Oxe Protocol — Drill Server")
    print(f"  {'='*44}")
    print(f"  Phone:   http://{ip}:{port}")
    print(f"  Local:   http://localhost:{port}")
    print(f"  Tier:    {tier} ({TIER_LABELS[tier]})")
    print(f"  Due:     {due_count} words")
    print(f"  {'='*44}")
    print(f"  Open the Phone URL on your mobile.")
    print(f"  Audio auto-plays. Tap after shadowing.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
