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
    add_chunk, get_next_chunk, get_due_chunks,
    update_chunk_pass, record_chunk_review, get_chunk_by_id,
    get_review_feed,
)
from story_gen import LEVELS, init_story_db, generate_story, generate_story_audio
from podcast_gen import generate_podcast, save_podcast, get_podcast, list_podcasts
from prosody_transplant import ensure_clone_exists, register_clone, get_or_generate_golden
from srs_engine import migrate_v2
from acquisition_engine import (
    get_or_create_state, update_state_after_review,
    get_state_distribution, run_fragility_scan, get_fragile_queue,
    get_fragile_summary, get_items_in_state, resolve_fragility,
)
from daily_router import (
    get_today_plan, get_next_block, record_block_completion,
    adjust_plan_mid_session, get_plan_progress,
)
from training_modes import (
    select_mode_for_item, get_drill_config, get_available_modes,
    TRAINING_MODES,
)
from chunk_engine import (
    extract_chunks_from_text, extract_chunks_from_story,
    extract_chunks_from_podcast, rank_chunk_families,
    get_next_chunks_for_srs, add_chunks_to_queue,
    get_family_variants,
)
from content_ladder import (
    get_learner_level, classify_content, select_content_for_mode,
    compute_compression_pct, classify_all_content,
)
from fatigue_monitor import (
    check_fatigue, record_review_event, design_session_blocks,
    get_fatigue_history, reset_session as reset_fatigue_session,
)
from speech_ladder import (
    get_current_stage, evaluate_gates, advance_stage,
    check_regression, get_activities_for_stage,
)

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

# ── Imports from dictionary_engine ────────────────────────────
from dictionary_engine import (
    search_word, get_full_word_data, log_search,
)

# ── Imports from story_server ──────────────────────────────────
from story_server import STORY_HTML

# ── Shared Tab Bar ─────────────────────────────────────────────

def TAB_BAR_HTML(active_tab):
    """Generate the 5-tab bottom navigation bar.
    active_tab is one of: inicio, buscar, treinar, biblioteca, conversa
    """
    tabs = [
        ("inicio", "/", "In\u00edcio",
         '<svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>'),
        ("buscar", "/search", "Buscar",
         '<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>'),
        ("treinar", "/drill", "Treinar",
         '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>'),
        ("biblioteca", "/library", "Biblioteca",
         '<svg viewBox="0 0 24 24"><path d="M21 5c-1.11-.35-2.33-.5-3.5-.5-1.95 0-4.05.4-5.5 1.5-1.45-1.1-3.55-1.5-5.5-1.5S2.45 4.9 1 6v14.65c0 .25.25.5.5.5.1 0 .15-.05.25-.05C3.1 20.45 5.05 20 6.5 20c1.95 0 4.05.4 5.5 1.5 1.35-.85 3.8-1.5 5.5-1.5 1.65 0 3.35.3 4.75 1.05.1.05.15.05.25.05.25 0 .5-.25.5-.5V6c-.6-.45-1.25-.75-2-1z"/></svg>'),
        ("conversa", "/conversa", "Conversa",
         '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>'),
    ]
    items = []
    for key, href, label, icon in tabs:
        cls = "tab active" if key == active_tab else "tab"
        color = "#3B82F6" if key == active_tab else "rgba(255,255,255,0.4)"
        items.append(
            f'<a href="{href}" class="{cls}" style="color:{color}">{icon}<span>{label}</span></a>'
        )
    return (
        '<style>'
        '.tab-bar{position:fixed;bottom:0;left:0;right:0;z-index:100;'
        'display:flex;justify-content:space-around;align-items:center;'
        'height:60px;padding-bottom:env(safe-area-inset-bottom,0);'
        'background:rgba(10,10,11,0.95);border-top:1px solid rgba(255,255,255,0.06);'
        'backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}'
        '.tab{display:flex;flex-direction:column;align-items:center;gap:3px;'
        'text-decoration:none;font-size:0.62em;font-weight:500;'
        '-webkit-tap-highlight-color:transparent;padding:6px 12px;transition:color 0.2s}'
        '.tab svg{width:22px;height:22px;fill:currentColor}'
        '</style>'
        '<nav class="tab-bar">' + ''.join(items) + '</nav>'
    )


# Session state
_laranjada_remaining = 0

# Conversation history for Conversa mode
_conversa_history = []
_conversa_system_prompt = ""
_conversa_session_id = None
_conversa_chunks_vocab = []  # known chunks injected at session start


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
        <div class="progress-tier-name" id="tier-name">Sobrevivência</div>
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
      <div class="fcard-badge" id="due-badge">0 pendentes</div>
    </a>
    <a href="/library" class="fcard purple-edge">
      <div class="fcard-icon purple">&#x1f4d6;</div>
      <div class="fcard-title">Biblioteca</div>
      <div class="fcard-desc">Historias, podcasts e revisao</div>
      <div class="fcard-badge" id="stories-badge">0 historias</div>
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

{tab_bar}

<script>
var tierNames={1:'Sobrevivência',2:'Cotidiano',3:'Conversação',4:'Fluência',5:'Nuance',6:'Quase Nativo'};
fetch('/api/home-stats').then(r=>r.json()).then(d=>{
  document.getElementById('tier-label').textContent='Tier '+d.tier;
  document.getElementById('tier-name').textContent=tierNames[d.tier]||'';
  document.getElementById('tier-num').textContent=d.tier;
  document.getElementById('due').textContent=d.due;
  document.getElementById('mastery-pct').textContent=d.mastery_pct+'%';
  document.getElementById('streak').textContent=d.streak||0;
  document.getElementById('story-count').textContent=d.story_count;
  document.getElementById('due-badge').textContent=d.due+' pendentes';
  document.getElementById('stories-badge').textContent=d.story_count+' historias';
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

{tab_bar}

</body></html>"""


# ── Search / Dictionary HTML ──────────────────────────────────

SEARCH_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Oxe — Dicionário</title>
<style>
  @keyframes fadeUp { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; -webkit-user-select: none; user-select: none;
    padding-bottom: 76px;
  }
  .search-bar {
    position: sticky; top: 0; z-index: 20; padding: 14px 16px;
    background: rgba(10,10,11,0.92); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
    border-bottom: 2px solid transparent; border-image: linear-gradient(90deg, #3B82F6, #7C5CFC) 1;
  }
  .search-wrap {
    display: flex; align-items: center; gap: 10px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px; padding: 12px 16px;
    backdrop-filter: blur(10px);
  }
  .search-wrap svg { width: 20px; height: 20px; fill: #60a5fa; flex-shrink: 0; }
  .search-wrap input {
    flex: 1; background: none; border: none; outline: none; color: #fafafa;
    font-size: 1em; font-family: inherit; -webkit-appearance: none;
  }
  .search-wrap input::placeholder { color: #525263; }
  .search-clear {
    width: 24px; height: 24px; border-radius: 50%; border: none;
    background: rgba(255,255,255,0.08); color: #9ca3af; font-size: 0.8em;
    cursor: pointer; display: none; align-items: center; justify-content: center;
  }
  .autocomplete {
    position: absolute; left: 16px; right: 16px; top: 100%;
    background: rgba(20,20,22,0.98); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px; overflow: hidden; display: none; z-index: 30;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    max-height: 300px; overflow-y: auto;
  }
  .autocomplete.visible { display: block; }
  .ac-item {
    padding: 14px 18px; cursor: pointer; border-bottom: 1px solid rgba(255,255,255,0.04);
    display: flex; justify-content: space-between; align-items: center;
    transition: background 0.15s;
  }
  .ac-item:active { background: rgba(59,130,246,0.1); }
  .ac-item:last-child { border-bottom: none; }
  .ac-word { font-weight: 600; }
  .ac-tier { font-size: 0.7em; color: #525263; }

  .page { padding: 20px 16px; }

  /* ── Result Card ── */
  .result-card {
    display: none; animation: fadeUp 0.4s ease-out;
  }
  .result-card.visible { display: block; }
  .result-word {
    font-size: 1.8em; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    margin-bottom: 4px;
  }
  .result-meta { font-size: 0.78em; color: #7a7a8e; margin-bottom: 20px; }
  .result-audio-btn {
    display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
    background: rgba(59,130,246,0.1); border: 1px solid rgba(59,130,246,0.2);
    border-radius: 20px; color: #60a5fa; font-size: 0.8em; font-weight: 600;
    cursor: pointer; margin-bottom: 20px; transition: all 0.2s;
  }
  .result-audio-btn:active { transform: scale(0.97); }
  .result-audio-btn svg { width: 16px; height: 16px; fill: currentColor; }

  /* ── Tabs ── */
  .tab-row {
    display: flex; gap: 0; border-bottom: 1px solid rgba(255,255,255,0.06); margin-bottom: 20px;
  }
  .tab-btn {
    flex: 1; padding: 12px 0; text-align: center; font-size: 0.78em; font-weight: 600;
    color: #525263; cursor: pointer; border-bottom: 2px solid transparent;
    transition: all 0.2s; background: none; border-top: none; border-left: none; border-right: none;
  }
  .tab-btn.active { color: #60a5fa; border-bottom-color: #3B82F6; }
  .tab-content { display: none; animation: fadeUp 0.3s ease-out; }
  .tab-content.visible { display: block; }

  /* ── Definition ── */
  .def-card {
    background: rgba(255,255,255,0.03); border-radius: 16px; padding: 20px;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 4px 12px rgba(0,0,0,0.3);
    margin-bottom: 14px;
  }
  .def-text { font-size: 1em; line-height: 1.7; color: #e0e0e5; }
  .def-regional {
    display: inline-block; margin-top: 10px; padding: 3px 10px; border-radius: 10px;
    font-size: 0.7em; font-weight: 600; background: rgba(59,130,246,0.1); color: #60a5fa;
  }
  .def-chunk { font-size: 0.9em; color: #7a7a8e; margin-top: 10px; font-style: italic; }

  /* ── Examples ── */
  .example-item {
    padding: 14px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
    display: flex; align-items: flex-start; gap: 12px;
  }
  .example-item:last-child { border-bottom: none; }
  .ex-audio {
    width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
    background: rgba(59,130,246,0.1); border: none; color: #60a5fa;
    display: flex; align-items: center; justify-content: center; cursor: pointer;
    font-size: 0.9em;
  }
  .ex-audio:active { transform: scale(0.95); }
  .ex-text { font-size: 0.95em; line-height: 1.6; color: #e0e0e5; }
  .ex-chunk { color: #60a5fa; font-weight: 600; }

  /* ── Pronunciation ── */
  .pron-section { margin-bottom: 20px; }
  .pron-label { font-size: 0.7em; color: #525263; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .pron-value { font-size: 1.1em; color: #e0e0e5; letter-spacing: 1px; }
  .pron-guide { font-size: 0.9em; color: #7a7a8e; line-height: 1.6; margin-top: 12px; }

  /* ── Expressions ── */
  .expr-item {
    background: rgba(255,255,255,0.03); border-radius: 14px; padding: 16px;
    margin-bottom: 10px;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 2px 8px rgba(0,0,0,0.2);
  }
  .expr-phrase { font-size: 1em; font-weight: 600; color: #fafafa; margin-bottom: 4px; }
  .expr-meaning { font-size: 0.85em; color: #7a7a8e; line-height: 1.5; }

  /* ── Add to SRS button ── */
  .add-srs-btn {
    width: 100%; padding: 16px; margin-top: 24px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    border: none; border-radius: 16px; font-size: 1em; font-weight: 700;
    cursor: pointer; transition: all 0.2s;
    box-shadow: 0 4px 16px rgba(59,130,246,0.25);
  }
  .add-srs-btn:active { transform: scale(0.98); opacity: 0.9; }
  .add-srs-btn:disabled { background: rgba(255,255,255,0.04); color: #333; box-shadow: none; }

  /* ── Empty state ── */
  .empty-state { text-align: center; padding: 60px 20px; color: #525263; }
  .empty-state .icon { font-size: 3em; margin-bottom: 16px; opacity: 0.3; }
  .empty-state p { font-size: 0.9em; line-height: 1.5; }

  /* ── Loading ── */
  .loading { text-align: center; padding: 40px; color: #525263; font-size: 0.9em; }

  /* ── Tab Bar — injected by TAB_BAR_HTML ── */
</style>
</head><body>

<div class="search-bar">
  <div class="search-wrap">
    <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
    <input type="text" id="search-input" placeholder="Buscar palavra..." autocomplete="off" autocorrect="off" spellcheck="false">
    <button class="search-clear" id="search-clear" onclick="clearSearch()">&times;</button>
  </div>
  <div class="autocomplete" id="autocomplete"></div>
</div>

<div class="page">
  <!-- Empty state -->
  <div class="empty-state" id="empty-state">
    <div class="icon">&#x1F50D;</div>
    <p>Busca uma palavra pra ver<br>definição, exemplos e pronúncia</p>
  </div>

  <!-- Loading -->
  <div class="loading" id="loading" style="display:none">Carregando...</div>

  <!-- Result card -->
  <div class="result-card" id="result-card">
    <div class="result-word" id="r-word"></div>
    <div class="result-meta" id="r-meta"></div>
    <button class="result-audio-btn" id="r-audio-btn" onclick="playWordAudio()">
      <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>
      Ouvir
    </button>

    <!-- Tabs -->
    <div class="tab-row">
      <button class="tab-btn active" onclick="showTab(0)">Dicionário</button>
      <button class="tab-btn" onclick="showTab(1)">Exemplos</button>
      <button class="tab-btn" onclick="showTab(2)">Pronúncia</button>
      <button class="tab-btn" onclick="showTab(3)">Expressões</button>
    </div>

    <!-- Tab: Dicionário -->
    <div class="tab-content visible" id="tab-0">
      <div class="def-card">
        <div class="def-text" id="def-text"></div>
        <div class="def-regional" id="def-regional"></div>
        <div class="def-chunk" id="def-chunk"></div>
      </div>
    </div>

    <!-- Tab: Exemplos -->
    <div class="tab-content" id="tab-1">
      <div id="examples-list"></div>
    </div>

    <!-- Tab: Pronúncia -->
    <div class="tab-content" id="tab-2">
      <div class="pron-section">
        <div class="pron-label">Sílabas</div>
        <div class="pron-value" id="pron-silabas"></div>
      </div>
      <div class="pron-section">
        <div class="pron-label">Guia fonético</div>
        <div class="pron-guide" id="pron-guide"></div>
      </div>
    </div>

    <!-- Tab: Expressões -->
    <div class="tab-content" id="tab-3">
      <div id="expressions-list"></div>
    </div>

    <button class="add-srs-btn" id="add-srs-btn" onclick="addToSRS()">Adicionar ao treino</button>
  </div>
</div>

<audio id="player" preload="auto"></audio>

{tab_bar}

<script>
const searchInput = document.getElementById('search-input');
const acBox = document.getElementById('autocomplete');
const clearBtn = document.getElementById('search-clear');
const player = document.getElementById('player');
let debounceTimer = null;
let currentData = null;
let currentWordId = null;

searchInput.addEventListener('input', function() {
  clearTimeout(debounceTimer);
  const q = this.value.trim();
  clearBtn.style.display = q ? 'flex' : 'none';
  if (q.length < 2) { acBox.classList.remove('visible'); return; }
  debounceTimer = setTimeout(() => fetchSearch(q), 300);
});

searchInput.addEventListener('focus', function() {
  if (this.value.trim().length >= 2) acBox.classList.add('visible');
});

function clearSearch() {
  searchInput.value = '';
  clearBtn.style.display = 'none';
  acBox.classList.remove('visible');
  document.getElementById('result-card').classList.remove('visible');
  document.getElementById('empty-state').style.display = '';
}

async function fetchSearch(q) {
  try {
    const res = await fetch('/api/search?q=' + encodeURIComponent(q));
    const data = await res.json();
    if (!data.results || data.results.length === 0) {
      acBox.innerHTML = '<div class="ac-item" style="color:#525263">Nenhum resultado</div>';
    } else {
      acBox.innerHTML = data.results.map(r =>
        '<div class="ac-item" onclick="selectWord(' + r.word_id + ',\'' + r.word.replace(/'/g, "\\'") + '\')">' +
        '<span class="ac-word">' + r.word + '</span>' +
        '<span class="ac-tier">Tier ' + r.difficulty_tier + '</span></div>'
      ).join('');
    }
    acBox.classList.add('visible');
  } catch(e) { acBox.classList.remove('visible'); }
}

async function selectWord(wordId, word) {
  acBox.classList.remove('visible');
  searchInput.value = word;
  currentWordId = wordId;
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('loading').style.display = '';
  document.getElementById('result-card').classList.remove('visible');

  try {
    const res = await fetch('/api/search/word/' + wordId);
    currentData = await res.json();
    renderResult(currentData);
  } catch(e) {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('empty-state').style.display = '';
  }
}

function renderResult(d) {
  document.getElementById('loading').style.display = 'none';
  document.getElementById('r-word').textContent = d.word || '';
  const tierNames = {1:'Sobrevivência',2:'Cotidiano',3:'Conversação',4:'Fluência',5:'Nuance',6:'Quase Nativo'};
  document.getElementById('r-meta').textContent = 'Tier ' + (d.tier||1) + ' — ' + (tierNames[d.tier]||'') + ' · #' + (d.frequency_rank||'');

  // Definition tab
  const def = d.definition || {};
  document.getElementById('def-text').textContent = def.definicao || 'Sem definição disponível';
  document.getElementById('def-regional').textContent = (def.uso_regional || 'geral');
  document.getElementById('def-chunk').textContent = def.exemplo_chunk ? '"' + def.exemplo_chunk + '"' : '';

  // Examples tab
  const exList = document.getElementById('examples-list');
  exList.innerHTML = '';
  (d.examples || []).forEach(function(ex) {
    const div = document.createElement('div');
    div.className = 'example-item';
    div.innerHTML = '<button class="ex-audio" onclick="playExAudio(this)">&#x1F50A;</button>' +
      '<div class="ex-text">' + (ex.texto || '').replace(new RegExp('(' + (d.word||'') + ')', 'gi'), '<span class="ex-chunk">$1</span>') + '</div>';
    exList.appendChild(div);
  });

  // Pronunciation tab
  const pron = d.pronunciation || {};
  document.getElementById('pron-silabas').textContent = pron.silabas || '';
  document.getElementById('pron-guide').textContent = pron.guia_fonetico || '';

  // Expressions tab
  const exprList = document.getElementById('expressions-list');
  exprList.innerHTML = '';
  (d.expressions || []).forEach(function(expr) {
    const div = document.createElement('div');
    div.className = 'expr-item';
    div.innerHTML = '<div class="expr-phrase">' + (expr.expressao || '') + '</div>' +
      '<div class="expr-meaning">' + (expr.significado || '') + '</div>';
    exprList.appendChild(div);
  });

  // Show first tab
  showTab(0);
  document.getElementById('result-card').classList.add('visible');
  document.getElementById('add-srs-btn').disabled = false;
  document.getElementById('add-srs-btn').textContent = 'Adicionar ao treino';
}

function showTab(idx) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', i===idx));
  document.querySelectorAll('.tab-content').forEach((c,i) => c.classList.toggle('visible', i===idx));
}

function playWordAudio() {
  if (currentData && currentData.audio_file) {
    player.src = '/audio/' + currentData.audio_file;
    player.play().catch(()=>{});
  }
}

function playExAudio(btn) {
  // For now just play the main word audio
  playWordAudio();
}

async function addToSRS() {
  if (!currentData || !currentWordId) return;
  const btn = document.getElementById('add-srs-btn');
  btn.disabled = true;
  btn.textContent = 'Adicionando...';
  try {
    const def = currentData.definition || {};
    const chunk = def.exemplo_chunk || currentData.word || '';
    const carrier = (currentData.examples && currentData.examples[0]) ? currentData.examples[0].texto : chunk;
    await fetch('/api/search/add-to-srs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ word_id: currentWordId, chunk: chunk, carrier: carrier }),
    });
    btn.textContent = 'Adicionado ✓';
  } catch(e) {
    btn.textContent = 'Erro';
    btn.disabled = false;
  }
}
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
            self._html(HOME_HTML.replace("{tab_bar}", TAB_BAR_HTML("inicio")))

        # ── Search / Dictionary ──
        elif path == "/search":
            self._html(SEARCH_HTML.replace("{tab_bar}", TAB_BAR_HTML("buscar")))
        elif path == "/api/search":
            q = query.get("q", [""])[0]
            self._dict_search(q)
        elif path.startswith("/api/search/word/"):
            word_id = int(path.split("/")[4])
            self._dict_word(word_id)
        elif path == "/api/search/history":
            self._dict_history()

        # ── Drill ──
        elif path == "/drill":
            self._html(DRILL_HTML.replace("{tab_bar}", TAB_BAR_HTML("treinar")))
        elif path == "/api/drill/next":
            self._drill_next_chunk()

        # ── Conversa ──
        elif path == "/conversa":
            self._html(CONVERSA_HTML.replace("{tab_bar}", TAB_BAR_HTML("conversa")))

        # ── Library (Stories + Podcasts + Review) ──
        elif path == "/library":
            self._html(STORY_HTML.replace("{tab_bar}", TAB_BAR_HTML("biblioteca")))
        elif path == "/stories":
            self._html(STORY_HTML.replace("{tab_bar}", TAB_BAR_HTML("biblioteca")))
        elif path == "/api/library/review-feed":
            self._review_feed()
        elif path == "/api/library/podcasts":
            self._podcast_list()
        elif path.startswith("/api/library/podcast/") and path.count("/") == 4 and path.split("/")[4].isdigit():
            podcast_id = int(path.split("/")[4])
            self._podcast_get(podcast_id)
        elif path == "/api/levels":
            self._story_get_levels()
        elif path == "/api/stories":
            level = query.get("level", ["A1"])[0]
            self._story_get_stories(level)
        elif path.startswith("/api/story/") and path.count("/") == 3:
            story_id = int(path.split("/")[3])
            self._story_get_story(story_id)

        # ── Neural Mapping ──
        elif path == "/api/neural/status":
            self._neural_status()
        elif path.startswith("/api/neural/golden/"):
            word_id = int(path.split("/")[4])
            self._neural_golden(word_id)

        # ── Acquisition Engine ──
        elif path == "/api/dashboard":
            self._dashboard()
        elif path == "/api/acquisition/distribution":
            self._json(get_state_distribution())
        elif path == "/api/acquisition/items":
            state = query.get("state", ["UNKNOWN"])[0]
            limit = int(query.get("limit", ["100"])[0])
            item_type = query.get("item_type", [None])[0]
            self._json(get_items_in_state(state, item_type, limit))
        elif path == "/api/fragile":
            ft = query.get("type", ["known_but_slow"])[0]
            limit = int(query.get("limit", ["20"])[0])
            self._json(get_fragile_queue(ft, limit))
        elif path == "/api/fragile/summary":
            self._json(get_fragile_summary())

        # ── Daily Plan ──
        elif path == "/api/plan/today":
            self._json(get_today_plan())
        elif path == "/api/plan/next-block":
            block = get_next_block()
            self._json(block if block else {"done": True})
        elif path == "/api/plan/progress":
            self._json(get_plan_progress())

        # ── Chunks ──
        elif path == "/api/chunks/families":
            limit = int(query.get("limit", ["50"])[0])
            self._chunk_families(limit)
        elif path.startswith("/api/chunks/family/") and path.endswith("/variants"):
            family_id = int(path.split("/")[4])
            self._json(get_family_variants(family_id))

        # ── Training Modes ──
        elif path == "/api/modes/available":
            stage = int(query.get("stage", ["1"])[0])
            self._json({"modes": get_available_modes(stage)})
        elif path == "/api/modes/config":
            mode = query.get("mode", ["audio_meaning_recognition"])[0]
            self._json(get_drill_config(mode))

        # ── Speech Stage ──
        elif path == "/api/speech/stage":
            self._speech_stage()
        elif path == "/api/speech/gates":
            self._json(evaluate_gates())
        elif path == "/api/speech/activities":
            stage = int(query.get("stage", ["0"])[0])
            if stage == 0:
                stage = get_current_stage()
            self._json({"stage": stage, "activities": get_activities_for_stage(stage)})

        # ── Fatigue ──
        elif path == "/api/fatigue/status":
            self._json(check_fatigue())
        elif path == "/api/fatigue/history":
            date = query.get("date", [None])[0]
            self._json(get_fatigue_history(date))

        # ── Content Ladder ──
        elif path == "/api/content/level":
            self._json({"level": get_learner_level()})
        elif path == "/api/content/recommend":
            mode = query.get("mode", ["compression"])[0]
            limit = int(query.get("limit", ["10"])[0])
            self._json(select_content_for_mode(mode, limit=limit))

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
        if path in ("/api/score-pronunciation", "/api/shadow-score", "/api/drill/score"):
            if path == "/api/score-pronunciation":
                self._score_pronunciation()
            elif path == "/api/drill/score":
                self._score_pronunciation()
            else:
                self._shadow_score()
            return

        body = self._read_body()

        # ── Dictionary ──
        if path == "/api/search/add-to-srs":
            self._dict_add_to_srs(body)
            return

        # ── New Drill (5-Pass) ──
        elif path == "/api/drill/advance":
            self._drill_advance_pass(body)
            return
        elif path == "/api/drill/complete":
            self._drill_complete(body)
            return

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

        # ── Podcasts ──
        elif path == "/api/library/podcast/generate":
            self._podcast_generate(body)

        # ── Neural Mapping ──
        elif path == "/api/neural/clone":
            self._neural_register_clone(body)

        # ── Acquisition Engine POST ──
        elif path == "/api/fragile/scan":
            result = run_fragility_scan()
            self._json(result)
        elif path == "/api/plan/block/complete":
            block_id = body.get("block_id", 0)
            actual = body.get("actual_data", {})
            record_block_completion(block_id, actual)
            self._json({"ok": True})
        elif path == "/api/plan/adjust":
            fatigue = body.get("fatigue_score", 50)
            plan = adjust_plan_mid_session(fatigue)
            self._json(plan)
        elif path == "/api/chunks/extract":
            src = body.get("source", "story")
            src_id = body.get("source_id", 0)
            if src == "story":
                count = extract_chunks_from_story(src_id)
            else:
                count = extract_chunks_from_podcast(src_id)
            rank_chunk_families()
            self._json({"extracted": count})
        elif path == "/api/chunks/seed":
            limit = body.get("limit", 10)
            chunks = get_next_chunks_for_srs(limit)
            added = add_chunks_to_queue(chunks)
            self._json({"seeded": added})

        # ── Speech Ladder POST ──
        elif path == "/api/speech/advance":
            result = advance_stage()
            self._json(result)
        elif path == "/api/speech/check-regression":
            result = check_regression()
            self._json(result)

        # ── Content Ladder POST ──
        elif path == "/api/content/classify":
            ct = body.get("content_type", "story")
            cid = body.get("content_id", 0)
            level = classify_content(ct, cid)
            self._json({"content_type": ct, "content_id": cid, "level": level})
        elif path == "/api/content/classify-all":
            results = classify_all_content()
            self._json(results)

        # ── Fatigue POST ──
        elif path == "/api/fatigue/reset":
            reset_fatigue_session()
            self._json({"ok": True})

        # ── Conversa (stage-scaffolded) ──
        elif path == "/api/conversa/start":
            self._conversa_start(body)
        elif path == "/api/conversa/turn":
            self._conversa_turn(body)
        elif path == "/api/conversa/end":
            self._conversa_end(body)

        else:
            self.send_error(404)

    # ── Podcast Endpoints ──────────────────────────────────

    def _podcast_list(self):
        podcasts = list_podcasts()
        self._json(podcasts)

    def _podcast_get(self, podcast_id):
        data = get_podcast(podcast_id)
        if data is None:
            self._json({"error": "not found"}, status=404)
            return
        self._json(data)

    def _podcast_generate(self, body):
        difficulty = body.get("difficulty", 80)
        focus_words = body.get("focus_words", [])
        podcast_data = generate_podcast(difficulty=difficulty, focus_words=focus_words or None)
        if not podcast_data:
            self._json({"error": "Erro ao gerar podcast"}, status=500)
            return
        podcast_id = save_podcast(podcast_data)
        podcast_data["id"] = podcast_id
        self._json(podcast_data)

    # ── Home Stats ─────────────────────────────────────────

    # ── Acquisition Engine Endpoints ─────────────────────

    def _dashboard(self):
        """Full dashboard data combining acquisition state, plan progress, and stats."""
        dist = get_state_distribution()
        tier = get_unlocked_tier()
        progress = tier_progress()
        current_pct = 0
        for t, label, mastered, total, pct in progress:
            if t == tier:
                current_pct = round(pct)
                break
        streak = get_streak()
        fragile = get_fragile_summary()
        plan_prog = get_plan_progress()

        acquired = dist.get("AUTOMATIC_CLEAN", 0) + dist.get("AUTOMATIC_NATIVE", 0) + dist.get("AVAILABLE_OUTPUT", 0)
        automatic = dist.get("AUTOMATIC_NATIVE", 0) + dist.get("AVAILABLE_OUTPUT", 0)

        # Phase B enrichment
        try:
            content_level = get_learner_level()
        except Exception:
            content_level = "P1"
        try:
            fatigue = check_fatigue()
        except Exception:
            fatigue = {"fatigue_score": 0, "recommendation": "start_session"}
        try:
            speech_stage = get_current_stage()
            speech_gates = evaluate_gates()
        except Exception:
            speech_stage = 1
            speech_gates = {}

        self._json({
            "acquisition_state": {
                "distribution": dist,
                "acquired_count": acquired,
                "automatic_count": automatic,
                "available_count": dist.get("AVAILABLE_OUTPUT", 0),
            },
            "today": plan_prog,
            "fragile_summary": fragile,
            "tier": {"current": tier, "mastery_pct": current_pct, "label": TIER_LABELS.get(tier, "")},
            "streak": streak,
            "content_level": content_level,
            "fatigue": fatigue,
            "speech": {"stage": speech_stage, "gates": speech_gates},
        })

    def _chunk_families(self, limit):
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM chunk_families ORDER BY composite_rank DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _speech_stage(self):
        try:
            stage = get_current_stage()
            gates = evaluate_gates()
            activities = get_activities_for_stage(stage)
            self._json({
                "stage": stage,
                "gates": gates,
                "activities": activities,
            })
        except Exception:
            self._json({"stage": 1, "gates": {}, "activities": []})

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

    # ── Dictionary Endpoints ──────────────────────────────

    def _dict_search(self, q):
        if not q:
            self._json({"results": [], "query": ""})
            return
        results = search_word(q)
        self._json({"results": results, "query": q})

    def _dict_word(self, word_id):
        try:
            data = get_full_word_data(word_id)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_add_to_srs(self, body):
        word_id = body.get("word_id")
        chunk = body.get("chunk", "")
        carrier = body.get("carrier", "")
        if not word_id or not chunk:
            self._json({"error": "word_id e chunk obrigatorios"}, status=400)
            return
        chunk_id = add_chunk(word_id, chunk, carrier, "dictionary")
        self._json({"chunk_id": chunk_id, "status": "adicionado" if chunk_id else "ja existe"})

    def _dict_history(self):
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM search_history ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
        self._json({"history": [dict(r) for r in rows]})

    # ── 5-Pass Drill Endpoints ─────────────────────────────

    def _drill_next_chunk(self):
        chunk = get_next_chunk()
        if chunk is None:
            # Cold start: auto-seed from word_bank Tier 1
            conn = get_conn()
            words = conn.execute(
                "SELECT id, word FROM word_bank WHERE difficulty_tier = 1 ORDER BY frequency_rank LIMIT 10"
            ).fetchall()
            conn.close()
            for w in words:
                carrier = build_carrier(w["word"])
                add_chunk(w["id"], w["word"], carrier, "corpus")
            chunk = get_next_chunk()

        if chunk is None:
            self._json({"error": "Nenhum chunk disponivel"}, status=404)
            return

        # Generate audio + image
        audio_file = generate_tts(chunk["carrier_sentence"])
        image_file = None
        try:
            image_file = generate_image(chunk["word"])
        except Exception:
            pass

        due_count = len(get_due_chunks())
        self._json({
            "chunk_id": chunk["id"],
            "word": chunk["word"],
            "word_id": chunk["word_id"],
            "target_chunk": chunk["target_chunk"],
            "carrier_sentence": chunk["carrier_sentence"],
            "current_pass": chunk["current_pass"],
            "audio_file": audio_file,
            "image_file": image_file,
            "tier": chunk["difficulty_tier"],
            "due_count": due_count,
        })

    def _drill_advance_pass(self, body):
        chunk_id = body.get("chunk_id")
        current_pass = body.get("current_pass", 1)
        if not chunk_id:
            self._json({"error": "chunk_id obrigatorio"}, status=400)
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
            self._json({"error": "chunk_id obrigatorio"}, status=400)
            return

        # Determine rating
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

        # Update automaticity state
        try:
            chunk_row = get_chunk_by_id(chunk_id)
            if chunk_row:
                update_state_after_review(
                    'chunk', chunk_id, rating, latency_ms or 0,
                    'clean', biometric,
                )
                if chunk_row['word_id']:
                    update_state_after_review(
                        'word', chunk_row['word_id'], rating, latency_ms or 0,
                        'clean', biometric,
                    )
        except Exception:
            pass  # never crash drill for state tracking

        # Record fatigue event
        try:
            record_review_event(latency_ms or 0, rating.value, retries)
        except Exception:
            pass

        rating_names = {
            Rating.Again: "De novo",
            Rating.Hard: "Difícil",
            Rating.Good: "Bom",
            Rating.Easy: "Fácil",
        }
        self._json({
            "rating": rating.value,
            "rating_name": rating_names.get(rating, ""),
            "new_mastery": mastery,
            "latency_downgraded": downgraded,
        })

    def _review_feed(self):
        chunks = get_review_feed()
        self._json({
            "chunks": [
                {
                    "chunk_id": c["id"],
                    "word": c["word"],
                    "target_chunk": c["target_chunk"],
                    "source": c["source"],
                    "current_pass": c["current_pass"],
                    "mastery_level": c["mastery_level"],
                }
                for c in chunks
            ]
        })

    # ── Legacy Drill Endpoints ─────────────────────────────

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

    # ── Conversa (Stage-Scaffolded) ─────────────────────────

    def _conversa_start(self, body):
        """Start a new stage-scaffolded conversa session.

        For stages 3-6, fetches known chunks at AUTOMATIC_CLEAN or above
        and injects 3-5 as vocabulary into the system prompt.  For stage 4+
        a specific prompt/question is generated based on the stage type.
        """
        try:
            stage = get_current_stage()
            stage_info = get_activities_for_stage(stage)
            topic = body.get("topic", "")
            recent_words = self._get_recent_words()

            # Build stage-appropriate system prompt
            base = (
                "Tu é um parceiro soteropolitano de Salvador. "
                "Conversa naturalmente em português baiano — usa oxe, vixe, massa, arretado. "
                "NUNCA use inglês. Respostas curtas, 2-3 frases máximo."
            )

            stage_instructions = {
                1: " O aprendiz está no nível Eco. Fala frases curtas e simples pra ele repetir. Máximo 5 palavras por frase.",
                2: " O aprendiz está no nível Troca de Chunk. Dá uma frase modelo e pede pra trocar um chunk. Ex: 'Diz a mesma frase mas troca X por Y.'",
                3: " O aprendiz está no nível Reconto Guiado. Conta uma mini-história (3 frases) e pede pra recontar com as próprias palavras.",
                4: " O aprendiz está no nível Expressão Guiada. Faz perguntas simples sobre o dia-a-dia pra ele responder usando chunks conhecidos.",
                5: " O aprendiz está no nível Semi-Livre. Conversa sobre um tópico definido mas deixa ele falar mais. Corrige sutilmente se precisar.",
                6: " O aprendiz está no nível Livre. Conversa natural sobre qualquer assunto. Não simplifica demais.",
            }

            system_prompt = base + stage_instructions.get(stage, stage_instructions[1])
            if recent_words:
                system_prompt += f" Tenta usar estas palavras: {', '.join(recent_words)}"

            # ── Stages 3-6: inject known chunks as vocabulary ──
            global _conversa_chunks_vocab
            _conversa_chunks_vocab = []
            chunks_used_list = []

            if stage >= 3:
                try:
                    known_items = get_items_in_state('AUTOMATIC_CLEAN', 'chunk', limit=20)
                    # Also pull from higher states
                    for higher_state in ('AUTOMATIC_NATIVE', 'AVAILABLE_OUTPUT'):
                        known_items.extend(get_items_in_state(higher_state, 'chunk', limit=10))

                    if known_items:
                        # Look up the actual chunk text from chunk_queue
                        conn = get_conn()
                        vocab_chunks = []
                        for item in known_items:
                            row = conn.execute(
                                "SELECT target_chunk FROM chunk_queue WHERE id = ?",
                                (item['item_id'],),
                            ).fetchone()
                            if row:
                                vocab_chunks.append(row['target_chunk'])
                        conn.close()

                        # Pick 3-5 random chunks
                        sample_size = min(max(3, len(vocab_chunks)), 5)
                        if vocab_chunks:
                            selected = random.sample(vocab_chunks, min(sample_size, len(vocab_chunks)))
                            _conversa_chunks_vocab = selected
                            chunks_used_list = selected
                            system_prompt += (
                                f" Vocabulário que o aprendiz já domina (tenta usar na conversa): "
                                f"{', '.join(selected)}"
                            )
                except Exception as e:
                    print(f"[Conversa Start] Chunk fetch warning: {e}")

            # ── Stage 4+: generate a specific prompt based on stage type ──
            prompt_data = None
            if stage >= 4:
                prompt_templates = {
                    4: [
                        "Pergunta pro aprendiz: 'O que tu fez hoje de manhã?'",
                        "Pergunta pro aprendiz: 'Como é teu final de semana em Salvador?'",
                        "Pergunta pro aprendiz: 'O que tu gosta de comer no almoço?'",
                        "Pergunta pro aprendiz: 'Qual teu lugar favorito em Salvador?'",
                    ],
                    5: [
                        "Tema pra conversa: vida noturna em Salvador",
                        "Tema pra conversa: comida baiana — acarajé, vatapá, moqueca",
                        "Tema pra conversa: praias da Bahia",
                        "Tema pra conversa: festas e Carnaval de Salvador",
                    ],
                    6: [
                        "Conversa livre — qualquer assunto que surgir",
                        "Conversa livre — deixa o aprendiz escolher o tema",
                    ],
                }
                templates = prompt_templates.get(stage, prompt_templates[6])
                prompt_data = random.choice(templates)
                if not topic:
                    topic = prompt_data

            # Store session in DB
            conn = get_conn()
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            today = datetime.utcnow().strftime("%Y-%m-%d")
            conn.execute(
                """INSERT INTO conversa_sessions
                   (date, speech_stage, mode, prompt_type, prompt_data, messages, chunks_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (today, stage, "stage_scaffolded", f"stage_{stage}",
                 prompt_data, json.dumps([]), json.dumps(chunks_used_list)),
            )
            session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            conn.close()

            # Store system prompt in global for this session
            global _conversa_history, _conversa_system_prompt, _conversa_session_id
            _conversa_history = []
            _conversa_system_prompt = system_prompt
            _conversa_session_id = session_id

            # Generate opening message
            import openai
            client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
            opener_prompt = "Começa a conversa com uma saudação curta baiana e inicia a atividade do nível."
            if topic:
                opener_prompt += f" Tema: {topic}"

            resp = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=150,
                temperature=0.8,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": opener_prompt},
                ],
            )
            reply = resp.choices[0].message.content.strip()
            _conversa_history.append({"role": "assistant", "content": reply})

            audio_fname = generate_tts(reply)

            self._json({
                "session_id": session_id,
                "stage": stage,
                "reply": reply,
                "audio_file": audio_fname,
                "chunks_vocab": _conversa_chunks_vocab,
            })
        except Exception as e:
            print(f"[Conversa Start] Error: {e}")
            self._json({"error": str(e), "stage": 1})

    def _conversa_turn(self, body):
        """Handle a turn in the stage-scaffolded conversa.

        Tracks which vocabulary chunks the learner uses and encourages
        full-chunk responses for stages 2+ when messages are too short.
        """
        global _conversa_history, _conversa_system_prompt, _conversa_chunks_vocab
        message = body.get("message", "").strip()
        if not message:
            self._json({"reply": "Fala alguma coisa, parceiro!", "audio_file": None})
            return

        try:
            # ── Track which vocab chunks the learner used ──
            chunks_hit = []
            if _conversa_chunks_vocab:
                msg_lower = message.lower()
                for chunk in _conversa_chunks_vocab:
                    if chunk.lower() in msg_lower:
                        chunks_hit.append(chunk)

            # ── Stage 2+: encourage full chunks if message is very short ──
            stage = get_current_stage()
            encouragement = ""
            word_count = len(message.split())
            if stage >= 2 and word_count < 3:
                encouragement = (
                    " (O aprendiz respondeu com poucas palavras. "
                    "Incentiva ele a usar frases completas com chunks. "
                    "Dá um exemplo curto pra ele repetir ou completar.)"
                )

            _conversa_history.append({"role": "user", "content": message})
            if len(_conversa_history) > 20:
                _conversa_history = _conversa_history[-20:]

            system_prompt = getattr(self, '_conversa_system_prompt', None) or _conversa_system_prompt
            # Inject encouragement into a transient system message if needed
            effective_system = system_prompt + encouragement
            messages = [{"role": "system", "content": effective_system}] + _conversa_history

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

            audio_fname = generate_tts(reply)

            # Record fatigue event (conversa is low-stress but still active)
            record_review_event(latency_ms=0, rating=3, replays=0)

            self._json({
                "reply": reply,
                "audio_file": audio_fname,
                "chunks_hit": chunks_hit,
            })
        except Exception as e:
            print(f"[Conversa Turn] Error: {e}")
            self._json({"reply": "Oxe, deu erro aqui. Tenta de novo.", "audio_file": None})

    def _conversa_end(self, body):
        """End a conversa session with full post-session chunk extraction.

        1. Collects all learner messages from the session.
        2. Calls chunk_engine.extract_chunks_from_text() to find new chunks.
        3. Seeds each extracted chunk into the SRS queue via srs_engine.add_chunk().
        4. Updates the conversa_sessions row with post_extraction and chunks_introduced.
        5. Returns a summary: turns, chunks extracted, vocab chunks used.
        """
        global _conversa_history, _conversa_session_id, _conversa_chunks_vocab
        session_id = body.get("session_id") or getattr(self.__class__, '_conversa_session_id', None) or globals().get('_conversa_session_id')

        try:
            # ── 1. Collect learner messages ──
            learner_msgs = [m["content"] for m in _conversa_history if m["role"] == "user"]
            turn_count = len(learner_msgs)

            # ── Save full message history to DB ──
            if session_id and _conversa_history:
                conn = get_conn()
                conn.execute(
                    "UPDATE conversa_sessions SET messages = ?, duration_seconds = ? WHERE id = ?",
                    (json.dumps(_conversa_history), body.get("duration_seconds", 0), session_id),
                )
                conn.commit()
                conn.close()

            # ── 2. Track which vocabulary chunks were actually used ──
            vocab_chunks_used = []
            if _conversa_chunks_vocab and learner_msgs:
                combined_lower = " ".join(learner_msgs).lower()
                for chunk in _conversa_chunks_vocab:
                    if chunk.lower() in combined_lower:
                        vocab_chunks_used.append(chunk)

            # ── 3. Extract new chunks from learner text via GPT-4o ──
            extracted_chunks = []
            chunks_introduced = []
            if learner_msgs:
                full_text = " ".join(learner_msgs)
                try:
                    raw_chunks = extract_chunks_from_text(full_text, min_words=2, max_words=5)
                    extracted_chunks = raw_chunks if raw_chunks else []
                except Exception as e:
                    print(f"[Conversa End] Chunk extraction error: {e}")
                    extracted_chunks = []

                # ── 4. Seed extracted chunks into SRS queue ──
                conn = get_conn()
                for chunk_data in extracted_chunks:
                    chunk_text = chunk_data.get("chunk", "")
                    root_form = chunk_data.get("root_form", chunk_text)
                    if not chunk_text:
                        continue

                    # Try to find a matching word_id from root_form words
                    word_id = None
                    for token in root_form.split():
                        row = conn.execute(
                            "SELECT id FROM word_bank WHERE word = ? LIMIT 1", (token,)
                        ).fetchone()
                        if row:
                            word_id = row["id"]
                            break

                    carrier = f"Oxe, {chunk_text} — é mermo!"
                    chunk_id = add_chunk(word_id, chunk_text, carrier, "conversation")
                    if chunk_id is not None:
                        chunks_introduced.append(chunk_text)
                conn.close()

            # ── 5. Update the conversa_sessions row ──
            post_extraction = {
                "raw_chunks": [c.get("chunk", "") for c in extracted_chunks],
                "chunks_introduced": chunks_introduced,
                "vocab_chunks_used": vocab_chunks_used,
            }
            if session_id:
                conn = get_conn()
                conn.execute(
                    """UPDATE conversa_sessions
                       SET post_extraction = ?, chunks_introduced = ?
                       WHERE id = ?""",
                    (json.dumps(post_extraction), json.dumps(chunks_introduced), session_id),
                )
                conn.commit()
                conn.close()

            # ── Clean up session globals ──
            history_copy = list(_conversa_history)
            _conversa_history = []
            _conversa_chunks_vocab = []

            self._json({
                "ok": True,
                "session_id": session_id,
                "turns": turn_count,
                "chunks_extracted": len(extracted_chunks),
                "chunks_introduced": len(chunks_introduced),
                "chunks_introduced_list": chunks_introduced,
                "vocab_chunks_used": vocab_chunks_used,
                "vocab_chunks_used_count": len(vocab_chunks_used),
            })
        except Exception as e:
            print(f"[Conversa End] Error: {e}")
            self._json({"ok": False, "error": str(e)})

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

    # ── Neural Mapping handlers ─────────────────────────────

    def _neural_status(self):
        voice_id = ensure_clone_exists()
        self._json({"clone_exists": voice_id is not None, "voice_id": voice_id})

    def _neural_register_clone(self, body):
        voice_name = body.get("voice_name", "")
        voice_id = body.get("voice_id", "")
        if not voice_name or not voice_id:
            self._json({"error": "voice_name and voice_id required"}, status=400)
            return
        row_id = register_clone(voice_name, voice_id)
        self._json({"ok": True, "id": row_id})

    def _neural_golden(self, word_id):
        # Find native audio path from chunk_queue
        conn = get_conn()
        row = conn.execute(
            "SELECT native_audio_path FROM chunk_queue WHERE word_id = ? LIMIT 1",
            (word_id,),
        ).fetchone()
        conn.close()
        native_path = row["native_audio_path"] if row and row["native_audio_path"] else None
        if native_path and not Path(native_path).is_absolute():
            native_path = str(AUDIO_DIR / native_path)
        if not native_path:
            self._json({"golden_audio": None, "error": "no native audio for word"})
            return
        filename = get_or_generate_golden(word_id, native_path)
        self._json({"golden_audio": filename})

    # ── Helpers ────────────────────────────────────────────────

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())

    def _json(self, data, status=200):
        self.send_response(status)
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
    migrate_v2()
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
