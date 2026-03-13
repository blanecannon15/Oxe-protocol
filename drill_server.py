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
LOG_DIR = Path(__file__).parent / "voca_vault" / "logs"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
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

DRILL_HTML = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Oxe Protocol</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d1117; color: #e6edf3; font-family: system-ui, -apple-system, sans-serif;
    display: flex; flex-direction: column; min-height: 100vh; min-height: 100dvh;
    overflow: hidden; -webkit-user-select: none; user-select: none;
  }
  .header {
    padding: 16px 20px; background: #161b22; border-bottom: 1px solid #30363d;
    display: flex; justify-content: space-between; align-items: center;
  }
  .header h1 { font-size: 1.1em; color: #f7931e; }
  .stats { font-size: 0.75em; color: #8b949e; text-align: right; }
  .main {
    flex: 1; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 20px; gap: 24px;
  }
  .word-display {
    font-size: 2.4em; font-weight: 700; color: #f7931e;
    min-height: 1.2em; text-align: center;
  }
  .carrier { font-size: 1em; color: #8b949e; text-align: center; min-height: 1.5em; }
  .tier-badge {
    font-size: 0.75em; padding: 4px 12px; border-radius: 12px;
    background: #1f2937; color: #9ca3af; display: inline-block;
  }
  .latency {
    font-size: 3em; font-weight: 700; min-height: 1.2em;
    transition: color 0.3s;
  }
  .latency.fast { color: #3fb950; }
  .latency.ok { color: #d29922; }
  .latency.slow { color: #f85149; }
  .rating-label { font-size: 1em; min-height: 1.2em; }
  #tap-zone {
    width: 100%; padding: 28px; font-size: 1.3em; font-weight: 600;
    border: none; border-radius: 16px; cursor: pointer;
    background: #238636; color: #fff; transition: all 0.15s;
    -webkit-tap-highlight-color: transparent;
  }
  #tap-zone:active { transform: scale(0.97); background: #2ea043; }
  #tap-zone:disabled { background: #21262d; color: #484f58; }
  .trap-zone {
    width: 100%; display: none; gap: 8px;
  }
  .trap-btn {
    flex: 1; padding: 16px 8px; font-size: 1em; font-weight: 600;
    border: 2px solid #30363d; border-radius: 12px; background: #161b22;
    color: #e6edf3; cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  .trap-btn:active { background: #f7931e; color: #000; border-color: #f7931e; }
  .penalty { color: #f85149; font-size: 0.85em; min-height: 1.2em; }
  .progress-bar {
    width: 100%; height: 4px; background: #21262d; border-radius: 2px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%; background: #f7931e; transition: width 0.5s;
  }
  .footer {
    padding: 12px 20px; background: #161b22; border-top: 1px solid #30363d;
    display: flex; justify-content: space-between; font-size: 0.75em; color: #484f58;
  }
  .loading { color: #8b949e; font-size: 1.2em; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
  .pulsing { animation: pulse 1.5s infinite; }
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
  <div class="word-display" id="word-display"></div>
  <div class="carrier" id="carrier-display"></div>
  <div class="latency" id="latency-display"></div>
  <div class="rating-label" id="rating-label"></div>
  <div class="penalty" id="penalty-display"></div>
  <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>

  <audio id="player" preload="auto"></audio>

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
const tapZone = $('tap-zone');
const trapZone = $('trap-zone');

let state = 'loading'; // loading, playing, waiting, result, trap
let audioEndTime = 0;
let currentWord = null;
let sessionCount = 0;
let sessionCorrect = 0;
let trapStart = 0;

// ── Fetch next word from server ──────────────────────────
async function fetchNext() {
  setState('loading');
  try {
    const res = await fetch('/api/next');
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
    } else {
      showDrill(data);
    }
  } catch (e) {
    $('word-display').textContent = 'Erro';
    $('carrier-display').textContent = e.message;
    setTimeout(fetchNext, 3000);
  }
}

function showDrill(data) {
  // Don't show the word yet — audio first (zero-reading)
  $('word-display').textContent = '';
  $('carrier-display').textContent = '';
  $('latency-display').textContent = '';
  $('rating-label').textContent = '';

  trapZone.style.display = 'none';
  tapZone.style.display = 'block';

  player.src = '/audio/' + data.audio_file;
  player.onended = () => {
    audioEndTime = performance.now();
    setState('waiting');
  };
  player.onerror = () => {
    // Skip to next on audio error
    setTimeout(fetchNext, 1000);
  };

  setState('playing');
  player.play().catch(() => {
    // Autoplay blocked — let user tap to start
    tapZone.textContent = 'TAP TO PLAY';
    tapZone.disabled = false;
    tapZone.onclick = () => { player.play(); tapZone.onclick = handleTap; };
  });
}

function showTrap(data) {
  $('word-display').textContent = '';
  $('carrier-display').textContent = '';
  $('latency-display').textContent = '';
  $('rating-label').textContent = '';

  tapZone.style.display = 'none';
  trapZone.style.display = 'flex';

  player.src = '/audio/' + data.audio_file;
  player.onended = () => {
    trapStart = performance.now();
    // Show trap buttons
    $('word-display').textContent = '🎭';
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
  $('rating-label').textContent = data.passed ? 'Sobreviveu!' : '🍊 LARANJADA!';
  $('carrier-display').textContent = data.expected;
  if (data.penalty_remaining > 0) {
    $('penalty-display').textContent = '🍊 Penalty: ' + data.penalty_remaining + ' restantes';
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
    $('word-display').innerHTML = '<span class="loading pulsing">●●●</span>';
  } else if (s === 'playing') {
    tapZone.textContent = 'Ouvindo...';
    tapZone.disabled = true;
  } else if (s === 'waiting') {
    tapZone.textContent = 'SHADOW → TAP';
    tapZone.disabled = false;
    tapZone.onclick = handleTap;
  } else if (s === 'result') {
    tapZone.textContent = 'Próximo...';
    tapZone.disabled = true;
  }
}

async function handleTap() {
  if (state !== 'waiting') return;
  const latencyMs = Math.round(performance.now() - audioEndTime);

  setState('result');

  // Show word now (after attempt)
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

  // Send response to server
  const res = await fetch('/api/respond', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      word_id: currentWord.word_id,
      latency_ms: latencyMs,
    }),
  });
  const data = await res.json();

  $('rating-label').textContent = data.rating_name;
  $('mastery-label').textContent = 'Mastery: ' + data.new_mastery + '/5';
  if (data.penalty_active) {
    $('penalty-display').textContent = '🍊 Laranjada — forced Hard';
  } else {
    $('penalty-display').textContent = '';
  }

  // Update progress bar
  if (data.tier_progress !== undefined) {
    $('progress-fill').style.width = data.tier_progress + '%';
  }

  sessionCount++;
  if (data.rating >= 3) sessionCorrect++;
  updateSessionStats();

  setTimeout(fetchNext, 2000);
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

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/respond":
            self._handle_respond(body)

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

        tier = get_unlocked_tier()
        due = get_due_words()
        due_count = len(list(due))

        self._json({
            "type": "drill",
            "word_id": word["id"],
            "word": word["word"],
            "carrier": carrier,
            "audio_file": fname,
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
