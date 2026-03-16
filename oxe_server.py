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
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fsrs import Rating

from srs_engine import (
    get_next_word, get_due_words, record_review,
    get_unlocked_tier, tier_progress, TIER_LABELS, DB_PATH,
    migrate_db, get_daily_stats, get_streak, get_weak_words,
)
from story_gen import LEVELS, init_story_db, generate_story, generate_story_audio

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
LOG_DIR = Path(__file__).parent / "voca_vault" / "logs"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

LATENCY_THRESHOLD_MS = 1000
TRAP_PROBABILITY = 0.15
TRAP_LATENCY_MS = 800
CLOZE_PROBABILITY = 0.30

# ── Imports from drill_server ──────────────────────────────────
from drill_server import (
    DRILL_HTML, build_carrier, generate_tts, generate_image, generate_explanation,
    prefetch_images, log_drill, TRAP_SENTENCES, TRAP_REACTIONS, IMAGE_DIR,
    build_cloze, score_pronunciation,
)

# ── Imports from story_server ──────────────────────────────────
from story_server import STORY_HTML

# Session state
_laranjada_remaining = 0

# Conversation history for Conversa mode
_conversa_history = []


# ── Levenshtein edit distance ──────────────────────────────────
def _levenshtein(a, b):
    if len(a) < len(b):
        return _levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


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

HOME_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Oxe</title>
<style>
  @keyframes fadeIn { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
  @keyframes ringPulse { 0%,100%{filter:drop-shadow(0 0 6px rgba(79,123,239,0.3))} 50%{filter:drop-shadow(0 0 12px rgba(79,123,239,0.5))} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column;
    -webkit-user-select: none; user-select: none;
    padding-bottom: 76px;
  }

  /* ── Top Bar ── */
  .topbar {
    padding: 16px 20px 14px; display: flex; justify-content: space-between; align-items: center;
    position: sticky; top: 0; z-index: 10; background: #0a0a0b;
    border-bottom: 2px solid transparent;
    border-image: linear-gradient(90deg, #3B82F6, #7C5CFC) 1;
  }
  .topbar-brand {
    font-size: 1.5em; font-weight: 800; letter-spacing: -1px;
    background: linear-gradient(135deg, #4F7BEF, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .streak-pill {
    display: flex; align-items: center; gap: 5px; padding: 6px 14px;
    background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.15);
    border-radius: 20px; font-size: 0.8em; font-weight: 600; color: #60a5fa;
  }

  /* ── Scroll content ── */
  .page { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 0 20px 24px; }

  /* ── Progress Card ── */
  .progress-card {
    background: linear-gradient(135deg, rgba(59,130,246,0.10), rgba(124,92,252,0.06));
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
    border-radius: 20px;
    padding: 28px; margin-bottom: 32px; animation: fadeIn 0.4s ease-out;
    transition: transform 0.2s, box-shadow 0.2s;
  }
  .progress-row { display: flex; justify-content: space-between; align-items: center; }
  .progress-left { display: flex; flex-direction: column; gap: 4px; }
  .progress-left h2 { font-size: 1.2em; font-weight: 700; color: #fafafa; }
  .progress-tier-name { font-size: 0.85em; color: #60a5fa; font-weight: 600; }
  .progress-due { font-size: 0.78em; color: #7a7a8e; margin-top: 2px; }
  .progress-ring { position: relative; width: 80px; height: 80px; }
  .progress-ring svg { transform: rotate(-90deg); animation: ringPulse 3s ease-in-out infinite; }
  .progress-ring .bg { fill: none; stroke: rgba(255,255,255,0.06); stroke-width: 5; }
  .progress-ring .fg { fill: none; stroke: url(#grad); stroke-width: 5; stroke-linecap: round;
    transition: stroke-dashoffset 0.8s ease; }
  .progress-tier-num {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
  }
  .progress-tier-num .tier-big { font-size: 1.4em; font-weight: 800; color: #fafafa; line-height: 1; }
  .progress-tier-num .tier-sub { font-size: 0.55em; color: #7a7a8e; font-weight: 500; margin-top: 1px; }
  .stats-row {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; margin-top: 22px;
    border-top: 1px solid rgba(255,255,255,0.06); padding-top: 18px;
  }
  .sstat { text-align: center; }
  .sstat-val { font-size: 1.3em; font-weight: 700; font-variant-numeric: tabular-nums; color: #fafafa; }
  .sstat-lbl { font-size: 0.6em; color: #525263; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 3px; }

  /* ── Palavra do Dia ── */
  .wod-card {
    background: rgba(255,255,255,0.03);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
    border-radius: 20px; padding: 24px 28px; margin-bottom: 32px;
    animation: fadeIn 0.4s ease-out 0.08s both;
    border-top: 2px solid #7C5CFC;
    transition: transform 0.2s, box-shadow 0.2s;
  }
  .wod-header {
    font-size: 0.7em; font-weight: 600; color: #7C5CFC; text-transform: uppercase;
    letter-spacing: 1.5px; margin-bottom: 14px; display: flex; align-items: center; gap: 6px;
  }
  .wod-header::before { content: ''; display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #7C5CFC; }
  .wod-word {
    font-size: 1.6em; font-weight: 800; color: #fafafa; letter-spacing: -0.5px;
    margin-bottom: 8px;
  }
  .wod-sentence {
    font-size: 0.88em; color: #7a7a8e; line-height: 1.6; font-style: italic;
  }

  /* ── Section Headers ── */
  .section-hdr {
    font-size: 0.78em; font-weight: 700; color: #60a5fa; text-transform: uppercase;
    letter-spacing: 1.5px; margin-bottom: 16px; padding-left: 4px;
  }

  /* ── Feature Grid ── */
  .feature-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 32px; }
  .fcard {
    background: rgba(255,255,255,0.03);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
    border-radius: 20px; padding: 22px 18px; text-decoration: none; color: inherit;
    -webkit-tap-highlight-color: transparent;
    transition: transform 0.2s, box-shadow 0.2s;
    display: flex; flex-direction: column; gap: 12px;
    position: relative; overflow: hidden;
  }
  .fcard::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  }
  .fcard.blue-edge::before { background: linear-gradient(90deg, #3B82F6, #60a5fa); }
  .fcard.purple-edge::before { background: linear-gradient(90deg, #7C5CFC, #a78bfa); }
  .fcard.red-edge::before { background: linear-gradient(90deg, #f87171, #fca5a5); }
  .fcard.cyan-edge::before { background: linear-gradient(90deg, #22d3ee, #67e8f9); }
  .fcard:active { transform: scale(0.97); box-shadow: 0 0 0 1px rgba(255,255,255,0.08), 0 2px 6px rgba(0,0,0,0.4); }
  .fcard-icon {
    width: 48px; height: 48px; border-radius: 14px;
    display: flex; align-items: center; justify-content: center; font-size: 1.3em;
  }
  .fcard-icon.blue { background: rgba(59,130,246,0.12); }
  .fcard-icon.purple { background: rgba(124,92,252,0.12); }
  .fcard-icon.red { background: rgba(248,113,113,0.10); }
  .fcard-icon.cyan { background: rgba(34,211,238,0.10); }
  .fcard-title { font-size: 1em; font-weight: 700; }
  .fcard-desc { font-size: 0.72em; color: #7a7a8e; line-height: 1.55; }
  .fcard-badge {
    display: inline-block; padding: 3px 10px; border-radius: 10px; font-size: 0.65em;
    font-weight: 600; background: rgba(59,130,246,0.10); color: #60a5fa;
    align-self: flex-start;
  }
  .fcard-badge.red { background: rgba(248,113,113,0.10); color: #f87171; }

  /* ── Today Stats ── */
  .today-card {
    background: rgba(255,255,255,0.03);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
    border-radius: 20px; padding: 20px 0; margin-bottom: 32px;
    display: flex; align-items: center;
    animation: fadeIn 0.4s ease-out 0.15s both;
    transition: transform 0.2s, box-shadow 0.2s;
  }
  .today-stat {
    flex: 1; text-align: center; position: relative;
  }
  .today-stat + .today-stat::before {
    content: ''; position: absolute; left: 0; top: 50%; transform: translateY(-50%);
    width: 1px; height: 36px; background: rgba(255,255,255,0.06);
  }
  .today-val { font-size: 1.6em; font-weight: 700; font-variant-numeric: tabular-nums; color: #fafafa; }
  .today-lbl { font-size: 0.6em; color: #525263; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }

  /* ── Bottom Tab Bar ── */
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
    -webkit-tap-highlight-color: transparent; padding: 6px 12px;
    transition: color 0.2s;
  }
  .tab.active { color: #60a5fa; }
  .tab svg { width: 22px; height: 22px; fill: currentColor; }

  /* ── Gradient def ── */
  .hidden-svg { position: absolute; width: 0; height: 0; }
</style>
</head><body>

<!-- SVG gradient definition -->
<svg class="hidden-svg"><defs>
  <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
    <stop offset="0%" stop-color="#3B82F6"/>
    <stop offset="100%" stop-color="#7C5CFC"/>
  </linearGradient>
</defs></svg>

<div class="topbar">
  <div class="topbar-brand">Oxe</div>
  <div class="streak-pill" id="streak-pill">
    <span>&#x1f525;</span> <span id="streak">0</span> dias
  </div>
</div>

<div class="page">

  <!-- Progress Card -->
  <div class="progress-card">
    <div class="progress-row">
      <div class="progress-left">
        <h2 id="tier-label">Tier 1</h2>
        <div class="progress-tier-name" id="tier-name">Survival</div>
        <div class="progress-due"><span id="due">0</span> palavras pra revisar</div>
      </div>
      <div class="progress-ring">
        <svg width="80" height="80" viewBox="0 0 80 80">
          <circle class="bg" cx="40" cy="40" r="34"/>
          <circle class="fg" id="ring-fg" cx="40" cy="40" r="34"
            stroke-dasharray="213.63" stroke-dashoffset="213.63"/>
        </svg>
        <div class="progress-tier-num">
          <span class="tier-big" id="tier-num">1</span>
          <span class="tier-sub" id="mastery-pct">0%</span>
        </div>
      </div>
    </div>
    <div class="stats-row">
      <div class="sstat"><div class="sstat-val" id="today-reviewed">0</div><div class="sstat-lbl">Hoje</div></div>
      <div class="sstat"><div class="sstat-val" id="today-mastered">0</div><div class="sstat-lbl">Dominadas</div></div>
      <div class="sstat"><div class="sstat-val" id="story-count">0</div><div class="sstat-lbl">Historias</div></div>
    </div>
  </div>

  <!-- Palavra do Dia -->
  <div class="wod-card" id="wod-card" style="display:none">
    <div class="wod-header">Palavra do Dia</div>
    <div class="wod-word" id="wod-word"></div>
    <div class="wod-sentence" id="wod-sentence"></div>
  </div>

  <!-- Practice -->
  <div class="section-hdr">Praticar</div>
  <div class="feature-grid" style="animation:fadeIn 0.4s ease-out 0.1s both">
    <a href="/drill" class="fcard blue-edge">
      <div class="fcard-icon blue">&#x1f3af;</div>
      <div class="fcard-title">Treinar</div>
      <div class="fcard-desc">Drills com audio, imagem e SRS</div>
      <div class="fcard-badge" id="due-badge">0 due</div>
    </a>
    <a href="/stories" class="fcard purple-edge">
      <div class="fcard-icon purple">&#x1f4d6;</div>
      <div class="fcard-title">Historias</div>
      <div class="fcard-desc">Narrativas graduadas 10 min</div>
      <div class="fcard-badge" id="stories-badge">0 stories</div>
    </a>
    <a href="/drill?mode=weak" class="fcard red-edge">
      <div class="fcard-icon red">&#x26a0;&#xfe0f;</div>
      <div class="fcard-title">Reforco</div>
      <div class="fcard-desc">Palavras que voce mais erra</div>
      <div class="fcard-badge red" id="weak-badge">0 fracas</div>
    </a>
    <a href="/conversa" class="fcard cyan-edge">
      <div class="fcard-icon cyan">&#x1f4ac;</div>
      <div class="fcard-title">Conversa</div>
      <div class="fcard-desc">Papo livre com IA baiana</div>
    </a>
  </div>

  <!-- Today -->
  <div class="section-hdr">Hoje</div>
  <div class="today-card">
    <div class="today-stat">
      <div class="today-val" id="today-mins">0</div>
      <div class="today-lbl">Minutos</div>
    </div>
    <div class="today-stat">
      <div class="today-val" id="today-words">0</div>
      <div class="today-lbl">Revisadas</div>
    </div>
    <div class="today-stat">
      <div class="today-val" id="today-new">0</div>
      <div class="today-lbl">Dominadas</div>
    </div>
  </div>

</div>

<!-- Bottom Tab Bar -->
<nav class="tab-bar">
  <a href="/" class="tab active">
    <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
    <span>Inicio</span>
  </a>
  <a href="/drill" class="tab">
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

<script>
var tierNames={1:'Survival',2:'Daily Core',3:'Conversational',4:'Fluency',5:'Nuanced',6:'Near-Native'};
fetch('/api/home-stats').then(r=>r.json()).then(d=>{
  document.getElementById('tier-label').textContent='Tier '+d.tier;
  document.getElementById('tier-name').textContent=tierNames[d.tier]||'';
  document.getElementById('tier-num').textContent=d.tier;
  document.getElementById('due').textContent=d.due;
  document.getElementById('mastery-pct').textContent=d.mastery_pct+'%';
  document.getElementById('streak').textContent=d.streak||0;
  document.getElementById('story-count').textContent=d.story_count;
  document.getElementById('due-badge').textContent=d.due+' due';
  document.getElementById('stories-badge').textContent=d.story_count+' stories';
  document.getElementById('weak-badge').textContent=(d.weak_count||0)+' fracas';
  // Progress ring (r=34, circumference=2*pi*34=213.63)
  var circumference=213.63;
  var offset=circumference-(d.mastery_pct/100)*circumference;
  document.getElementById('ring-fg').style.strokeDashoffset=offset;
  // Word of the Day
  if(d.word_of_day){
    var wod=d.word_of_day;
    document.getElementById('wod-word').textContent=wod.text||'';
    document.getElementById('wod-sentence').textContent=wod.sentence||'';
    document.getElementById('wod-card').style.display='block';
  }
});
fetch('/api/daily-stats').then(r=>r.json()).then(d=>{
  var t=d.today||{};
  document.getElementById('today-reviewed').textContent=t.words_reviewed||0;
  document.getElementById('today-mastered').textContent=t.words_mastered||0;
  document.getElementById('today-words').textContent=t.words_reviewed||0;
  document.getElementById('today-new').textContent=t.words_mastered||0;
  document.getElementById('today-mins').textContent=Math.round(t.minutes||0);
});
</script>
</body></html>"""


# ── Conversa HTML ─────────────────────────────────────────────

CONVERSA_HTML = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Oxe Protocol — Conversa</title>
<style>
  @keyframes fadeUp { from{opacity:0;transform:translateY(12px)} to{opacity:1;transform:translateY(0)} }
  @keyframes micPulse { 0%,100%{box-shadow:0 0 0 0 rgba(248,113,113,0.4)} 50%{box-shadow:0 0 0 10px rgba(248,113,113,0)} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    display: flex; flex-direction: column; height: 100vh; height: 100dvh;
    overflow: hidden; -webkit-user-select: none; user-select: none;
  }
  .header {
    padding: 14px 20px; background: rgba(255,255,255,0.03);
    border-bottom: 2px solid transparent;
    border-image: linear-gradient(90deg, #3B82F6, #7C5CFC) 1;
    display: flex; justify-content: space-between; align-items: center;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    flex-shrink: 0;
  }
  .header h1 {
    font-size: 1.05em; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .header-btns { display: flex; gap: 8px; }
  .hdr-btn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
    color: #9ca3af; padding: 6px 14px; border-radius: 10px; font-size: 0.8em;
    cursor: pointer; backdrop-filter: blur(10px); transition: all 0.2s;
    font-weight: 600;
  }
  .hdr-btn:active { background: rgba(255,255,255,0.1); }
  .messages {
    flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px;
    -webkit-overflow-scrolling: touch;
  }
  .msg {
    max-width: 82%; padding: 12px 16px; border-radius: 20px; font-size: 0.95em;
    line-height: 1.5; animation: fadeUp 0.3s ease-out;
    word-wrap: break-word;
  }
  .msg.ai {
    align-self: flex-start; background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.06);
    border-bottom-left-radius: 4px;
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
  }
  .msg.user {
    align-self: flex-end;
    background: linear-gradient(135deg, rgba(59,130,246,0.25), rgba(124,92,252,0.2));
    border: 1px solid rgba(59,130,246,0.3);
    border-bottom-right-radius: 4px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.2);
  }
  .msg.ai .typing { color: #525263; }
  .input-bar {
    padding: 12px 16px 80px; background: rgba(255,255,255,0.03);
    border-top: 1px solid rgba(255,255,255,0.06);
    display: flex; gap: 10px; align-items: center; flex-shrink: 0;
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .input-bar input {
    flex: 1; padding: 12px 16px; background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08); border-radius: 14px;
    color: #fafafa; font-size: 1em; outline: none; font-family: inherit;
    -webkit-appearance: none; transition: border-color 0.2s;
  }
  .input-bar input:focus { border-color: rgba(59,130,246,0.5); }
  .input-bar input::placeholder { color: #333; }
  .send-btn {
    width: 44px; height: 44px; border-radius: 50%; border: none;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    font-size: 1.2em; cursor: pointer; display: flex; align-items: center;
    justify-content: center; flex-shrink: 0; transition: all 0.2s;
    box-shadow: 0 4px 12px rgba(59,130,246,0.25);
  }
  .send-btn:active { transform: scale(0.95); }
  .send-btn:disabled { background: rgba(255,255,255,0.04); color: #333; box-shadow: none; }
  .mic-send-btn {
    width: 44px; height: 44px; border-radius: 50%; border: 1px solid rgba(255,255,255,0.1);
    background: rgba(255,255,255,0.04); color: #60a5fa; font-size: 1.2em;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; transition: all 0.2s;
  }
  .mic-send-btn.recording { border-color: #f87171; color: #f87171; animation: micPulse 1.2s infinite; }
  .mic-send-btn:active { transform: scale(0.95); }
</style>
</head><body>

<div class="header">
  <h1>CONVERSA</h1>
  <div class="header-btns">
    <button class="hdr-btn" onclick="newConversa()">Nova</button>
  </div>
</div>

<div class="messages" id="messages"></div>

<div class="input-bar">
  <input type="text" id="msg-input" placeholder="Fala, parceiro..." autocomplete="off" autocorrect="off">
  <button class="mic-send-btn" id="mic-send-btn" onclick="toggleConvMic()">&#x1F3A4;</button>
  <button class="send-btn" id="send-btn" onclick="sendMessage()">&#x27A4;</button>
</div>

<audio id="conv-player" preload="auto"></audio>

<script>
const msgBox = document.getElementById('messages');
const msgInput = document.getElementById('msg-input');
const sendBtn = document.getElementById('send-btn');
const convPlayer = document.getElementById('conv-player');
const micSendBtn = document.getElementById('mic-send-btn');

let convRecording = false;
let convRecorder = null;
let convMicChunks = [];

msgInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

function addMsg(text, role) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  msgBox.appendChild(div);
  msgBox.scrollTop = msgBox.scrollHeight;
  return div;
}

async function sendMessage(text) {
  const msg = text || msgInput.value.trim();
  if (!msg) return;
  msgInput.value = '';
  addMsg(msg, 'user');

  sendBtn.disabled = true;
  const typing = addMsg('...', 'ai');
  typing.innerHTML = '<span class="typing">Pensando...</span>';

  try {
    const res = await fetch('/api/conversa/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });
    const data = await res.json();
    typing.textContent = data.reply || 'Erro';
    if (data.audio_file) {
      convPlayer.src = '/audio/' + data.audio_file;
      convPlayer.play().catch(() => {});
    }
  } catch (e) {
    typing.textContent = 'Erro de conexao.';
  }
  sendBtn.disabled = false;
  msgInput.focus();
}

function newConversa() {
  fetch('/api/conversa/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ message: '__reset__' }),
  });
  msgBox.innerHTML = '';
  addMsg('Opa! Bora conversar, parceiro?', 'ai');
}

async function toggleConvMic() {
  if (convRecording) {
    convRecording = false;
    micSendBtn.classList.remove('recording');
    if (convRecorder && convRecorder.state === 'recording') convRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    convMicChunks = [];
    convRecorder = new MediaRecorder(stream);
    convRecorder.ondataavailable = e => { if (e.data.size > 0) convMicChunks.push(e.data); };
    convRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(convMicChunks, { type: convRecorder.mimeType || 'audio/mp4' });
      // For now, transcribe locally is not available — inform user to type
      addMsg('[Gravacao de voz — use texto por enquanto]', 'user');
    };
    convRecorder.start();
    convRecording = true;
    micSendBtn.classList.add('recording');
    setTimeout(() => { if (convRecording) toggleConvMic(); }, 10000);
  } catch (e) {
    // Mic not available
  }
}

// Init
addMsg('E ai, parceiro! Bora jogar conversa fora?', 'ai');
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
  <a href="/drill" class="tab">
    <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
    <span>Treinar</span>
  </a>
  <a href="/stories" class="tab">
    <svg viewBox="0 0 24 24"><path d="M21 5c-1.11-.35-2.33-.5-3.5-.5-1.95 0-4.05.4-5.5 1.5-1.45-1.1-3.55-1.5-5.5-1.5S2.45 4.9 1 6v14.65c0 .25.25.5.5.5.1 0 .15-.05.25-.05C3.1 20.45 5.05 20 6.5 20c1.95 0 4.05.4 5.5 1.5 1.35-.85 3.8-1.5 5.5-1.5 1.65 0 3.35.3 4.75 1.05.1.05.15.05.25.05.25 0 .5-.25.5-.5V6c-.6-.45-1.25-.75-2-1z"/></svg>
    <span>Historias</span>
  </a>
  <a href="/conversa" class="tab active">
    <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
    <span>Conversa</span>
  </a>
</nav>

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
            self._drill_next(query)

        # ── Conversa ──
        elif path == "/conversa":
            self._html(CONVERSA_HTML)

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
        elif path == "/api/daily-stats":
            self._daily_stats()
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

        # Multipart routes (audio upload)
        if path in ("/api/score-pronunciation", "/api/shadow-score"):
            if path == "/api/score-pronunciation":
                self._score_pronunciation()
            else:
                self._shadow_score()
            return

        body = self._read_body()

        # ── Drill ──
        if path == "/api/respond":
            self._drill_respond(body)
        elif path == "/api/explain":
            self._drill_explain(body)
        elif path == "/api/trap-respond":
            self._drill_trap_respond(body)
        elif path == "/api/cloze-respond":
            self._cloze_respond(body)

        # ── Conversa ──
        elif path == "/api/conversa/send":
            self._conversa_send(body)

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
        streak = get_streak()
        weak_count = len(get_weak_words())
        # Word of the day — deterministic per date
        conn2 = get_conn()
        day_seed = int(datetime.now().strftime("%Y%m%d"))
        total_words = conn2.execute("SELECT COUNT(*) FROM word_bank WHERE difficulty_tier <= ?", (tier,)).fetchone()[0]
        wod = None
        if total_words > 0:
            idx = day_seed % total_words
            row = conn2.execute(
                "SELECT word FROM word_bank WHERE difficulty_tier <= ? LIMIT 1 OFFSET ?",
                (tier, idx)
            ).fetchone()
            if row:
                wod = {"text": row[0], "sentence": ""}
        conn2.close()
        resp = {
            "tier": tier,
            "due": due,
            "mastery_pct": current_pct,
            "story_count": story_count,
            "streak": streak,
            "weak_count": weak_count,
        }
        if wod:
            resp["word_of_day"] = wod
        self._json(resp)

    def _daily_stats(self):
        self._json({
            "today": get_daily_stats(),
            "streak": get_streak(),
        })

    # ── Drill Endpoints ────────────────────────────────────

    def _drill_next(self, query=None):
        global _laranjada_remaining

        mode = query.get("mode", [None])[0] if query else None

        if mode == "weak":
            weak = get_weak_words()
            word = weak[0] if weak else None
        else:
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

        # Cloze mode: 30% chance (not in weak mode, not trap)
        is_cloze = (mode != "weak" and random.random() < CLOZE_PROBABILITY)

        if is_cloze:
            cloze_text, full_carrier = build_cloze(word["word"], carrier)
            fname = generate_tts(cloze_text)
            if not fname:
                self._json({"error": "TTS failed"})
                return
            img_fname = generate_image(word["word"])
            due_words = list(get_due_words())
            upcoming = [w["word"] for w in due_words[:5] if w["word"] != word["word"]]
            if upcoming:
                prefetch_images(upcoming)
            tier = get_unlocked_tier()
            self._json({
                "type": "cloze",
                "word_id": word["id"],
                "word": word["word"],
                "cloze_text": cloze_text,
                "full_carrier": full_carrier,
                "audio_file": fname,
                "image_file": img_fname,
                "tier": word["difficulty_tier"],
                "tier_label": TIER_LABELS[word["difficulty_tier"]],
                "mastery": word["mastery_level"],
                "due_count": len(due_words),
            })
            return

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

    # ── Cloze Endpoint ──────────────────────────────────────

    def _cloze_respond(self, body):
        word_id = body.get("word_id")
        answer = body.get("answer", "").strip().lower()
        expected = body.get("expected", "").strip().lower()

        # Normalize: strip accents for comparison
        import unicodedata
        def _strip_accents(s):
            return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

        norm_answer = _strip_accents(answer)
        norm_expected = _strip_accents(expected)

        dist = _levenshtein(norm_answer, norm_expected)

        if dist == 0 or (dist == 1 and norm_answer != norm_expected):
            # Exact or accent difference
            rating = Rating.Good
            correct = True
        elif dist <= 2:
            rating = Rating.Hard
            correct = False
        else:
            rating = Rating.Again
            correct = False

        card, new_mastery, downgraded = record_review(word_id, rating, None)
        rating_name = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}[rating.value]
        log_drill(word_id, expected, rating.value, 0, drill_type="cloze")

        self._json({
            "correct": correct,
            "rating": rating.value,
            "rating_name": rating_name,
            "new_mastery": new_mastery,
            "edit_distance": dist,
        })

    # ── Pronunciation Scoring Endpoint ────────────────────

    def _parse_multipart(self):
        """Parse multipart/form-data from the request."""
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if "boundary=" not in content_type:
            return {}, raw

        boundary = content_type.split("boundary=")[1].strip()
        if boundary.startswith('"') and boundary.endswith('"'):
            boundary = boundary[1:-1]

        fields = {}
        parts = raw.split(("--" + boundary).encode())
        for part in parts:
            if part in (b"", b"--", b"--\r\n", b"\r\n"):
                continue
            if b"\r\n\r\n" not in part:
                continue
            header_data, body_data = part.split(b"\r\n\r\n", 1)
            if body_data.endswith(b"\r\n"):
                body_data = body_data[:-2]
            header_str = header_data.decode("utf-8", errors="replace")
            name_match = re.search(r'name="([^"]+)"', header_str)
            if name_match:
                name = name_match.group(1)
                fname_match = re.search(r'filename="([^"]+)"', header_str)
                if fname_match:
                    fields[name] = {"filename": fname_match.group(1), "data": body_data}
                else:
                    fields[name] = body_data.decode("utf-8", errors="replace")
        return fields

    def _score_pronunciation(self):
        fields = self._parse_multipart()
        audio_field = fields.get("audio")
        word_id = fields.get("word_id", "0")
        native_audio_name = fields.get("native_audio", "")

        if not audio_field or not isinstance(audio_field, dict):
            self._json({"error": "No audio uploaded", "score": 0})
            return

        # Save uploaded audio
        ts = int(time.time() * 1000)
        ext = ".m4a"
        user_fname = f"user_pron_{word_id}_{ts}{ext}"
        user_path = AUDIO_DIR / user_fname
        with open(user_path, "wb") as f:
            f.write(audio_field["data"])

        native_path = AUDIO_DIR / native_audio_name
        if not native_path.exists():
            self._json({"error": "Native audio not found", "score": 0})
            return

        try:
            result = score_pronunciation(str(user_path), str(native_path))
            self._json({
                "score": result.get("score", 0),
                "details": result.get("issues", []),
                "force_redrill": result.get("force_redrill", False),
                "metrics": result.get("metrics", {}),
            })
        except Exception as e:
            print(f"[Pronunciation] Error: {e}")
            self._json({"error": str(e), "score": 0})

    def _shadow_score(self):
        fields = self._parse_multipart()
        audio_field = fields.get("audio")
        word_id = fields.get("word_id", "0")
        native_audio_name = fields.get("native_audio", "")

        if not audio_field or not isinstance(audio_field, dict):
            self._json({"error": "No audio uploaded", "score": 0})
            return

        ts = int(time.time() * 1000)
        user_fname = f"shadow_{word_id}_{ts}.m4a"
        user_path = AUDIO_DIR / user_fname
        with open(user_path, "wb") as f:
            f.write(audio_field["data"])

        native_path = AUDIO_DIR / native_audio_name
        if not native_path.exists():
            self._json({"error": "Native audio not found", "score": 0})
            return

        try:
            result = score_pronunciation(str(user_path), str(native_path))
            self._json({
                "score": result.get("score", 0),
                "details": result.get("issues", []),
                "force_redrill": result.get("force_redrill", False),
                "metrics": result.get("metrics", {}),
                "user_audio_url": "/audio/" + user_fname,
            })
        except Exception as e:
            print(f"[Shadow] Error: {e}")
            self._json({"error": str(e), "score": 0})

    # ── Conversa Endpoints ────────────────────────────────

    def _conversa_send(self, body):
        global _conversa_history
        message = body.get("message", "").strip()

        if message == "__reset__":
            _conversa_history = []
            self._json({"reply": "Nova conversa!", "audio_file": None})
            return

        if not message:
            self._json({"reply": "Fala alguma coisa, parceiro!", "audio_file": None})
            return

        # Get recent drilled words
        recent_words = self._get_recent_words()
        words_str = ", ".join(recent_words) if recent_words else "nenhuma palavra recente"

        system_prompt = (
            "Tu \u00E9 um parceiro soteropolitano de Salvador. "
            "Conversa naturalmente em portugu\u00EAs baiano \u2014 usa oxe, vixe, massa, arretado. "
            "NUNCA use ingl\u00EAs. Respostas curtas, 2-3 frases m\u00E1ximo. "
            f"Tenta usar estas palavras na conversa: {words_str}"
        )

        _conversa_history.append({"role": "user", "content": message})

        # Keep history manageable
        if len(_conversa_history) > 20:
            _conversa_history = _conversa_history[-20:]

        messages = [{"role": "system", "content": system_prompt}] + _conversa_history

        try:
            import openai
            client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
            resp = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=150,
                temperature=0.8,
                messages=messages,
            )
            reply = resp.choices[0].message.content.strip()
            _conversa_history.append({"role": "assistant", "content": reply})

            # Generate TTS
            audio_fname = generate_tts(reply)

            self._json({
                "reply": reply,
                "audio_file": audio_fname,
            })
        except Exception as e:
            print(f"[Conversa] Error: {e}")
            self._json({"reply": "Oxe, deu erro aqui. Tenta de novo.", "audio_file": None})

    def _get_recent_words(self):
        """Get up to 5 recently drilled words from today."""
        try:
            conn = get_conn()
            # Get words with recent last_retrieval_latency updates
            rows = conn.execute(
                """SELECT word FROM word_bank
                   WHERE last_retrieval_latency IS NOT NULL
                   ORDER BY ROWID DESC LIMIT 20"""
            ).fetchall()
            conn.close()
            words = [r["word"] for r in rows]
            if len(words) > 5:
                words = random.sample(words, 5)
            return words
        except Exception:
            return []

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
        ct = "audio/mpeg"
        if filename.endswith(".m4a"):
            ct = "audio/mp4"
        elif filename.endswith(".wav"):
            ct = "audio/wav"
        self.send_response(200)
        self.send_header("Content-Type", ct)
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
    migrate_db()
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
    print(f"  /          Home")
    print(f"  /drill     1+T Drills + Pronunciation + Cloze")
    print(f"  /stories   Graded Stories")
    print(f"  /conversa  Conversation Mode")
    print(f"  {'='*44}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
