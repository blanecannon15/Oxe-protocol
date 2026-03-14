"""
oxe_server.py — Unified Oxe Protocol server.

Single entry point that serves:
  - Home screen with Drills + Stories navigation
  - Drill interface (from drill_server.py) at /drill
  - Story interface (from story_server.py) at /stories
  - Shared audio serving at /audio/

Works on both phone (Safari) and Mac (any browser).

Usage:
    source ~/.profile && python3 oxe_server.py              # port 7777
    source ~/.profile && python3 oxe_server.py --port 9000  # custom port
"""

import http.server
import json
import os
import random
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fsrs import Rating

from srs_engine import (
    get_next_word, get_due_words, record_review,
    get_unlocked_tier, tier_progress, TIER_LABELS, DB_PATH,
)
from story_gen import LEVELS, init_story_db, generate_story, generate_story_audio

import sqlite3

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
LOG_DIR = Path(__file__).parent / "voca_vault" / "logs"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

LATENCY_THRESHOLD_MS = 1000
TRAP_PROBABILITY = 0.15
TRAP_LATENCY_MS = 800

# ── Imports from drill_server ──────────────────────────────────
from drill_server import (
    DRILL_HTML, build_carrier, generate_tts, generate_image, generate_explanation,
    prefetch_images, log_drill, TRAP_SENTENCES, TRAP_REACTIONS, IMAGE_DIR,
)

# ── Imports from story_server ──────────────────────────────────
from story_server import STORY_HTML

# Session state
_laranjada_remaining = 0


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── Home Page ──────────────────────────────────────────────────

HOME_HTML = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Oxe Protocol</title>
<style>
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(20px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column;
    align-items: center; justify-content: space-between; padding: 0;
    -webkit-user-select: none; user-select: none;
    overflow: hidden;
  }
  .bg-glow {
    position: fixed; top: -40%; left: 50%; transform: translateX(-50%);
    width: 600px; height: 600px;
    background: radial-gradient(circle, rgba(94,106,210,0.12) 0%, transparent 70%);
    pointer-events: none; z-index: 0;
  }
  .content {
    position: relative; z-index: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center; flex: 1;
    width: 100%; max-width: 420px; padding: 48px 24px 24px;
  }
  .brand {
    animation: fadeUp 0.6s ease-out;
    margin-bottom: 48px; text-align: center;
  }
  .logo {
    font-size: 3em; font-weight: 800; letter-spacing: -2px;
    background: linear-gradient(135deg, #5E6AD2 0%, #8B5CF6 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  .subtitle {
    font-size: 0.8em; color: #525263; margin-top: 6px;
    font-weight: 500; letter-spacing: 2px; text-transform: uppercase;
  }
  .cards {
    display: flex; flex-direction: column; gap: 14px;
    width: 100%; animation: fadeUp 0.6s ease-out 0.1s both;
  }
  .card {
    display: flex; align-items: center; gap: 18px;
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px; padding: 22px 24px; cursor: pointer;
    transition: all 0.2s ease; text-decoration: none; color: inherit;
    -webkit-tap-highlight-color: transparent;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .card:active {
    transform: scale(0.98);
    background: rgba(255,255,255,0.07);
  }
  .card-icon {
    width: 52px; height: 52px; border-radius: 14px;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.5em; flex-shrink: 0;
  }
  .card.drill .card-icon {
    background: linear-gradient(135deg, rgba(94,106,210,0.2), rgba(94,106,210,0.05));
  }
  .card.stories .card-icon {
    background: linear-gradient(135deg, rgba(139,92,246,0.2), rgba(139,92,246,0.05));
  }
  .card-text { flex: 1; }
  .card-title {
    font-size: 1.05em; font-weight: 700; margin-bottom: 3px;
  }
  .card.drill .card-title { color: #818cf8; }
  .card.stories .card-title { color: #a78bfa; }
  .card-desc {
    font-size: 0.78em; color: #525263; line-height: 1.4; font-weight: 400;
  }
  .card-arrow {
    color: #333; font-size: 1.2em; font-weight: 300; flex-shrink: 0;
  }
  .stats {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 0;
    width: 100%; margin-top: 40px;
    animation: fadeUp 0.6s ease-out 0.2s both;
  }
  .stat {
    text-align: center; padding: 16px 0;
    border-right: 1px solid rgba(255,255,255,0.04);
  }
  .stat:last-child { border-right: none; }
  .stat-value {
    font-size: 1.5em; font-weight: 700; color: #fafafa;
    font-variant-numeric: tabular-nums;
  }
  .stat-label {
    font-size: 0.65em; color: #525263; margin-top: 4px;
    text-transform: uppercase; letter-spacing: 1px; font-weight: 500;
  }
  .footer-bar {
    position: relative; z-index: 1; width: 100%; padding: 20px 0;
    text-align: center;
    animation: fadeUp 0.6s ease-out 0.3s both;
  }
  .dot {
    display: inline-block; width: 4px; height: 4px; border-radius: 50%;
    background: #5E6AD2; margin: 0 6px; vertical-align: middle;
    animation: pulse 3s ease-in-out infinite;
  }
  .footer-text {
    font-size: 0.7em; color: #333; font-weight: 400;
  }
</style>
</head><body>

<div class="bg-glow"></div>

<div class="content">
  <div class="brand">
    <div class="logo">OXE</div>
    <div class="subtitle">Parceiro Soteropolitano</div>
  </div>

  <div class="cards">
    <a href="/drill" class="card drill">
      <div class="card-icon">&#x1f3af;</div>
      <div class="card-text">
        <div class="card-title">Treinar</div>
        <div class="card-desc">Audio-first 1+T drills com SRS</div>
      </div>
      <div class="card-arrow">&#x203A;</div>
    </a>
    <a href="/stories" class="card stories">
      <div class="card-icon">&#x1f4d6;</div>
      <div class="card-text">
        <div class="card-title">Historias</div>
        <div class="card-desc">Narrativas graduadas de Salvador</div>
      </div>
      <div class="card-arrow">&#x203A;</div>
    </a>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-value" id="tier">-</div>
      <div class="stat-label">Tier</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="due">-</div>
      <div class="stat-label">Due</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="mastery">-</div>
      <div class="stat-label">Mastery</div>
    </div>
    <div class="stat">
      <div class="stat-value" id="stories">-</div>
      <div class="stat-label">Stories</div>
    </div>
  </div>
</div>

<div class="footer-bar">
  <span class="footer-text">Salvador, Bahia</span>
  <span class="dot"></span>
  <span class="footer-text">Oxe Protocol</span>
</div>

<script>
fetch('/api/home-stats').then(r=>r.json()).then(d=>{
  document.getElementById('tier').textContent=d.tier;
  document.getElementById('due').textContent=d.due;
  document.getElementById('mastery').textContent=d.mastery_pct+'%';
  document.getElementById('stories').textContent=d.story_count;
});
</script>
</body></html>"""


# ── Unified Handler ────────────────────────────────────────────

class OxeHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # ── Home ──
        if path == "/":
            self._html(HOME_HTML)

        # ── Drill ──
        elif path == "/drill":
            self._html(DRILL_HTML)
        elif path == "/api/next":
            self._drill_next()

        # ── Stories ──
        elif path == "/stories":
            self._html(STORY_HTML)
        elif path == "/api/levels":
            self._story_get_levels()
        elif path == "/api/stories":
            level = query.get("level", ["A1"])[0]
            self._story_get_stories(level)
        elif path.startswith("/api/story/") and path.count("/") == 3:
            story_id = int(path.split("/")[3])
            self._story_get_story(story_id)

        # ── Shared ──
        elif path == "/api/home-stats":
            self._home_stats()
        elif path.startswith("/audio/"):
            self._serve_audio(path[7:])
        elif path.startswith("/image/"):
            self._serve_image(path[7:])
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        # ── Drill ──
        if path == "/api/respond":
            self._drill_respond(body)
        elif path == "/api/explain":
            self._drill_explain(body)
        elif path == "/api/trap-respond":
            self._drill_trap_respond(body)

        # ── Stories ──
        elif path == "/api/generate":
            self._story_generate(body)
        elif path.endswith("/audio") and path.startswith("/api/story/"):
            story_id = int(path.split("/")[3])
            self._story_gen_audio(story_id)
        elif path.endswith("/play") and path.startswith("/api/story/"):
            story_id = int(path.split("/")[3])
            self._story_record_play(story_id)
        elif path == "/api/answer":
            self._story_log_answer(body)
        elif path.endswith("/result") and path.startswith("/api/story/"):
            story_id = int(path.split("/")[3])
            self._story_save_result(story_id, body)
        else:
            self.send_error(404)

    # ── Home Stats ─────────────────────────────────────────

    def _home_stats(self):
        tier = get_unlocked_tier()
        due = len(list(get_due_words()))
        progress = tier_progress()
        current_pct = 0
        for t, label, mastered, total, pct in progress:
            if t == tier:
                current_pct = round(pct)
                break
        conn = get_conn()
        story_count = conn.execute("SELECT COUNT(*) FROM story_library").fetchone()[0]
        conn.close()
        self._json({
            "tier": tier,
            "due": due,
            "mastery_pct": current_pct,
            "story_count": story_count,
        })

    # ── Drill Endpoints ────────────────────────────────────

    def _drill_next(self):
        global _laranjada_remaining

        if random.random() < TRAP_PROBABILITY:
            trap = random.choice(TRAP_SENTENCES)
            sentence, trap_type, expected = trap
            fname = generate_tts(sentence)
            if not fname:
                self._json({"error": "TTS failed"})
                return
            tier = get_unlocked_tier()
            due_count = len(list(get_due_words()))
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

        # Generate DALL-E image (waits if not cached)
        img_fname = generate_image(word["word"])

        # Pre-fetch images for next 5 due words in background
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
            "due_count": len(due_words),
        })

    def _drill_respond(self, body):
        global _laranjada_remaining
        word_id = body["word_id"]
        latency_ms = body["latency_ms"]

        if latency_ms <= 600:
            rating = Rating.Easy
        elif latency_ms <= LATENCY_THRESHOLD_MS:
            rating = Rating.Good
        elif latency_ms <= 2000:
            rating = Rating.Hard
        else:
            rating = Rating.Again

        penalty_active = False
        if _laranjada_remaining > 0:
            _laranjada_remaining -= 1
            if rating.value > Rating.Hard.value:
                rating = Rating.Hard
            penalty_active = True

        card, new_mastery, downgraded = record_review(word_id, rating, latency_ms)
        rating_name = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}[rating.value]
        log_drill(word_id, str(word_id), rating.value, latency_ms)

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
            "latency_downgraded": downgraded,
            "tier_progress": round(current_pct, 1),
        })

    def _drill_explain(self, body):
        word = body.get("word", "")
        explanation, audio_fname = generate_explanation(word)
        self._json({
            "explanation": explanation,
            "audio_file": audio_fname,
        })

    def _drill_trap_respond(self, body):
        global _laranjada_remaining
        reaction = body.get("reaction", "").lower().strip()
        latency_ms = body.get("latency_ms", 9999)
        sentence = body.get("sentence", "")

        passed = any(v in reaction for v in TRAP_REACTIONS)
        if latency_ms > TRAP_LATENCY_MS:
            passed = False
        if not passed:
            _laranjada_remaining = 5

        expected = ""
        for s, t, e in TRAP_SENTENCES:
            if s == sentence:
                expected = e
                break

        log_file = LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "trap", "sentence": sentence,
            "reaction": reaction, "latency_ms": latency_ms, "passed": passed,
        }
        with open(log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._json({
            "passed": passed,
            "expected": expected,
            "penalty_remaining": _laranjada_remaining,
        })

    # ── Story Endpoints ────────────────────────────────────

    def _story_get_levels(self):
        tier = get_unlocked_tier()
        conn = get_conn()
        levels = []
        for key, lv in LEVELS.items():
            unlocked = tier >= lv["min_tier"]
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM story_library WHERE level = ?", (key,)
            ).fetchone()
            story_count = row["cnt"] if row else 0
            score_row = conn.execute(
                "SELECT comprehension_scores FROM story_library WHERE level = ? AND comprehension_scores != '[]'",
                (key,),
            ).fetchall()
            scores = []
            for sr in score_row:
                try:
                    scores.extend(json.loads(sr["comprehension_scores"]))
                except Exception:
                    pass
            avg_score = round(sum(scores) / len(scores)) if scores else None

            levels.append({
                "key": key,
                "label": lv["label"],
                "description": lv["description"],
                "unlocked": unlocked,
                "story_count": story_count,
                "avg_score": avg_score,
            })
        conn.close()
        self._json({"levels": levels})

    def _story_get_stories(self, level):
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, title, word_count, audio_chunks, times_played FROM story_library WHERE level = ? ORDER BY id",
            (level,),
        ).fetchall()
        conn.close()
        stories = []
        for r in rows:
            ac = json.loads(r["audio_chunks"]) if r["audio_chunks"] else {}
            has_audio = len(ac.get("story_chunks", [])) > 0
            stories.append({
                "id": r["id"], "title": r["title"],
                "word_count": r["word_count"], "has_audio": has_audio,
                "times_played": r["times_played"],
            })
        self._json({"stories": stories})

    def _story_get_story(self, story_id):
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM story_library WHERE id = ?", (story_id,)
        ).fetchone()
        conn.close()
        if not row:
            self._json({"error": "not found"})
            return
        audio_chunks = json.loads(row["audio_chunks"]) if row["audio_chunks"] else {}
        self._json({
            "id": row["id"], "level": row["level"], "title": row["title"],
            "body": row["body"], "word_count": row["word_count"],
            "questions": json.loads(row["questions"]),
            "audio_chunks": audio_chunks,
            "times_played": row["times_played"],
            "setting": row["setting"], "theme": row["theme"],
        })

    def _story_generate(self, body):
        level = body.get("level", "A1")
        if level not in LEVELS:
            self._json({"error": f"Unknown level: {level}"})
            return
        init_story_db()
        story_id = generate_story(level)
        if story_id:
            generate_story_audio(story_id)
            self._json({"id": story_id})
        else:
            self._json({"error": "Generation failed"})

    def _story_gen_audio(self, story_id):
        audio = generate_story_audio(story_id)
        if audio:
            self._json({"audio": audio})
        else:
            self._json({"error": "Audio generation failed"})

    def _story_record_play(self, story_id):
        conn = get_conn()
        conn.execute(
            "UPDATE story_library SET times_played = times_played + 1, last_played = ? WHERE id = ?",
            (datetime.now().isoformat(), story_id),
        )
        conn.commit()
        conn.close()
        self._json({"ok": True})

    def _story_log_answer(self, body):
        log_file = LOG_DIR / f"stories_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        body["timestamp"] = datetime.now().isoformat()
        body["type"] = "comprehension_answer"
        with open(log_file, "a") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
        self._json({"ok": True})

    def _story_save_result(self, story_id, body):
        score = body.get("score", 0)
        conn = get_conn()
        row = conn.execute(
            "SELECT comprehension_scores FROM story_library WHERE id = ?", (story_id,)
        ).fetchone()
        if row:
            scores = json.loads(row["comprehension_scores"]) if row["comprehension_scores"] else []
            scores.append(score)
            conn.execute(
                "UPDATE story_library SET comprehension_scores = ? WHERE id = ?",
                (json.dumps(scores), story_id),
            )
            conn.commit()
        conn.close()
        self._json({"ok": True})

    # ── Shared ─────────────────────────────────────────────

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
        if args and str(args[0]).startswith(("4", "5")):
            super().log_message(format, *args)


# ── Main ───────────────────────────────────────────────────────

def main():
    port = int(os.environ.get("PORT", 7777))
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    init_story_db()
    ip = get_local_ip()
    server = http.server.HTTPServer(("0.0.0.0", port), OxeHandler)

    tier = get_unlocked_tier()
    due = len(list(get_due_words()))

    print(f"\n  Oxe Protocol — Unified Server")
    print(f"  {'='*44}")
    print(f"  Phone:   http://{ip}:{port}")
    print(f"  Mac:     http://localhost:{port}")
    print(f"  Tier:    {tier} ({TIER_LABELS[tier]})")
    print(f"  Due:     {due} words")
    print(f"  {'='*44}")
    print(f"  /        Home (Treinar + Histórias)")
    print(f"  /drill   1+T Drills")
    print(f"  /stories Graded Stories")
    print(f"  {'='*44}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
