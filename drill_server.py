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
)

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
    "Ave Maria,", "Ô xente,",
]
TAGS = [
    "viu!", "visse.", "tá ligado?", "rapaz.", "né?",
    "é mermo.", "sabe como é.", "acredita?",
]
LOCATIONS = [
    "no Pelourinho", "lá no Rio Vermelho", "na Barra", "em Itapuã",
    "no Candeal", "no Comércio", "na Ribeira", "na Pituba",
]
CARRIERS = [
    "{intj} tu sabe o que é {word}? {tag}",
    "{intj} ontem eu vi um negócio de {word} {loc}, {tag}",
    "Eu tava pensando em {word} agora mesmo, {tag}",
    "{intj} {word} é uma coisa que todo baiano conhece, {tag}",
    "Tu já ouviu falar de {word}? {tag}",
    "A gente sempre fala de {word} {loc}, {tag}",
    "{intj} sem {word} não dá pra viver, {tag}",
    "{intj} {word} é barril demais, {tag}",
]

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
    return t.format(
        intj=random.choice(INTERJECTIONS),
        word=word,
        tag=random.choice(TAGS),
        loc=random.choice(LOCATIONS),
    )


def generate_tts(text):
    """Generate TTS, return filename."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None

    from elevenlabs import ElevenLabs
    client = ElevenLabs(api_key=api_key)

    audio_iter = client.text_to_speech.convert(
        text=text,
        voice_id="pNInz6obpgDQGcFmaJgB",
        model_id="eleven_multilingual_v2",
        output_format="mp3_44100_128",
        voice_settings={
            "stability": 0.55,
            "similarity_boost": 0.90,
            "style": 0.45,
            "use_speaker_boost": True,
        },
    )

    fname = f"tts_{int(time.time() * 1000)}.mp3"
    outpath = AUDIO_DIR / fname
    with open(outpath, "wb") as f:
        for chunk in audio_iter:
            f.write(chunk)
    return fname


import threading

_image_lock = threading.Lock()
_image_pending = set()


def get_cached_image(word):
    """Return cached image filename if it exists, else None."""
    safe = "".join(c if c.isalnum() else "_" for c in word)
    fname = f"img_{safe}.png"
    cached = IMAGE_DIR / fname
    if cached.exists() and cached.stat().st_size > 0:
        return fname
    return None


def _bg_generate_image(word):
    """Background thread: generate DALL-E image and cache it."""
    safe = "".join(c if c.isalnum() else "_" for c in word)
    fname = f"img_{safe}.png"
    cached = IMAGE_DIR / fname
    try:
        import openai
        client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
        prompt = (
            f"A vivid, colorful illustration representing the concept of '{word}' "
            f"in the context of Salvador, Bahia, Brazil. "
            f"Include Bahian cultural elements — colorful colonial buildings, palm trees, "
            f"or street scenes from Pelourinho. No text, no words, no letters in the image. "
            f"Warm tropical colors, clean modern illustration style."
        )
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


def generate_image(word):
    """Return cached image, generating synchronously if needed (user wants to see it)."""
    cached = get_cached_image(word)
    if cached:
        return cached
    # Generate now — user wants the image before drilling
    _bg_generate_image(word)
    return get_cached_image(word)


def prefetch_images(words):
    """Pre-generate images for a list of words in background threads."""
    for w in words:
        if get_cached_image(w):
            continue
        with _image_lock:
            if w not in _image_pending:
                _image_pending.add(w)
                t = threading.Thread(target=_bg_generate_image, args=(w,), daemon=True)
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
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    display: flex; flex-direction: column; min-height: 100vh; min-height: 100dvh;
    overflow: hidden; -webkit-user-select: none; user-select: none;
  }
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
  .main {
    flex: 1; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 20px; gap: 24px;
    animation: fadeUp 0.5s ease-out;
  }
  .word-display {
    font-size: 2.4em; font-weight: 700; min-height: 1.2em; text-align: center;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .carrier { font-size: 1em; color: #525263; text-align: center; min-height: 1.5em; }
  .tier-badge {
    font-size: 0.75em; padding: 5px 16px; border-radius: 999px;
    background: rgba(59,130,246,0.08); color: #60a5fa; display: inline-block;
    border: 1px solid transparent;
    background-clip: padding-box;
    position: relative;
  }
  .tier-badge::before {
    content: ''; position: absolute; inset: 0; border-radius: 999px; padding: 1px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude;
    pointer-events: none;
  }
  .latency {
    font-size: 3em; font-weight: 700; min-height: 1.2em;
    transition: color 0.3s; font-variant-numeric: tabular-nums;
  }
  .latency.fast { color: #34d399; }
  .latency.ok { color: #fbbf24; }
  .latency.slow { color: #f87171; }
  .rating-label { font-size: 1em; min-height: 1.2em; color: #9ca3af; }
  .drill-image {
    width: 160px; height: 160px; border-radius: 20px; object-fit: cover;
    border: 1px solid rgba(255,255,255,0.08); display: none;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3), 0 0 40px rgba(59,130,246,0.08);
  }
  .drill-image.visible { display: block; animation: fadeUp 0.4s ease-out; }
  #tap-zone {
    width: 100%; max-width: 340px; padding: 22px 80px; font-size: 1.3em; font-weight: 700;
    border: none; border-radius: 20px; cursor: pointer;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    transition: all 0.2s; -webkit-tap-highlight-color: transparent;
    box-shadow: 0 0 20px rgba(59,130,246,0.25), 0 4px 12px rgba(0,0,0,0.3);
    letter-spacing: 0.5px;
  }
  #tap-zone:active { transform: scale(0.97); opacity: 0.9; }
  #tap-zone:disabled { background: rgba(255,255,255,0.04); color: #444; box-shadow: none; }
  .trap-zone {
    width: 100%; display: none; gap: 8px;
  }
  .trap-btn {
    flex: 1; padding: 16px 8px; font-size: 1em; font-weight: 600;
    border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;
    background: rgba(255,255,255,0.04); color: #fafafa; cursor: pointer;
    -webkit-tap-highlight-color: transparent; transition: all 0.15s;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  }
  .trap-btn:active { background: #3B82F6; color: #fff; border-color: #3B82F6; }
  .penalty { color: #f87171; font-size: 0.85em; min-height: 1.2em; }
  .explanation {
    font-size: 0.9em; color: #9ca3af; text-align: left; line-height: 1.6;
    padding: 14px 18px 14px 20px; background: rgba(59,130,246,0.04);
    border: 1px solid rgba(59,130,246,0.1); border-left: 3px solid #3B82F6;
    border-radius: 14px; display: none; width: 100%; max-width: 340px;
    animation: fadeUp 0.4s ease-out;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
  }
  .explanation.visible { display: block; }
  .progress-bar {
    width: 100%; height: 3px; background: rgba(255,255,255,0.04); border-radius: 2px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%; background: linear-gradient(90deg, #3B82F6, #7C5CFC);
    transition: width 0.5s;
  }
  .footer {
    padding: 14px 24px 80px; background: rgba(255,255,255,0.02);
    border-top: 1px solid rgba(255,255,255,0.06);
    display: flex; justify-content: space-between; align-items: center;
    font-size: 0.75em; color: #6b7280; letter-spacing: 0.3px;
  }
  .loading { color: #525263; font-size: 1.2em; }
  .pulsing { animation: pulse 1.5s infinite; }

  /* Mic button */
  @keyframes micPulse { 0%,100%{box-shadow:0 0 0 0 rgba(248,113,113,0.4)} 50%{box-shadow:0 0 0 12px rgba(248,113,113,0)} }
  .mic-btn {
    width: 72px; height: 72px; border-radius: 50%; border: 2px solid rgba(255,255,255,0.1);
    background: rgba(255,255,255,0.04); color: #60a5fa; font-size: 1.8em;
    cursor: pointer; display: none; align-items: center; justify-content: center;
    transition: all 0.2s; backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    -webkit-tap-highlight-color: transparent;
  }
  .mic-btn.visible { display: flex; animation: fadeUp 0.3s ease-out; }
  .mic-btn.recording { border-color: #f87171; color: #f87171; animation: micPulse 1.2s infinite; background: rgba(248,113,113,0.08); }
  .mic-btn:active { transform: scale(0.95); }
  .mic-score { font-size: 2em; font-weight: 700; min-height: 1.2em; text-align: center; }
  .mic-score.pass { color: #60a5fa; }
  .mic-score.fail { color: #f87171; }
  .mic-msg { font-size: 0.9em; color: #525263; text-align: center; min-height: 1.2em; }
  .mic-denied { font-size: 0.75em; color: #525263; text-align: center; display: none; }

  /* Shadow button */
  .shadow-btn {
    padding: 12px 24px; border: 1px solid rgba(124,92,252,0.3); border-radius: 14px;
    background: rgba(124,92,252,0.08); color: #a78bfa; font-size: 0.9em; font-weight: 600;
    cursor: pointer; display: none; transition: all 0.2s;
    -webkit-tap-highlight-color: transparent;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  }
  .shadow-btn.visible { display: inline-block; animation: fadeUp 0.3s ease-out; }
  .shadow-btn:active { transform: scale(0.97); background: rgba(124,92,252,0.15); }

  /* Cloze UI */
  .cloze-input-wrap {
    width: 100%; max-width: 300px; display: none; flex-direction: column; align-items: center; gap: 12px;
  }
  .cloze-input-wrap.visible { display: flex; animation: fadeUp 0.4s ease-out; }
  .cloze-input {
    width: 100%; padding: 16px 20px; font-size: 1.3em; font-weight: 600; text-align: center;
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px; color: #fafafa; outline: none; font-family: inherit;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    -webkit-appearance: none;
  }
  .cloze-input:focus { border-color: rgba(59,130,246,0.5); }
  .cloze-input::placeholder { color: #333; }
  .cloze-submit {
    padding: 12px 32px; border: none; border-radius: 12px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    font-size: 1em; font-weight: 600; cursor: pointer; transition: all 0.2s;
    box-shadow: 0 0 12px rgba(59,130,246,0.2);
  }
  .cloze-submit:active { transform: scale(0.97); opacity: 0.9; }
  .cloze-result { font-size: 1.4em; font-weight: 700; text-align: center; min-height: 1.5em; }
  .cloze-result.correct { color: #60a5fa; }
  .cloze-result.wrong { color: #f87171; }
  .cloze-answer { font-size: 0.9em; color: #525263; text-align: center; min-height: 1.2em; }
</style>
</head><body>

<div class="header">
  <h1>OXE PROTOCOL</h1>
  <div class="stats">
    <div>Tier <span id="tier">-</span> | Due: <span id="due">-</span></div>
    <div><span id="session-count">0</span> drills | <span id="session-score">-</span></div>
  </div>
</div>

<div class="main">
  <div class="tier-badge" id="tier-label"></div>
  <img class="drill-image" id="drill-image" alt="">
  <div class="word-display" id="word-display"></div>
  <div class="carrier" id="carrier-display"></div>
  <div class="latency" id="latency-display"></div>
  <div class="rating-label" id="rating-label"></div>
  <div class="penalty" id="penalty-display"></div>
  <div class="explanation" id="explanation"></div>
  <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>

  <div class="mic-score" id="mic-score"></div>
  <div class="mic-msg" id="mic-msg"></div>
  <button class="mic-btn" id="mic-btn" onclick="toggleMic()">&#x1F3A4;</button>
  <div class="mic-denied" id="mic-denied">Mic access denied — check browser permissions</div>
  <button class="shadow-btn" id="shadow-btn" onclick="startShadow()">Sombra</button>

  <div class="cloze-input-wrap" id="cloze-wrap">
    <input class="cloze-input" id="cloze-input" type="text" placeholder="Qual palavra?" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
    <button class="cloze-submit" id="cloze-submit" onclick="submitCloze()">Enviar</button>
  </div>
  <div class="cloze-result" id="cloze-result"></div>
  <div class="cloze-answer" id="cloze-answer"></div>

  <audio id="player" preload="auto"></audio>
  <audio id="explain-player" preload="auto"></audio>
  <audio id="mic-playback" preload="auto"></audio>

  <button id="tap-zone" disabled>Loading...</button>

  <div class="trap-zone" id="trap-zone">
    <button class="trap-btn" onclick="trapReact('lá ele')">Lá ele!</button>
    <button class="trap-btn" onclick="trapReact('oxe')">Oxe!</button>
    <button class="trap-btn" onclick="trapReact('vixe')">Vixe!</button>
    <button class="trap-btn" onclick="trapReact('eita')">Eita!</button>
  </div>
</div>

<div class="footer">
  <span id="mastery-label">Mastery: -</span>
  <span id="time-label"></span>
</div>

<script>
const $ = id => document.getElementById(id);
const player = $('player');
const explainPlayer = $('explain-player');
const micPlayback = $('mic-playback');
const tapZone = $('tap-zone');
const trapZone = $('trap-zone');
const micBtn = $('mic-btn');
const shadowBtn = $('shadow-btn');

const isWeakMode = new URLSearchParams(window.location.search).get('mode') === 'weak';
if (isWeakMode) {
  document.querySelector('.header h1').textContent = 'PALAVRAS FRACAS';
}

let state = 'loading'; // loading, playing, waiting, result, mic, shadow, cloze, trap
let audioEndTime = 0;
let currentWord = null;
let sessionCount = 0;
let sessionCorrect = 0;
let trapStart = 0;

// Mic recording state
let mediaRecorder = null;
let micChunks = [];
let isRecording = false;
let micAttempts = 0;
let lastDrillRating = 0;

// ── Fetch next word from server ──────────────────────────
async function fetchNext() {
  setState('loading');
  resetMicUI();
  resetClozeUI();
  try {
    const res = await fetch(isWeakMode ? '/api/next?mode=weak' : '/api/next');
    const data = await res.json();

    if (data.error) {
      $('word-display').textContent = 'Nenhuma palavra!';
      $('carrier-display').textContent = 'Todas revisadas. Descansa, parceiro.';
      tapZone.textContent = 'Done';
      return;
    }

    currentWord = data;
    $('tier').textContent = data.tier;
    $('tier-label').textContent = data.tier_label;
    $('due').textContent = data.due_count;
    $('mastery-label').textContent = 'Mastery: ' + data.mastery + '/5';

    if (data.type === 'trap') {
      showTrap(data);
    } else if (data.type === 'cloze') {
      showCloze(data);
    } else {
      showDrill(data);
    }
  } catch (e) {
    $('word-display').textContent = 'Erro';
    $('carrier-display').textContent = e.message;
    setTimeout(fetchNext, 3000);
  }
}

function resetMicUI() {
  micBtn.classList.remove('visible', 'recording');
  shadowBtn.classList.remove('visible');
  $('mic-score').textContent = '';
  $('mic-msg').textContent = '';
  $('mic-denied').style.display = 'none';
  micAttempts = 0;
  isRecording = false;
}

function resetClozeUI() {
  $('cloze-wrap').classList.remove('visible');
  $('cloze-input').value = '';
  $('cloze-result').textContent = '';
  $('cloze-result').className = 'cloze-result';
  $('cloze-answer').textContent = '';
}

function showDrill(data) {
  $('word-display').textContent = '';
  $('carrier-display').textContent = '';
  $('latency-display').textContent = '';
  $('rating-label').textContent = '';
  $('explanation').classList.remove('visible');
  $('explanation').textContent = '';
  explainPlayer.pause();

  trapZone.style.display = 'none';
  tapZone.style.display = 'block';

  const img = $('drill-image');
  const playAudio = () => {
    player.src = '/audio/' + data.audio_file;
    player.onended = () => {
      setState('waiting');
      audioEndTime = performance.now();
    };
    player.onerror = () => { setTimeout(fetchNext, 1000); };
    setState('playing');
    player.play().catch(() => {
      tapZone.textContent = 'TAP TO PLAY';
      tapZone.disabled = false;
      tapZone.onclick = () => { player.play(); tapZone.onclick = handleTap; };
    });
  };

  if (data.image_file) {
    img.onload = () => { setTimeout(playAudio, 800); };
    img.onerror = () => { playAudio(); };
    img.src = '/image/' + data.image_file;
    img.classList.add('visible');
    setState('image');
  } else {
    img.classList.remove('visible');
    playAudio();
  }
}

// ── Cloze Drill ──────────────────────────────────────────
function showCloze(data) {
  $('word-display').textContent = '';
  $('carrier-display').textContent = '';
  $('latency-display').textContent = '';
  $('rating-label').textContent = '';
  $('explanation').classList.remove('visible');
  explainPlayer.pause();

  trapZone.style.display = 'none';
  tapZone.style.display = 'none';

  const img = $('drill-image');
  if (data.image_file) {
    img.src = '/image/' + data.image_file;
    img.classList.add('visible');
  } else {
    img.classList.remove('visible');
  }

  player.src = '/audio/' + data.audio_file;
  player.onended = () => {
    setState('cloze');
    $('cloze-wrap').classList.add('visible');
    $('cloze-input').focus();
  };
  setState('playing');
  player.play().catch(() => {
    setState('cloze');
    $('cloze-wrap').classList.add('visible');
    $('cloze-input').focus();
  });
}

$('cloze-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') submitCloze();
});

async function submitCloze() {
  const answer = $('cloze-input').value.trim();
  if (!answer) return;

  $('cloze-submit').disabled = true;
  $('cloze-input').disabled = true;

  const res = await fetch('/api/cloze-respond', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      word_id: currentWord.word_id,
      answer: answer,
      expected: currentWord.word,
    }),
  });
  const data = await res.json();

  $('cloze-wrap').classList.remove('visible');

  if (data.correct) {
    $('cloze-result').textContent = currentWord.word;
    $('cloze-result').className = 'cloze-result correct';
  } else {
    $('cloze-result').textContent = answer;
    $('cloze-result').className = 'cloze-result wrong';
    $('cloze-answer').textContent = 'Resposta: ' + currentWord.word;
  }
  $('word-display').textContent = currentWord.word;
  $('carrier-display').textContent = currentWord.full_carrier || '';

  $('rating-label').textContent = data.rating_name;
  sessionCount++;
  if (data.rating >= 3) sessionCorrect++;
  updateSessionStats();

  $('cloze-submit').disabled = false;
  $('cloze-input').disabled = false;
  $('cloze-input').value = '';

  setTimeout(fetchNext, 2500);
}

function showTrap(data) {
  $('word-display').textContent = '';
  $('carrier-display').textContent = '';
  $('latency-display').textContent = '';
  $('rating-label').textContent = '';
  $('drill-image').classList.remove('visible');

  tapZone.style.display = 'none';
  trapZone.style.display = 'flex';

  player.src = '/audio/' + data.audio_file;
  player.onended = () => {
    trapStart = performance.now();
    $('word-display').textContent = '\u{1F3AD}';
    $('carrier-display').textContent = 'REACT!';
  };

  setState('trap');
  player.play().catch(() => {
    trapZone.querySelectorAll('.trap-btn')[0].click();
  });
}

async function trapReact(reaction) {
  const latency = trapStart > 0 ? Math.round(performance.now() - trapStart) : 9999;
  trapZone.style.display = 'none';
  tapZone.style.display = 'block';

  const res = await fetch('/api/trap-respond', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      sentence: currentWord.trap_sentence,
      reaction: reaction,
      latency_ms: latency,
    }),
  });
  const data = await res.json();

  $('latency-display').textContent = latency + 'ms';
  $('latency-display').className = 'latency ' + (data.passed ? 'fast' : 'slow');
  $('rating-label').textContent = data.passed ? 'Sobreviveu!' : '\u{1F34A} LARANJADA!';
  $('carrier-display').textContent = data.expected;
  if (data.penalty_remaining > 0) {
    $('penalty-display').textContent = '\u{1F34A} Penalty: ' + data.penalty_remaining + ' restantes';
  }

  sessionCount++;
  if (data.passed) sessionCorrect++;
  updateSessionStats();

  setTimeout(fetchNext, 2500);
}

function setState(s) {
  state = s;
  if (s === 'loading') {
    tapZone.textContent = 'Carregando...';
    tapZone.disabled = true;
    tapZone.className = '';
    tapZone.style.display = 'block';
    $('word-display').innerHTML = '<span class="loading pulsing">\u25CF\u25CF\u25CF</span>';
  } else if (s === 'image') {
    tapZone.textContent = 'Olha...';
    tapZone.disabled = true;
  } else if (s === 'playing') {
    tapZone.textContent = 'Ouvindo...';
    tapZone.disabled = true;
  } else if (s === 'waiting') {
    tapZone.textContent = 'SEI';
    tapZone.disabled = false;
    tapZone.onclick = handleTap;
  } else if (s === 'result') {
    tapZone.textContent = 'Pr\u00F3ximo...';
    tapZone.disabled = true;
  } else if (s === 'mic') {
    tapZone.style.display = 'none';
    micBtn.classList.add('visible');
  } else if (s === 'shadow') {
    tapZone.style.display = 'none';
  } else if (s === 'cloze') {
    tapZone.style.display = 'none';
  }
}

async function handleTap() {
  if (state !== 'waiting') return;
  const latencyMs = Math.round(performance.now() - audioEndTime);

  setState('result');

  $('word-display').textContent = currentWord.word;
  $('carrier-display').textContent = currentWord.carrier;

  $('latency-display').textContent = latencyMs + 'ms';
  if (latencyMs <= 600) {
    $('latency-display').className = 'latency fast';
  } else if (latencyMs <= 1000) {
    $('latency-display').className = 'latency ok';
  } else {
    $('latency-display').className = 'latency slow';
  }

  const res = await fetch('/api/respond', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      word_id: currentWord.word_id,
      latency_ms: latencyMs,
    }),
  });
  const data = await res.json();

  lastDrillRating = data.rating;
  $('rating-label').textContent = data.rating_name;
  $('mastery-label').textContent = 'Mastery: ' + data.new_mastery + '/5';
  if (data.penalty_active) {
    $('penalty-display').textContent = '\u{1F34A} Laranjada \u2014 forced Hard';
  } else {
    $('penalty-display').textContent = '';
  }

  if (data.tier_progress !== undefined) {
    $('progress-fill').style.width = data.tier_progress + '%';
  }

  sessionCount++;
  if (data.rating >= 3) sessionCorrect++;
  updateSessionStats();

  // On miss — fetch and play explanation, then show mic
  if (data.rating <= 2) {
    fetch('/api/explain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ word: currentWord.word }),
    }).then(r => r.json()).then(ex => {
      if (ex.explanation) {
        $('explanation').textContent = ex.explanation;
        $('explanation').classList.add('visible');
      }
      if (ex.audio_file) {
        explainPlayer.src = '/audio/' + ex.audio_file;
        explainPlayer.play().catch(() => {});
        explainPlayer.onended = () => { showMicPhase(); };
        return;
      }
      setTimeout(() => { showMicPhase(); }, 2000);
    }).catch(() => { showMicPhase(); });
  } else {
    setTimeout(() => { showMicPhase(); }, 800);
  }
}

// ── Mic Recording (Feature 1) ───────────────────────────
function showMicPhase() {
  micAttempts = 0;
  setState('mic');
  $('mic-msg').textContent = 'Toca pra gravar sua pron\u00FAncia';
}

async function toggleMic() {
  if (isRecording) {
    stopRecording();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) micChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(micChunks, { type: mediaRecorder.mimeType || 'audio/mp4' });
      await uploadPronunciation(blob);
    };
    mediaRecorder.start();
    isRecording = true;
    micBtn.classList.add('recording');
    $('mic-msg').textContent = 'Gravando... toca pra parar';
    setTimeout(() => { if (isRecording) stopRecording(); }, 5000);
  } catch (e) {
    $('mic-denied').style.display = 'block';
    $('mic-msg').textContent = '';
    setTimeout(fetchNext, 2500);
  }
}

function stopRecording() {
  if (mediaRecorder && isRecording) {
    isRecording = false;
    micBtn.classList.remove('recording');
    $('mic-msg').textContent = 'Analisando...';
    mediaRecorder.stop();
  }
}

async function uploadPronunciation(blob) {
  micAttempts++;
  const fd = new FormData();
  fd.append('audio', blob, 'recording.m4a');
  fd.append('word_id', currentWord.word_id);
  fd.append('native_audio', currentWord.audio_file);

  try {
    const res = await fetch('/api/score-pronunciation', { method: 'POST', body: fd });
    const data = await res.json();

    const score = data.score || 0;
    $('mic-score').textContent = score + '/100';
    $('mic-score').className = 'mic-score ' + (score >= 85 ? 'pass' : 'fail');

    if (score >= 85) {
      $('mic-msg').textContent = 'Massa! Pron\u00FAncia boa.';
      micBtn.classList.remove('visible');
      shadowBtn.classList.add('visible');
      setTimeout(() => {
        if (!shadowBtn.classList.contains('visible')) return;
        shadowBtn.classList.remove('visible');
        fetchNext();
      }, 5000);
    } else if (micAttempts >= 3) {
      $('mic-msg').textContent = 'Bora pra frente. Pratica depois.';
      micBtn.classList.remove('visible');
      setTimeout(fetchNext, 2000);
    } else {
      $('mic-msg').textContent = 'De novo! (' + micAttempts + '/3)';
      player.src = '/audio/' + currentWord.audio_file;
      player.play().catch(() => {});
    }
  } catch (e) {
    $('mic-msg').textContent = 'Erro na an\u00E1lise';
    setTimeout(fetchNext, 2000);
  }
}

// ── Shadow Mode (Feature 4) ─────────────────────────────
let shadowAttempts = 0;

async function startShadow() {
  shadowBtn.classList.remove('visible');
  setState('shadow');
  shadowAttempts = 0;
  doShadowRound();
}

async function doShadowRound() {
  shadowAttempts++;
  $('mic-score').textContent = '';
  $('mic-msg').textContent = 'Ouvindo nativo...';

  player.src = '/audio/' + currentWord.audio_file;
  player.onended = async () => {
    $('mic-msg').textContent = 'Gravando sombra...';
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      micChunks = [];
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = e => { if (e.data.size > 0) micChunks.push(e.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(micChunks, { type: rec.mimeType || 'audio/mp4' });
        await uploadShadow(blob);
      };
      rec.start();
      micBtn.classList.add('visible', 'recording');
      isRecording = true;

      const dur = player.duration || 3;
      setTimeout(() => {
        if (rec.state === 'recording') {
          rec.stop();
          isRecording = false;
          micBtn.classList.remove('recording');
          $('mic-msg').textContent = 'Analisando sombra...';
        }
      }, (dur + 1) * 1000);
    } catch (e) {
      $('mic-denied').style.display = 'block';
      setTimeout(fetchNext, 2000);
    }
  };
  player.play().catch(() => {});
}

async function uploadShadow(blob) {
  const fd = new FormData();
  fd.append('audio', blob, 'shadow.m4a');
  fd.append('word_id', currentWord.word_id);
  fd.append('native_audio', currentWord.audio_file);

  try {
    const res = await fetch('/api/shadow-score', { method: 'POST', body: fd });
    const data = await res.json();

    const score = data.score || 0;
    $('mic-score').textContent = score + '/100';
    $('mic-score').className = 'mic-score ' + (score >= 85 ? 'pass' : 'fail');

    $('mic-msg').textContent = 'Comparando...';
    player.src = '/audio/' + currentWord.audio_file;
    player.onended = () => {
      if (data.user_audio_url) {
        micPlayback.src = data.user_audio_url;
        micPlayback.onended = () => { finishShadowRound(score); };
        micPlayback.play().catch(() => { finishShadowRound(score); });
      } else {
        finishShadowRound(score);
      }
    };
    player.play().catch(() => { finishShadowRound(score); });
  } catch (e) {
    $('mic-msg').textContent = 'Erro na an\u00E1lise';
    micBtn.classList.remove('visible');
    setTimeout(fetchNext, 2000);
  }
}

function finishShadowRound(score) {
  if (score >= 85) {
    $('mic-msg').textContent = 'Sombra perfeita!';
    micBtn.classList.remove('visible');
    setTimeout(fetchNext, 2000);
  } else if (shadowAttempts >= 3) {
    $('mic-msg').textContent = 'Bora pra frente.';
    micBtn.classList.remove('visible');
    setTimeout(fetchNext, 2000);
  } else {
    $('mic-msg').textContent = 'De novo! (' + shadowAttempts + '/3)';
    setTimeout(() => doShadowRound(), 1000);
  }
}

function updateSessionStats() {
  $('session-count').textContent = sessionCount;
  const pct = sessionCount > 0 ? Math.round(sessionCorrect / sessionCount * 100) : 0;
  $('session-score').textContent = pct + '% accuracy';
}

function updateTime() {
  $('time-label').textContent = new Date().toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});
}
setInterval(updateTime, 10000);
updateTime();

// Start
fetchNext();
</script>

<!-- Bottom Tab Bar -->
<style>
  .tab-bar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 100;
    display: flex; justify-content: space-around; align-items: center;
    height: 68px; padding-bottom: env(safe-area-inset-bottom, 0);
    background: rgba(10,10,11,0.92); border-top: 1px solid rgba(255,255,255,0.06);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .tab {
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    text-decoration: none; color: #525263; font-size: 0.62em; font-weight: 500;
    -webkit-tap-highlight-color: transparent; padding: 6px 12px; transition: color 0.15s;
  }
  .tab.active { color: #60a5fa; }
  .tab svg { width: 22px; height: 22px; fill: currentColor; }
</style>
<nav class="tab-bar">
  <a href="/" class="tab">
    <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
    <span>Inicio</span>
  </a>
  <a href="/drill" class="tab active">
    <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
    <span>Treinar</span>
  </a>
  <a href="/stories" class="tab">
    <svg viewBox="0 0 24 24"><path d="M21 5c-1.11-.35-2.33-.5-3.5-.5-1.95 0-4.05.4-5.5 1.5-1.45-1.1-3.55-1.5-5.5-1.5S2.45 4.9 1 6v14.65c0 .25.25.5.5.5.1 0 .15-.05.25-.05C3.1 20.45 5.05 20 6.5 20c1.95 0 4.05.4 5.5 1.5 1.35-.85 3.8-1.5 5.5-1.5 1.65 0 3.35.3 4.75 1.05.1.05.15.05.25.05.25 0 .5-.25.5-.5V6c-.6-.45-1.25-.75-2-1z"/></svg>
    <span>Historias</span>
  </a>
  <a href="/conversa" class="tab">
    <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
    <span>Conversa</span>
  </a>
</nav>

</body></html>"""


class DrillHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/drill":
            self._html(DRILL_HTML)

        elif path == "/api/next":
            self._next_word()

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
                self._json({"error": "TTS failed"})
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
            self._json({"error": "no_words_due"})
            return

        carrier = build_carrier(word["word"])
        fname = generate_tts(carrier)
        if not fname:
            self._json({"error": "TTS failed"})
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
        rating_name = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}[rating.value]

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
