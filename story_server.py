"""
story_server.py — Autonomous graded story server for the Oxe Protocol.

Serves first-person Soteropolitano narratives to your phone.
Audio-first, zero-reading, comprehension questions after each story.

Usage:
    source ~/.profile && python3 story_server.py               # port 8888
    source ~/.profile && python3 story_server.py --port 9000   # custom port
"""

import http.server
import json
import os
import random
import socket
import sqlite3
import sys
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
LOG_DIR = Path(__file__).parent / "voca_vault" / "logs"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

from srs_engine import DB_PATH, get_unlocked_tier, TIER_LABELS
from story_gen import LEVELS, init_story_db, generate_story, generate_story_audio


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


# ── HTML UI ────────────────────────────────────────────────────────

STORY_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Oxe Protocol — Histórias</title>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  @keyframes fadeUp { from{opacity:0;transform:translateY(12px)} to{opacity:1;transform:translateY(0)} }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; -webkit-user-select: none; user-select: none;
  }
  .header {
    padding: 14px 20px; background: rgba(255,255,255,0.03);
    border-bottom: 1px solid rgba(255,255,255,0.06);
    display: flex; justify-content: space-between; align-items: center;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    position: sticky; top: 0; z-index: 10;
  }
  .header h1 {
    font-size: 1.05em; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #5E6AD2, #8B5CF6);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .back-btn {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    color: #9ca3af; padding: 6px 14px; border-radius: 8px; font-size: 0.85em;
    cursor: pointer; display: none; backdrop-filter: blur(10px);
  }
  .screen { display: none; padding: 20px; animation: fadeUp 0.4s ease-out; }
  .screen.active { display: block; }

  /* Level Select */
  .level-grid { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
  .level-card {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 16px; padding: 18px 20px; cursor: pointer; transition: all 0.2s;
    -webkit-tap-highlight-color: transparent;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .level-card:active { transform: scale(0.98); border-color: rgba(94,106,210,0.4); }
  .level-card.locked { opacity: 0.25; pointer-events: none; }
  .level-card .level-tag {
    font-size: 0.75em; font-weight: 700; color: #818cf8;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .level-card .level-name { font-size: 1.15em; font-weight: 600; margin: 4px 0; color: #fafafa; }
  .level-card .level-desc { font-size: 0.8em; color: #525263; }
  .level-card .level-stats { font-size: 0.75em; color: #333; margin-top: 8px; }
  .lock-icon { color: #333; }

  /* Story List */
  .story-list { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
  .story-item {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 14px; padding: 16px 18px; cursor: pointer; transition: all 0.2s;
    backdrop-filter: blur(20px);
  }
  .story-item:active { border-color: rgba(94,106,210,0.4); transform: scale(0.99); }
  .story-title { font-size: 1em; font-weight: 600; color: #fafafa; }
  .story-meta { font-size: 0.75em; color: #525263; margin-top: 4px; }
  .gen-btn {
    width: 100%; padding: 14px; margin-top: 16px;
    background: linear-gradient(135deg, #5E6AD2, #7C3AED); color: #fff;
    border: none; border-radius: 14px; font-size: 1em; font-weight: 600;
    cursor: pointer; transition: all 0.2s;
  }
  .gen-btn:active { transform: scale(0.98); opacity: 0.9; }
  .gen-btn:disabled { background: rgba(255,255,255,0.04); color: #333; }

  /* Player */
  .player-wrap {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 60vh; gap: 20px;
  }
  .chunk-dots { display: flex; gap: 6px; flex-wrap: wrap; justify-content: center; }
  .chunk-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: rgba(255,255,255,0.08); transition: all 0.3s;
  }
  .chunk-dot.played { background: #34d399; }
  .chunk-dot.current { background: #818cf8; transform: scale(1.4); }
  .player-status { font-size: 1.1em; text-align: center; min-height: 1.5em; color: #9ca3af; }
  .player-btn {
    width: 80px; height: 80px; border-radius: 50%; border: none;
    background: linear-gradient(135deg, #5E6AD2, #7C3AED); color: #fff;
    font-size: 2em; cursor: pointer; display: flex; align-items: center;
    justify-content: center; transition: all 0.2s;
    box-shadow: 0 0 30px rgba(94,106,210,0.2);
  }
  .player-btn:active { transform: scale(0.95); }
  .show-text-btn {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    color: #525263; padding: 8px 16px; border-radius: 10px; font-size: 0.8em;
    cursor: pointer; transition: all 0.2s;
  }
  .show-text-btn:active { border-color: rgba(255,255,255,0.15); }
  .story-text {
    display: none; background: rgba(255,255,255,0.03); border-radius: 14px;
    padding: 16px; font-size: 1em; line-height: 1.8; max-height: 50vh;
    overflow-y: auto; border: 1px solid rgba(255,255,255,0.06); width: 100%;
  }
  .story-text.visible { display: block; }
  .story-text .chunk-span { color: #333; transition: color 0.3s; }
  .story-text .chunk-span.active { color: #444; }
  .story-text .chunk-span.played { color: #7a7a8a; }
  .story-text .chunk-span .word {
    display: inline; transition: color 0.2s, background 0.2s;
    border-radius: 3px; padding: 0 2px;
  }
  .story-text .chunk-span.active .word.highlight {
    color: #818cf8; background: rgba(129,140,248,0.12);
  }
  .story-text .chunk-span.played .word.highlight {
    color: #7a7a8a; background: none;
  }

  /* Questions */
  .q-wrap {
    display: flex; flex-direction: column; align-items: center; gap: 16px;
    padding-top: 20px;
  }
  .q-number { font-size: 0.8em; color: #525263; }
  .q-text { font-size: 1.05em; text-align: center; font-weight: 500; min-height: 2em; color: #fafafa; }
  .q-options { display: flex; flex-direction: column; gap: 10px; width: 100%; }
  .q-option {
    width: 100%; padding: 14px 18px; background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 14px; color: #fafafa;
    font-size: 0.95em; cursor: pointer; text-align: left; transition: all 0.2s;
    backdrop-filter: blur(10px);
  }
  .q-option:active { border-color: rgba(94,106,210,0.4); }
  .q-option.correct { background: rgba(52,211,153,0.2); border-color: #34d399; }
  .q-option.wrong { background: rgba(248,113,113,0.2); border-color: #f87171; }

  /* Results */
  .result-wrap {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 50vh; gap: 20px;
  }
  .result-score { font-size: 3em; font-weight: 700; }
  .result-score.pass { color: #34d399; }
  .result-score.fail { color: #f87171; }
  .result-label { font-size: 1.1em; color: #525263; }
  .result-btn {
    padding: 16px 32px; border: none; border-radius: 14px;
    font-size: 1.1em; font-weight: 600; cursor: pointer;
    background: linear-gradient(135deg, #5E6AD2, #7C3AED); color: #fff;
    transition: all 0.2s;
  }
  .result-btn:active { transform: scale(0.97); }

  .loading-spinner { color: #525263; font-size: 1.1em; }
  .pulsing { animation: pulse 1.5s infinite; }
</style>
</head><body>

<div class="header">
  <h1>HISTÓRIAS BAIANAS</h1>
  <button class="back-btn" id="back-btn" onclick="goBack()">Voltar</button>
</div>

<!-- Screen 1: Level Select -->
<div class="screen active" id="screen-levels">
  <div class="level-grid" id="level-grid"></div>
</div>

<!-- Screen 2: Story List -->
<div class="screen" id="screen-stories">
  <h2 id="stories-heading" style="font-size:1.1em;color:#818cf8"></h2>
  <div class="story-list" id="story-list"></div>
  <button class="gen-btn" id="gen-btn" onclick="generateStory()">Gerar nova história</button>
</div>

<!-- Screen 3: Player -->
<div class="screen" id="screen-player">
  <div class="player-wrap">
    <div class="chunk-dots" id="chunk-dots"></div>
    <div class="player-status" id="player-status">Preparando...</div>
    <button class="player-btn" id="play-btn" onclick="togglePlay()">&#9654;</button>
    <button class="show-text-btn" id="show-text-btn" onclick="toggleText()">Mostrar texto</button>
    <div class="story-text" id="story-text"></div>
  </div>
</div>

<!-- Screen 4: Questions -->
<div class="screen" id="screen-questions">
  <div class="q-wrap">
    <div class="q-number" id="q-number"></div>
    <div class="q-text" id="q-text"></div>
    <audio id="q-player" preload="auto"></audio>
    <div class="q-options" id="q-options"></div>
  </div>
</div>

<!-- Screen 5: Results -->
<div class="screen" id="screen-results">
  <div class="result-wrap">
    <div class="result-score" id="result-score"></div>
    <div class="result-label" id="result-label"></div>
    <button class="result-btn" onclick="goToLevels()">Voltar</button>
  </div>
</div>

<audio id="audio-player" preload="auto"></audio>

<script>
const $ = id => document.getElementById(id);
const player = $('audio-player');
const qPlayer = $('q-player');

let currentLevel = null;
let currentStory = null;
let storyChunks = [];
let chunkTexts = [];
let questionAudio = [];
let chunkIndex = 0;
let highlightTimer = null;
let questions = [];
let qIndex = 0;
let qCorrect = 0;
let listenedOnce = false;

// ── Navigation ──────────────────────────────────────────
function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  $(id).classList.add('active');
  $('back-btn').style.display = id === 'screen-levels' ? 'none' : 'block';
}

function goBack() {
  if ($('screen-questions').classList.contains('active') ||
      $('screen-player').classList.contains('active')) {
    player.pause();
    qPlayer.pause();
    showScreen('screen-stories');
  } else if ($('screen-stories').classList.contains('active')) {
    showScreen('screen-levels');
  } else {
    showScreen('screen-levels');
  }
}

function goToLevels() {
  showScreen('screen-levels');
  loadLevels();
}

// ── Level Select ────────────────────────────────────────
async function loadLevels() {
  const res = await fetch('/api/levels');
  const data = await res.json();
  const grid = $('level-grid');
  grid.innerHTML = '';

  for (const lv of data.levels) {
    const locked = !lv.unlocked;
    const card = document.createElement('div');
    card.className = 'level-card' + (locked ? ' locked' : '');
    card.innerHTML = `
      <div class="level-tag">${lv.key} — ${lv.label}</div>
      <div class="level-name">${lv.description}</div>
      <div class="level-stats">
        ${locked ? '<span class="lock-icon">🔒 Locked</span>' :
          lv.story_count + ' histórias | ~10 min cada | ' + (lv.avg_score !== null ? lv.avg_score + '% avg' : 'sem pontuação')}
      </div>
    `;
    if (!locked) {
      card.onclick = () => selectLevel(lv.key, lv.label);
    }
    grid.appendChild(card);
  }
}

// ── Story List ──────────────────────────────────────────
async function selectLevel(key, label) {
  currentLevel = key;
  $('stories-heading').textContent = key + ' — ' + label;
  showScreen('screen-stories');

  const res = await fetch('/api/stories?level=' + key);
  const data = await res.json();
  const list = $('story-list');
  list.innerHTML = '';

  if (data.stories.length === 0) {
    list.innerHTML = '<p style="color:#8b949e;text-align:center;padding:20px">Nenhuma história ainda. Gere uma!</p>';
  }

  for (const st of data.stories) {
    const item = document.createElement('div');
    item.className = 'story-item';
    item.innerHTML = `
      <div class="story-title">${st.title}</div>
      <div class="story-meta">${st.word_count} palavras | ${st.has_audio ? '🔊' : '📝'} | Ouviu ${st.times_played}x</div>
    `;
    item.onclick = () => loadStory(st.id);
    list.appendChild(item);
  }
}

async function generateStory() {
  const btn = $('gen-btn');
  btn.disabled = true;
  btn.textContent = 'Gerando história (~1-2 min)...';

  try {
    const res = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({level: currentLevel}),
    });
    const data = await res.json();
    if (data.id) {
      await selectLevel(currentLevel, currentLevel);
    } else {
      alert(data.error || 'Erro ao gerar história');
    }
  } catch(e) {
    alert('Erro: ' + e.message);
  }

  btn.disabled = false;
  btn.textContent = 'Gerar nova história';
}

// ── Player ──────────────────────────────────────────────
async function loadStory(id) {
  showScreen('screen-player');
  $('player-status').innerHTML = '<span class="loading-spinner pulsing">Carregando áudio...</span>';
  $('chunk-dots').innerHTML = '';
  $('show-text-btn').style.display = 'inline-block';
  $('story-text').classList.remove('visible');
  listenedOnce = false;
  chunkIndex = 0;

  const res = await fetch('/api/story/' + id);
  const data = await res.json();
  currentStory = data;
  questions = data.questions || [];

  storyChunks = data.audio_chunks?.story_chunks || [];
  chunkTexts = data.audio_chunks?.chunk_texts || [];
  questionAudio = data.audio_chunks?.question_audio || [];

  // If no audio yet, generate it
  if (storyChunks.length === 0) {
    $('player-status').innerHTML = '<span class="loading-spinner pulsing">Gerando áudio...</span>';
    const ares = await fetch('/api/story/' + id + '/audio', {method:'POST'});
    const adata = await ares.json();
    storyChunks = adata.audio?.story_chunks || [];
    chunkTexts = adata.audio?.chunk_texts || [];
    questionAudio = adata.audio?.question_audio || [];
  }

  if (storyChunks.length === 0) {
    $('player-status').textContent = 'Erro: sem áudio.';
    return;
  }

  // Build chunk dots
  const dots = $('chunk-dots');
  dots.innerHTML = '';
  for (let i = 0; i < storyChunks.length; i++) {
    const dot = document.createElement('div');
    dot.className = 'chunk-dot' + (i === 0 ? ' current' : '');
    dot.id = 'dot-' + i;
    dots.appendChild(dot);
  }

  // Build karaoke text with chunk-span and word spans
  const storyText = $('story-text');
  storyText.innerHTML = '';
  if (chunkTexts.length > 0) {
    chunkTexts.forEach((text, i) => {
      const span = document.createElement('span');
      span.className = 'chunk-span';
      span.id = 'chunk-text-' + i;
      // Split into words, preserve whitespace
      const words = text.split(/(\s+)/);
      words.forEach(w => {
        if (/^\s+$/.test(w)) {
          span.appendChild(document.createTextNode(w));
        } else if (w) {
          const ws = document.createElement('span');
          ws.className = 'word';
          ws.textContent = w;
          span.appendChild(ws);
        }
      });
      // Add space between chunks
      if (i < chunkTexts.length - 1) {
        span.appendChild(document.createTextNode(' '));
      }
      storyText.appendChild(span);
    });
  } else {
    storyText.textContent = data.body;
  }
  $('player-status').textContent = 'Toque para ouvir';
  $('play-btn').innerHTML = '&#9654;';

  // Record play
  fetch('/api/story/' + id + '/play', {method:'POST'});
}

function togglePlay() {
  if (player.paused && storyChunks.length > 0) {
    playChunk(chunkIndex);
  } else {
    player.pause();
    $('play-btn').innerHTML = '&#9654;';
    $('player-status').textContent = 'Pausado';
  }
}

function playChunk(idx) {
  // Clear any previous highlight timer
  if (highlightTimer) { clearInterval(highlightTimer); highlightTimer = null; }

  if (idx >= storyChunks.length) {
    // Mark last chunk as played
    document.querySelectorAll('.chunk-span').forEach(s => {
      s.classList.remove('active');
      s.classList.add('played');
    });
    listenedOnce = true;
    $('player-status').textContent = 'Fim da história';
    $('play-btn').innerHTML = '&#9654;';

    if (questions.length > 0) {
      setTimeout(() => startQuestions(), 2000);
    }
    return;
  }

  chunkIndex = idx;
  player.src = '/audio/' + storyChunks[idx];
  player.play().catch(() => {
    $('player-status').textContent = 'Toque para continuar';
  });

  $('play-btn').innerHTML = '⏸';
  $('player-status').textContent = (idx + 1) + ' / ' + storyChunks.length;

  // Update dots
  document.querySelectorAll('.chunk-dot').forEach((d, i) => {
    d.className = 'chunk-dot' + (i < idx ? ' played' : '') + (i === idx ? ' current' : '');
  });

  // Update chunk text highlighting
  document.querySelectorAll('.chunk-span').forEach((s, i) => {
    s.classList.remove('active');
    if (i < idx) s.classList.add('played');
  });
  const activeChunk = document.getElementById('chunk-text-' + idx);
  if (activeChunk) {
    activeChunk.classList.add('active');
    activeChunk.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // Progressive word highlighting
    const words = activeChunk.querySelectorAll('.word');
    if (words.length > 0) {
      let wordIdx = 0;
      // Reset word highlights
      words.forEach(w => w.classList.remove('highlight'));

      player.ontimeupdate = () => {
        if (player.duration && words.length > 0) {
          const progress = player.currentTime / player.duration;
          const targetWord = Math.floor(progress * words.length);
          while (wordIdx <= targetWord && wordIdx < words.length) {
            words[wordIdx].classList.add('highlight');
            wordIdx++;
          }
        }
      };
    }
  }

  player.onended = () => {
    // Mark all words in chunk as highlighted
    if (activeChunk) {
      activeChunk.querySelectorAll('.word').forEach(w => w.classList.add('highlight'));
      activeChunk.classList.remove('active');
      activeChunk.classList.add('played');
    }
    $('dot-' + idx).className = 'chunk-dot played';
    player.ontimeupdate = null;
    playChunk(idx + 1);
  };
}

function toggleText() {
  $('story-text').classList.toggle('visible');
  $('show-text-btn').textContent =
    $('story-text').classList.contains('visible') ? 'Esconder texto' : 'Mostrar texto';
}

// ── Questions ───────────────────────────────────────────
function startQuestions() {
  qIndex = 0;
  qCorrect = 0;
  showScreen('screen-questions');
  showQuestion(0);
}

function showQuestion(idx) {
  if (idx >= questions.length) {
    showResults();
    return;
  }

  const q = questions[idx];
  $('q-number').textContent = 'Pergunta ' + (idx+1) + ' de ' + questions.length;
  $('q-text').textContent = q.question;

  // Play question audio if available
  if (questionAudio[idx]) {
    qPlayer.src = '/audio/' + questionAudio[idx];
    qPlayer.play().catch(() => {});
  }

  const opts = $('q-options');
  opts.innerHTML = '';
  q.options.forEach((opt, oi) => {
    const btn = document.createElement('button');
    btn.className = 'q-option';
    btn.textContent = opt;
    btn.onclick = () => answerQuestion(idx, oi, q.correct);
    opts.appendChild(btn);
  });
}

function answerQuestion(qIdx, selected, correct) {
  const btns = $('q-options').querySelectorAll('.q-option');
  btns.forEach((btn, i) => {
    btn.onclick = null;
    if (i === correct) btn.classList.add('correct');
    if (i === selected && selected !== correct) btn.classList.add('wrong');
  });

  if (selected === correct) qCorrect++;

  // Log answer
  fetch('/api/answer', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      story_id: currentStory.id,
      question_index: qIdx,
      selected: selected,
      correct: correct,
      is_correct: selected === correct,
    }),
  });

  setTimeout(() => {
    qIndex++;
    showQuestion(qIndex);
  }, 1500);
}

// ── Results ─────────────────────────────────────────────
function showResults() {
  showScreen('screen-results');
  const pct = Math.round(qCorrect / questions.length * 100);
  const pass = pct >= 75;

  $('result-score').textContent = pct + '%';
  $('result-score').className = 'result-score ' + (pass ? 'pass' : 'fail');
  $('result-label').textContent = pass
    ? 'Massa! Entendeu a história.'
    : 'Oxe, bora ouvir de novo.';

  // Log result
  fetch('/api/story/' + currentStory.id + '/result', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({score: pct, correct: qCorrect, total: questions.length}),
  });
}

// ── Init ────────────────────────────────────────────────
loadLevels();
</script>
</body></html>"""


class StoryHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/stories":
            self._html(STORY_HTML)
        elif path == "/api/levels":
            self._get_levels()
        elif path == "/api/stories":
            level = query.get("level", ["A1"])[0]
            self._get_stories(level)
        elif path.startswith("/api/story/") and path.count("/") == 3:
            story_id = int(path.split("/")[3])
            self._get_story(story_id)
        elif path.startswith("/audio/"):
            self._serve_audio(path[7:])
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/api/generate":
            self._generate(body)
        elif path.endswith("/audio") and path.startswith("/api/story/"):
            story_id = int(path.split("/")[3])
            self._gen_audio(story_id)
        elif path.endswith("/play") and path.startswith("/api/story/"):
            story_id = int(path.split("/")[3])
            self._record_play(story_id)
        elif path == "/api/answer":
            self._log_answer(body)
        elif path.endswith("/result") and path.startswith("/api/story/"):
            story_id = int(path.split("/")[3])
            self._save_result(story_id, body)
        else:
            self.send_error(404)

    # ── API ───────────────────────────────────────────────

    def _get_levels(self):
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
                scores.extend(json.loads(sr["comprehension_scores"]))
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
        self._json({"levels": levels, "current_tier": tier})

    def _get_stories(self, level):
        conn = get_conn()
        rows = conn.execute(
            """SELECT id, title, word_count, times_played, audio_chunks
               FROM story_library WHERE level = ? ORDER BY id DESC""",
            (level,),
        ).fetchall()
        conn.close()

        stories = []
        for r in rows:
            has_audio = bool(r["audio_chunks"])
            stories.append({
                "id": r["id"],
                "title": r["title"],
                "word_count": r["word_count"],
                "times_played": r["times_played"],
                "has_audio": has_audio,
            })

        self._json({"stories": stories})

    def _get_story(self, story_id):
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
            "id": row["id"],
            "level": row["level"],
            "title": row["title"],
            "body": row["body"],
            "word_count": row["word_count"],
            "questions": json.loads(row["questions"]),
            "audio_chunks": audio_chunks,
            "times_played": row["times_played"],
            "setting": row["setting"],
            "theme": row["theme"],
        })

    def _generate(self, body):
        level = body.get("level", "A1")
        if level not in LEVELS:
            self._json({"error": f"Unknown level: {level}"})
            return

        story_id = generate_story(level)
        if not story_id:
            self._json({"error": "Generation failed"})
            return

        audio = generate_story_audio(story_id)
        self._json({"id": story_id, "audio": audio})

    def _gen_audio(self, story_id):
        audio = generate_story_audio(story_id)
        if audio:
            self._json({"audio": audio})
        else:
            self._json({"error": "Audio generation failed"})

    def _record_play(self, story_id):
        conn = get_conn()
        conn.execute(
            "UPDATE story_library SET times_played = times_played + 1, last_played = ? WHERE id = ?",
            (datetime.now().isoformat(), story_id),
        )
        conn.commit()
        conn.close()
        self._json({"ok": True})

    def _log_answer(self, body):
        log_file = LOG_DIR / f"stories_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        body["timestamp"] = datetime.now().isoformat()
        body["type"] = "comprehension_answer"
        with open(log_file, "a") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
        self._json({"ok": True})

    def _save_result(self, story_id, body):
        score = body.get("score", 0)
        conn = get_conn()
        row = conn.execute(
            "SELECT comprehension_scores FROM story_library WHERE id = ?", (story_id,)
        ).fetchone()
        if row:
            scores = json.loads(row["comprehension_scores"])
            scores.append(score)
            conn.execute(
                "UPDATE story_library SET comprehension_scores = ? WHERE id = ?",
                (json.dumps(scores), story_id),
            )
            conn.commit()
        conn.close()

        # Log
        log_file = LOG_DIR / f"stories_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "story_result",
            "story_id": story_id,
            "score": score,
        }
        with open(log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._json({"ok": True})

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

    # ── Helpers ───────────────────────────────────────────

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


def main():
    port = 8888
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    init_story_db()

    ip = get_local_ip()
    server = http.server.HTTPServer(("0.0.0.0", port), StoryHandler)

    tier = get_unlocked_tier()
    conn = get_conn()
    story_count = conn.execute("SELECT COUNT(*) as c FROM story_library").fetchone()["c"]
    conn.close()

    print(f"\n  Oxe Protocol — Story Server")
    print(f"  {'='*44}")
    print(f"  Phone:    http://{ip}:{port}")
    print(f"  Local:    http://localhost:{port}")
    print(f"  Tier:     {tier} ({TIER_LABELS[tier]})")
    print(f"  Stories:  {story_count}")
    print(f"  {'='*44}")
    print(f"  Open on your phone to start listening.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
