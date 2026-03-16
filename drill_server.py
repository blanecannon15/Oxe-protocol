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
  <div class="pass-instruction" id="pass-instruction"></div>
  <div class="rep-counter" id="rep-counter"></div>
  <div class="rating-feedback" id="rating-feedback"></div>

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
let drillStartTime = 0;

// ── Fetch next chunk ──────────────────────────────────────
async function fetchNext() {
  showLoading();
  try {
    const res = await fetch('/api/drill/next');
    const data = await res.json();

    if (data.error) {
      $('pass-label').textContent = '';
      $('pass-instruction').textContent = 'Todas revisadas. Descansa, parceiro.';
      $('action-area').innerHTML = '';
      return;
    }

    currentChunk = data;
    currentPass = data.current_pass || 1;
    masteryReps = 0;
    retries = 0;
    drillStartTime = performance.now();

    $('tier-label').textContent = data.tier_label || ('Tier ' + data.tier);
    $('due-label').textContent = (data.due_count || 0) + ' pendentes';

    const img = $('drill-image');
    if (data.image_file) {
      img.src = '/image/' + data.image_file;
      img.classList.add('visible');
    } else {
      img.classList.remove('visible');
    }

    enterPass(currentPass);
  } catch (e) {
    $('pass-instruction').textContent = 'Erro: ' + e.message;
    setTimeout(fetchNext, 3000);
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

  // Carrier text: visible only in pass 3 and 4
  const ct = $('carrier-text');
  if (passNum === 3 || passNum === 4) {
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
  const latencyMs = Math.round(performance.now() - drillStartTime);
  $('rep-counter').classList.remove('visible');
  $('action-area').innerHTML = '';

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

    const ratingNames = {1: 'De novo', 2: 'Dif\u00EDcil', 3: 'Bom', 4: 'F\u00E1cil'};
    const fb = $('rating-feedback');
    fb.textContent = ratingNames[data.rating] || 'Bom';
    fb.style.color = data.rating >= 3 ? '#34d399' : '#fbbf24';
    fb.classList.add('visible');
  } catch (e) {
    sessionCount++;
    updateSessionStats();
  }

  setTimeout(fetchNext, 1800);
}

// ── Audio ─────────────────────────────────────────────────
function playAudio() {
  if (!currentChunk || !currentChunk.audio_file) return;
  player.src = '/audio/' + currentChunk.audio_file;
  player.onended = null;
  player.onerror = null;
  player.play().catch(() => {});
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
}

function updateTime() {
  $('time-label').textContent = new Date().toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});
}
setInterval(updateTime, 10000);
updateTime();

// ── Start ─────────────────────────────────────────────────
fetchNext();
</script>

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
