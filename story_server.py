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
<title>Oxe Protocol — Biblioteca</title>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  @keyframes fadeUp { from{opacity:0;transform:translateY(12px)} to{opacity:1;transform:translateY(0)} }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; -webkit-user-select: none; user-select: none;
    padding-bottom: 76px;
  }
  .header {
    padding: 14px 20px; background: rgba(255,255,255,0.03);
    border-bottom: 2px solid transparent;
    border-image: linear-gradient(90deg, #3B82F6, #7C5CFC) 1;
    display: flex; justify-content: space-between; align-items: center;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    position: sticky; top: 0; z-index: 10;
  }
  .header h1 {
    font-size: 1.05em; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .back-btn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
    color: #9ca3af; padding: 6px 14px; border-radius: 10px; font-size: 0.85em;
    cursor: pointer; display: none; backdrop-filter: blur(10px); transition: all 0.2s;
  }
  .back-btn:active { background: rgba(255,255,255,0.1); }

  /* ── Sub-tabs ── */
  .sub-tabs {
    display: flex; gap: 0; border-bottom: 1px solid rgba(255,255,255,0.06);
    position: sticky; top: 52px; z-index: 9;
    background: rgba(10,10,11,0.95); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .sub-tab {
    flex: 1; padding: 14px 0; text-align: center; font-size: 0.8em; font-weight: 700;
    color: #525263; cursor: pointer; border-bottom: 2px solid transparent;
    transition: all 0.2s; background: none; border-top: none; border-left: none; border-right: none;
    -webkit-tap-highlight-color: transparent;
  }
  .sub-tab.active { color: #60a5fa; border-bottom-color: #3B82F6; }

  /* ── Sub-tab content panels ── */
  .tab-panel { display: none; padding: 20px; animation: fadeUp 0.4s ease-out; }
  .tab-panel.active { display: block; }

  .screen { display: none; padding: 20px; animation: fadeUp 0.4s ease-out; }
  .screen.active { display: block; }

  /* Level Select */
  .level-grid { display: flex; flex-direction: column; gap: 14px; margin-top: 14px; }
  .level-card {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px; padding: 20px 22px; cursor: pointer; transition: all 0.2s;
    -webkit-tap-highlight-color: transparent;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
    border-left: 3px solid transparent;
  }
  .level-card:not(.locked) { border-left: 3px solid #3B82F6; }
  .level-card:active { transform: scale(0.98); border-color: rgba(59,130,246,0.4); border-left-color: #7C5CFC; }
  .level-card.locked { opacity: 0.25; pointer-events: none; }
  .level-card .level-tag {
    font-size: 0.75em; font-weight: 700; color: #60a5fa;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .level-card .level-name { font-size: 1.15em; font-weight: 600; margin: 6px 0 2px; color: #fafafa; }
  .level-card .level-desc { font-size: 0.8em; color: #525263; }
  .level-card .level-stats { font-size: 0.75em; color: #444; margin-top: 8px; }
  .lock-icon { color: #333; }

  /* Story List */
  .story-list { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
  .story-item {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px; padding: 16px 18px; cursor: pointer; transition: all 0.2s;
    backdrop-filter: blur(20px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
  }
  .story-item:active { border-color: rgba(59,130,246,0.4); transform: scale(0.99); }
  .story-title { font-size: 1em; font-weight: 600; color: #fafafa; }
  .story-meta { font-size: 0.75em; color: #525263; margin-top: 4px; }
  .gen-btn {
    width: 100%; padding: 16px; margin-top: 18px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    border: none; border-radius: 16px; font-size: 1em; font-weight: 700;
    cursor: pointer; transition: all 0.2s;
    box-shadow: 0 4px 16px rgba(59,130,246,0.25);
  }
  .gen-btn:active { transform: scale(0.98); opacity: 0.9; }
  .gen-btn:disabled { background: rgba(255,255,255,0.04); color: #333; box-shadow: none; }

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
  .chunk-dot.current { background: #60a5fa; transform: scale(1.4); }
  .player-status { font-size: 1.1em; text-align: center; min-height: 1.5em; color: #9ca3af; }
  .player-btn {
    width: 80px; height: 80px; border-radius: 50%; border: none;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    font-size: 2em; cursor: pointer; display: flex; align-items: center;
    justify-content: center; transition: all 0.2s;
    box-shadow: 0 0 30px rgba(59,130,246,0.25), 0 0 0 1px rgba(255,255,255,0.05);
  }
  .player-btn:active { transform: scale(0.95); }
  .show-text-btn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
    color: #6b7280; padding: 8px 18px; border-radius: 20px; font-size: 0.8em;
    cursor: pointer; transition: all 0.2s;
  }
  .show-text-btn:active { border-color: rgba(59,130,246,0.3); background: rgba(255,255,255,0.08); }
  .speed-controls {
    display: flex; gap: 8px; margin-top: 8px;
  }
  .speed-btn {
    padding: 6px 16px; border-radius: 20px; font-size: 0.8em; font-weight: 600;
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    color: #525263; cursor: pointer; transition: all 0.2s;
  }
  .speed-btn.active {
    background: linear-gradient(135deg, rgba(59,130,246,0.15), rgba(124,92,252,0.15));
    border-color: rgba(59,130,246,0.3);
    color: #60a5fa;
  }
  .story-text {
    display: none; background: rgba(255,255,255,0.03); border-radius: 20px;
    padding: 18px; font-size: 1em; line-height: 1.8; max-height: 50vh;
    overflow-y: auto; border: 1px solid rgba(255,255,255,0.06); width: 100%;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
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
    color: #60a5fa; background: rgba(96,165,250,0.12);
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
    border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; color: #fafafa;
    font-size: 0.95em; cursor: pointer; text-align: left; transition: all 0.2s;
    backdrop-filter: blur(10px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
  }
  .q-option:active { border-color: rgba(59,130,246,0.4); }
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
    padding: 16px 32px; border: none; border-radius: 16px;
    font-size: 1.1em; font-weight: 700; cursor: pointer;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    transition: all 0.2s;
    box-shadow: 0 4px 16px rgba(59,130,246,0.25);
  }
  .result-btn:active { transform: scale(0.97); }

  .loading-spinner { color: #525263; font-size: 1.1em; }
  .pulsing { animation: pulse 1.5s infinite; }

  /* ── Podcast styles ── */
  .podcast-list { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
  .podcast-item {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px; padding: 16px 18px; cursor: pointer; transition: all 0.2s;
    backdrop-filter: blur(20px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
    display: flex; justify-content: space-between; align-items: center;
  }
  .podcast-item:active { border-color: rgba(59,130,246,0.4); transform: scale(0.99); }
  .podcast-info { flex: 1; }
  .podcast-title { font-size: 1em; font-weight: 600; color: #fafafa; }
  .podcast-meta { font-size: 0.75em; color: #525263; margin-top: 4px; }
  .diff-badge {
    display: inline-block; padding: 3px 10px; border-radius: 10px; font-size: 0.65em;
    font-weight: 700; margin-left: 6px;
  }
  .diff-badge.easy { background: rgba(52,211,153,0.15); color: #34d399; }
  .diff-badge.medium { background: rgba(59,130,246,0.15); color: #60a5fa; }
  .diff-badge.hard { background: rgba(248,113,113,0.15); color: #f87171; }
  .podcast-play-icon {
    width: 44px; height: 44px; border-radius: 50%; flex-shrink: 0;
    background: linear-gradient(135deg, rgba(59,130,246,0.15), rgba(124,92,252,0.15));
    border: 1px solid rgba(59,130,246,0.2);
    display: flex; align-items: center; justify-content: center;
    color: #60a5fa; font-size: 1.2em;
  }

  /* ── Review feed styles ── */
  .review-list { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
  .review-item {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px; padding: 16px 18px; transition: all 0.2s;
    backdrop-filter: blur(20px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
  }
  .review-word { font-size: 1.05em; font-weight: 700; color: #60a5fa; }
  .review-chunk { font-size: 0.9em; color: #9ca3af; margin-top: 4px; font-style: italic; }
  .review-meta { font-size: 0.7em; color: #525263; margin-top: 6px; display: flex; gap: 10px; }
  .source-badge {
    display: inline-block; padding: 2px 8px; border-radius: 8px; font-size: 0.7em; font-weight: 600;
  }
  .source-badge.corpus { background: rgba(59,130,246,0.1); color: #60a5fa; }
  .source-badge.dictionary { background: rgba(124,92,252,0.1); color: #a78bfa; }
  .source-badge.story { background: rgba(52,211,153,0.1); color: #34d399; }
  .source-badge.podcast { background: rgba(248,113,113,0.1); color: #f87171; }
  .empty-msg { color: #525263; text-align: center; padding: 40px 20px; font-size: 0.95em; }
</style>
</head><body>

<div class="header">
  <h1>BIBLIOTECA</h1>
  <button class="back-btn" id="back-btn" onclick="goBack()">Voltar</button>
</div>

<!-- Sub-tabs: Historias | Podcasts | Revisao -->
<div class="sub-tabs" id="sub-tabs">
  <button class="sub-tab active" onclick="switchTab('historias')">Historias</button>
  <button class="sub-tab" onclick="switchTab('podcasts')">Podcasts</button>
  <button class="sub-tab" onclick="switchTab('revisao')">Revisao</button>
</div>

<!-- ═══ TAB PANEL: Historias ═══ -->
<div class="tab-panel active" id="panel-historias">
  <!-- Screen 1: Level Select -->
  <div class="screen active" id="screen-levels">
    <div class="level-grid" id="level-grid"></div>
  </div>

  <!-- Screen 2: Story List -->
  <div class="screen" id="screen-stories">
    <h2 id="stories-heading" style="font-size:1.1em;color:#60a5fa"></h2>
    <div class="story-list" id="story-list"></div>
    <button class="gen-btn" id="gen-btn" onclick="generateStory()">Gerar nova historia</button>
  </div>

  <!-- Screen 3: Player -->
  <div class="screen" id="screen-player">
    <div class="player-wrap">
      <div class="chunk-dots" id="chunk-dots"></div>
      <div class="player-status" id="player-status">Preparando...</div>
      <button class="player-btn" id="play-btn" onclick="togglePlay()">&#9654;</button>
      <button class="show-text-btn" id="show-text-btn" onclick="toggleText()">Mostrar texto</button>
      <div class="speed-controls">
        <button class="speed-btn" onclick="setSpeed(0.85)">0.85x</button>
        <button class="speed-btn active" onclick="setSpeed(1.0)">1.0x</button>
        <button class="speed-btn" onclick="setSpeed(1.25)">1.25x</button>
        <button class="speed-btn" onclick="setSpeed(1.5)">1.5x</button>
      </div>
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
</div>

<!-- ═══ TAB PANEL: Podcasts ═══ -->
<div class="tab-panel" id="panel-podcasts">
  <div class="podcast-list" id="podcast-list"></div>
  <button class="gen-btn" id="podcast-gen-btn" onclick="generatePodcast()">Gerar novo podcast</button>

  <!-- Podcast detail/reader screen (hidden by default) -->
  <div class="screen" id="screen-podcast-detail" style="display:none">
    <h2 id="podcast-detail-title" style="font-size:1.15em;color:#60a5fa;margin-bottom:12px"></h2>
    <div id="podcast-detail-meta" style="font-size:0.8em;color:#525263;margin-bottom:16px"></div>
    <div id="podcast-segments" style="line-height:1.8;color:#c0c0ca;font-size:0.95em"></div>
  </div>
</div>

<!-- ═══ TAB PANEL: Revisao ═══ -->
<div class="tab-panel" id="panel-revisao">
  <div class="review-list" id="review-list"></div>
</div>

<audio id="audio-player" preload="auto"></audio>

<script>
const $ = id => document.getElementById(id);
const player = $('audio-player');
const qPlayer = $('q-player');

// ── Sub-tab switching (no page reload) ──────────────────
let activeTab = 'historias';
function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.sub-tab').forEach((t, i) => {
    t.classList.toggle('active', ['historias','podcasts','revisao'][i] === tab);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  $('panel-' + tab).classList.add('active');
  // Hide back btn and sub-tabs detail
  $('back-btn').style.display = 'none';
  $('sub-tabs').style.display = 'flex';

  if (tab === 'historias') { loadLevels(); resetStoryScreens(); }
  if (tab === 'podcasts') { loadPodcasts(); }
  if (tab === 'revisao') { loadReviewFeed(); }
}

function resetStoryScreens() {
  document.querySelectorAll('#panel-historias .screen').forEach(s => s.classList.remove('active'));
  $('screen-levels').classList.add('active');
}

let currentSpeed = 1.0;
function setSpeed(speed) {
  currentSpeed = speed;
  player.playbackRate = speed;
  document.querySelectorAll('.speed-btn').forEach(b => {
    b.classList.toggle('active', parseFloat(b.textContent) === speed);
  });
}

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
  document.querySelectorAll('#panel-historias .screen').forEach(s => s.classList.remove('active'));
  $(id).classList.add('active');
  const isDeep = (id !== 'screen-levels');
  $('back-btn').style.display = isDeep ? 'block' : 'none';
  if (isDeep) $('sub-tabs').style.display = 'none';
  else $('sub-tabs').style.display = 'flex';
}

function goBack() {
  if (activeTab === 'podcasts') {
    // Back from podcast detail
    $('screen-podcast-detail').style.display = 'none';
    $('podcast-list').style.display = '';
    $('podcast-gen-btn').style.display = '';
    $('back-btn').style.display = 'none';
    $('sub-tabs').style.display = 'flex';
    return;
  }
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
        ${locked ? '<span class="lock-icon">Bloqueado</span>' :
          lv.story_count + ' historias | ~10 min cada | ' + (lv.avg_score !== null ? lv.avg_score + '% media' : 'sem pontuacao')}
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
    list.innerHTML = '<p class="empty-msg">Nenhuma historia ainda. Gere uma!</p>';
  }

  for (const st of data.stories) {
    const item = document.createElement('div');
    item.className = 'story-item';
    item.innerHTML = `
      <div class="story-title">${st.title}</div>
      <div class="story-meta">${st.word_count} palavras | Ouviu ${st.times_played}x</div>
    `;
    item.onclick = () => loadStory(st.id);
    list.appendChild(item);
  }
}

async function generateStory() {
  const btn = $('gen-btn');
  btn.disabled = true;
  btn.textContent = 'Gerando historia (~1-2 min)...';

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
      alert(data.error || 'Erro ao gerar historia');
    }
  } catch(e) {
    alert('Erro: ' + e.message);
  }

  btn.disabled = false;
  btn.textContent = 'Gerar nova historia';
}

// ── Player ──────────────────────────────────────────────
async function loadStory(id) {
  showScreen('screen-player');
  $('player-status').innerHTML = '<span class="loading-spinner pulsing">Carregando audio...</span>';
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
    $('player-status').innerHTML = '<span class="loading-spinner pulsing">Gerando audio...</span>';
    const ares = await fetch('/api/story/' + id + '/audio', {method:'POST'});
    const adata = await ares.json();
    storyChunks = adata.audio?.story_chunks || [];
    chunkTexts = adata.audio?.chunk_texts || [];
    questionAudio = adata.audio?.question_audio || [];
  }

  if (storyChunks.length === 0) {
    $('player-status').textContent = 'Erro: sem audio.';
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
  if (highlightTimer) { clearInterval(highlightTimer); highlightTimer = null; }

  if (idx >= storyChunks.length) {
    document.querySelectorAll('.chunk-span').forEach(s => {
      s.classList.remove('active');
      s.classList.add('played');
    });
    listenedOnce = true;
    $('player-status').textContent = 'Fim da historia';
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
  player.playbackRate = currentSpeed;

  $('play-btn').innerHTML = '&#x23F8;';
  $('player-status').textContent = (idx + 1) + ' / ' + storyChunks.length;

  document.querySelectorAll('.chunk-dot').forEach((d, i) => {
    d.className = 'chunk-dot' + (i < idx ? ' played' : '') + (i === idx ? ' current' : '');
  });

  document.querySelectorAll('.chunk-span').forEach((s, i) => {
    s.classList.remove('active');
    if (i < idx) s.classList.add('played');
  });
  const activeChunk = document.getElementById('chunk-text-' + idx);
  if (activeChunk) {
    activeChunk.classList.add('active');
    activeChunk.scrollIntoView({ behavior: 'smooth', block: 'center' });

    const words = activeChunk.querySelectorAll('.word');
    if (words.length > 0) {
      let wordIdx = 0;
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
    ? 'Massa! Entendeu a historia.'
    : 'Oxe, bora ouvir de novo.';

  if (pass && currentSpeed < 1.5) {
    const suggestion = document.createElement('div');
    suggestion.style.cssText = 'font-size:0.9em;color:#60a5fa;margin-top:8px;';
    suggestion.textContent = 'Tenta mais rapido?';
    $('result-label').parentNode.insertBefore(suggestion, $('result-label').nextSibling);
  }

  fetch('/api/story/' + currentStory.id + '/result', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({score: pct, correct: qCorrect, total: questions.length}),
  });
}

// ── Podcasts ────────────────────────────────────────────
async function loadPodcasts() {
  const list = $('podcast-list');
  list.innerHTML = '<p class="empty-msg pulsing">Carregando...</p>';

  try {
    const res = await fetch('/api/library/podcasts');
    const data = await res.json();

    list.innerHTML = '';
    if (!data.length) {
      list.innerHTML = '<p class="empty-msg">Nenhum podcast ainda. Gere o primeiro!</p>';
      return;
    }

    for (const p of data) {
      const diffClass = p.difficulty <= 60 ? 'easy' : p.difficulty <= 80 ? 'medium' : 'hard';
      const diffLabel = p.difficulty <= 60 ? 'Iniciante' : p.difficulty <= 80 ? 'Intermediario' : 'Avancado';
      const item = document.createElement('div');
      item.className = 'podcast-item';
      item.innerHTML = `
        <div class="podcast-info">
          <div class="podcast-title">${p.title} <span class="diff-badge ${diffClass}">${diffLabel}</span></div>
          <div class="podcast-meta">${p.word_count} palavras | ${p.total_segments} segmentos | Ouviu ${p.times_played}x</div>
        </div>
        <div class="podcast-play-icon">&#9654;</div>
      `;
      item.onclick = () => openPodcast(p.id);
      list.appendChild(item);
    }
  } catch(e) {
    list.innerHTML = '<p class="empty-msg">Erro ao carregar podcasts.</p>';
  }
}

async function openPodcast(id) {
  $('podcast-list').style.display = 'none';
  $('podcast-gen-btn').style.display = 'none';
  $('screen-podcast-detail').style.display = 'block';
  $('back-btn').style.display = 'block';
  $('sub-tabs').style.display = 'none';

  $('podcast-detail-title').textContent = 'Carregando...';
  $('podcast-detail-meta').textContent = '';
  $('podcast-segments').innerHTML = '';

  try {
    const res = await fetch('/api/library/podcast/' + id);
    const data = await res.json();

    const diffLabel = data.difficulty <= 60 ? 'Iniciante' : data.difficulty <= 80 ? 'Intermediario' : 'Avancado';
    $('podcast-detail-title').textContent = data.title;
    $('podcast-detail-meta').textContent = diffLabel + ' | ' + data.word_count + ' palavras | ' + (data.segments?.length || 0) + ' segmentos';

    const segs = $('podcast-segments');
    segs.innerHTML = '';
    (data.segments || []).forEach((seg, i) => {
      const div = document.createElement('div');
      div.style.cssText = 'margin-bottom:24px;';
      div.innerHTML = '<div style="font-size:0.7em;color:#60a5fa;font-weight:700;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Segmento ' + (i+1) + '</div>' +
        '<div style="white-space:pre-wrap">' + (seg.text || '') + '</div>';
      segs.appendChild(div);
    });
  } catch(e) {
    $('podcast-detail-title').textContent = 'Erro ao carregar podcast';
  }
}

async function generatePodcast() {
  const btn = $('podcast-gen-btn');
  btn.disabled = true;
  btn.textContent = 'Gerando podcast (~2-3 min)...';

  try {
    const res = await fetch('/api/library/podcast/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({difficulty: 80, focus_words: []}),
    });
    const data = await res.json();
    if (data.id) {
      await loadPodcasts();
    } else {
      alert(data.error || 'Erro ao gerar podcast');
    }
  } catch(e) {
    alert('Erro: ' + e.message);
  }

  btn.disabled = false;
  btn.textContent = 'Gerar novo podcast';
}

// ── Review Feed ─────────────────────────────────────────
async function loadReviewFeed() {
  const list = $('review-list');
  list.innerHTML = '<p class="empty-msg pulsing">Carregando...</p>';

  try {
    const res = await fetch('/api/library/review-feed');
    const data = await res.json();
    const chunks = data.chunks || [];

    list.innerHTML = '';
    if (!chunks.length) {
      list.innerHTML = '<p class="empty-msg">Nenhum chunk pra revisar agora. Bora treinar!</p>';
      return;
    }

    for (const c of chunks) {
      const item = document.createElement('div');
      item.className = 'review-item';
      const srcClass = c.source || 'corpus';
      const srcLabel = {corpus:'Corpus',dictionary:'Dicionario',story:'Historia',podcast:'Podcast'}[srcClass] || srcClass;
      item.innerHTML = `
        <div class="review-word">${c.word}</div>
        <div class="review-chunk">"${c.target_chunk}"</div>
        <div class="review-meta">
          <span class="source-badge ${srcClass}">${srcLabel}</span>
          <span>Passo ${c.current_pass}/5</span>
          <span>Dominio ${c.mastery_level}</span>
        </div>
      `;
      list.appendChild(item);
    }
  } catch(e) {
    list.innerHTML = '<p class="empty-msg">Erro ao carregar revisao.</p>';
  }
}

// ── Init ────────────────────────────────────────────────
loadLevels();
</script>

<!-- Bottom Tab Bar -->
<style>
  .tab-bar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 100;
    display: flex; justify-content: space-around; align-items: center;
    height: 72px; padding-bottom: env(safe-area-inset-bottom, 0);
    background: rgba(10,10,11,0.94); border-top: 1px solid rgba(255,255,255,0.06);
    backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
  }
  .tab {
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    text-decoration: none; color: #525263; font-size: 0.62em; font-weight: 500;
    -webkit-tap-highlight-color: transparent; padding: 6px 10px; transition: color 0.15s;
  }
  .tab.active { color: #60a5fa; }
  .tab svg { width: 22px; height: 22px; fill: currentColor; }
</style>
<nav class="tab-bar">
  <a href="/" class="tab">
    <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
    <span>Inicio</span>
  </a>
  <a href="/search" class="tab">
    <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
    <span>Buscar</span>
  </a>
  <a href="/drill" class="tab">
    <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
    <span>Treinar</span>
  </a>
  <a href="/library" class="tab active">
    <svg viewBox="0 0 24 24"><path d="M21 5c-1.11-.35-2.33-.5-3.5-.5-1.95 0-4.05.4-5.5 1.5-1.45-1.1-3.55-1.5-5.5-1.5S2.45 4.9 1 6v14.65c0 .25.25.5.5.5.1 0 .15-.05.25-.05C3.1 20.45 5.05 20 6.5 20c1.95 0 4.05.4 5.5 1.5 1.35-.85 3.8-1.5 5.5-1.5 1.65 0 3.35.3 4.75 1.05.1.05.15.05.25.05.25 0 .5-.25.5-.5V6c-.6-.45-1.25-.75-2-1z"/></svg>
    <span>Biblioteca</span>
  </a>
  <a href="/conversa" class="tab">
    <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
    <span>Conversa</span>
  </a>
</nav>

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
