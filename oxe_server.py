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

import gzip
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
import threading
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
from content_router import (
    find_content_for_chunks, get_recently_drilled_chunks,
    get_reencounter_queue, log_reencounter, get_reencounter_stats,
)
from conversa_analyzer import (
    analyze_conversation, generate_correction_drills,
    get_conversation_analysis, get_analysis_history,
)
from listening_layers import (
    LISTENING_LAYERS, get_listening_drill,
    advance_listening_layer, get_layer_audios,
)
from sentence_assembly import (
    get_assembly_challenge, check_assembly, get_assembly_stats,
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
    get_conjugation, get_synonyms, get_word_chunks, get_audio_for_word,
    get_definition_cached, get_examples_cached, get_pronunciation_cached,
    get_expressions_cached, get_conjugation_cached, get_synonyms_cached,
    get_word_chunks_cached,
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

# Time-based caches for expensive endpoints (10 second TTL)
_dashboard_cache = {"data": None, "ts": 0}
_dashboard_cache_lock = threading.Lock()
_home_stats_cache = {"data": None, "ts": 0}
_home_stats_cache_lock = threading.Lock()

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
  @keyframes fadeIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column;
    -webkit-user-select: none; user-select: none; padding-bottom: 76px;
  }

  /* ── Header ── */
  .header {
    padding: 20px 20px 0; display: flex; justify-content: space-between; align-items: center;
  }
  .brand { font-size: 1.6em; font-weight: 800; letter-spacing: -1px;
    background: linear-gradient(135deg, #4F7BEF, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .header-meta { font-size: 0.72em; color: #525263; display: flex; gap: 10px; align-items: center; }
  .header-meta b { color: #7a7a8e; }

  .page { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 20px; }

  /* ── Search bar (hero) ── */
  .search-wrap { margin-bottom: 28px; animation: fadeIn 0.3s ease-out; }
  .search-box {
    display: flex; align-items: center; gap: 10px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px; padding: 14px 16px; transition: border-color 0.2s;
  }
  .search-box:focus-within { border-color: rgba(79,123,239,0.5); }
  .search-box svg { flex-shrink: 0; color: #525263; }
  .search-box input {
    flex: 1; background: none; border: none; outline: none; color: #fafafa;
    font-size: 1em; font-family: inherit;
  }
  .search-box input::placeholder { color: #3a3a4a; }

  /* ── Word of Day ── */
  .wod { margin-bottom: 28px; animation: fadeIn 0.3s ease-out 0.05s both; display: none; }
  .wod-label { font-size: 0.65em; color: #7C5CFC; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 700; margin-bottom: 8px; }
  .wod-word { font-size: 1.4em; font-weight: 800; margin-bottom: 4px; }
  .wod-sentence { font-size: 0.85em; color: #7a7a8e; font-style: italic; line-height: 1.4; }

  /* ── Train button ── */
  .train-btn {
    display: flex; align-items: center; justify-content: space-between;
    width: 100%; padding: 18px 20px; margin-bottom: 20px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    border: none; border-radius: 14px; font-size: 1em; font-weight: 700;
    text-decoration: none; -webkit-tap-highlight-color: transparent;
    transition: transform 0.15s; animation: fadeIn 0.3s ease-out 0.1s both;
  }
  .train-btn:active { transform: scale(0.97); }
  .train-btn .due-count {
    background: rgba(255,255,255,0.2); padding: 4px 12px; border-radius: 10px;
    font-size: 0.85em; font-weight: 600;
  }

  /* ── Nav list ── */
  .nav-list { margin-bottom: 24px; animation: fadeIn 0.3s ease-out 0.15s both; }
  .nav-link {
    display: flex; align-items: center; gap: 14px;
    padding: 15px 0; text-decoration: none; color: #fafafa;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    -webkit-tap-highlight-color: transparent;
  }
  .nav-link:last-child { border-bottom: none; }
  .nav-link:active { opacity: 0.7; }
  .nav-ico {
    width: 36px; height: 36px; border-radius: 10px; display: flex;
    align-items: center; justify-content: center; font-size: 1.1em; flex-shrink: 0;
  }
  .nav-text { flex: 1; }
  .nav-title { font-size: 0.9em; font-weight: 600; }
  .nav-sub { font-size: 0.7em; color: #525263; margin-top: 1px; }
  .nav-arrow { color: #3a3a4a; font-size: 0.8em; }

  /* ── Progress (minimal) ── */
  .progress-row {
    display: flex; align-items: center; gap: 12px; padding: 14px 0;
    animation: fadeIn 0.3s ease-out 0.2s both;
  }
  .progress-bar-wrap {
    flex: 1; height: 6px; border-radius: 3px; background: rgba(255,255,255,0.06); overflow: hidden;
  }
  .progress-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, #3B82F6, #7C5CFC); transition: width 0.6s; }
  .progress-label { font-size: 0.7em; color: #525263; white-space: nowrap; }
  .progress-label b { color: #a78bfa; }

  /* ── Insight row ── */
  .insight-row {
    display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px;
    animation: fadeIn 0.3s ease-out 0.12s both;
  }
  .insight-pill {
    flex: 1 1 auto; min-width: 0; padding: 10px 12px; border-radius: 12px;
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.05);
    text-align: center;
  }
  .insight-val { font-size: 0.95em; font-weight: 700; font-variant-numeric: tabular-nums; }
  .insight-lbl { font-size: 0.6em; color: #525263; text-transform: uppercase; margin-top: 2px; }
  .insight-val.blue { color: #60a5fa; }
  .insight-val.green { color: #34d399; }
  .insight-val.purple { color: #a78bfa; }
  .insight-val.yellow { color: #facc15; }
  .rec-banner {
    padding: 12px 16px; border-radius: 12px; margin-bottom: 16px;
    background: rgba(59,130,246,0.06); border: 1px solid rgba(59,130,246,0.1);
    font-size: 0.8em; color: #60a5fa; display: none;
    animation: fadeIn 0.3s ease-out 0.18s both;
  }
  .rec-banner b { font-weight: 700; }
  .lock-pill {
    display: inline-block; padding: 3px 10px; border-radius: 8px;
    font-size: 0.6em; font-weight: 700; text-transform: uppercase;
    background: rgba(248,113,113,0.1); color: #f87171; border: 1px solid rgba(248,113,113,0.15);
  }
</style>
</head><body>

<div class="header">
  <div class="brand">Oxe</div>
  <div class="header-meta">
    <span>T<b id="h-tier">1</b></span>
    <span><b id="h-streak">0</b> dias</span>
    <span class="lock-pill" id="es-lock">ES trancado</span>
  </div>
</div>

<div class="page">

  <!-- Search (hero element) -->
  <div class="search-wrap">
    <div class="search-box" onclick="window.location='/search'">
      <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="8" r="6"/><line x1="12.5" y1="12.5" x2="17" y2="17"/></svg>
      <input type="text" placeholder="Buscar palavra..." readonly>
    </div>
  </div>

  <!-- Word of Day -->
  <div class="wod" id="wod">
    <div class="wod-label">Palavra do Dia</div>
    <div class="wod-word" id="wod-word"></div>
    <div class="wod-sentence" id="wod-sentence"></div>
  </div>

  <!-- Train button -->
  <a href="/drill" class="train-btn">
    <span>Treinar</span>
    <span class="due-count" id="due-count">0 pendentes</span>
  </a>

  <!-- Insight row -->
  <div class="insight-row">
    <div class="insight-pill">
      <div class="insight-val blue" id="i-hours">0h</div>
      <div class="insight-lbl">Horas Efetivas</div>
    </div>
    <div class="insight-pill">
      <div class="insight-val green" id="i-auto">0</div>
      <div class="insight-lbl">Automaticas</div>
    </div>
    <div class="insight-pill">
      <div class="insight-val purple" id="i-milestone">--</div>
      <div class="insight-lbl">Proxima Meta</div>
    </div>
  </div>

  <!-- Recommended session -->
  <div class="rec-banner" id="rec-banner"></div>

  <!-- Navigation -->
  <div class="nav-list">
    <a href="/library" class="nav-link">
      <div class="nav-ico" style="background:rgba(59,130,246,0.10)">&#x1f4d6;</div>
      <div class="nav-text">
        <div class="nav-title">Biblioteca</div>
        <div class="nav-sub" id="nav-stories">Historias e podcasts</div>
      </div>
      <div class="nav-arrow">&#x203a;</div>
    </a>
    <a href="/conversa" class="nav-link">
      <div class="nav-ico" style="background:rgba(124,92,252,0.10)">&#x1f4ac;</div>
      <div class="nav-text">
        <div class="nav-title">Conversa</div>
        <div class="nav-sub">Pratica de fala guiada</div>
      </div>
      <div class="nav-arrow">&#x203a;</div>
    </a>
    <a href="/assembly" class="nav-link">
      <div class="nav-ico" style="background:rgba(52,211,153,0.10)">&#x1f9e9;</div>
      <div class="nav-text">
        <div class="nav-title">Montar Frases</div>
        <div class="nav-sub">Monte frases com chunks</div>
      </div>
      <div class="nav-arrow">&#x203a;</div>
    </a>
    <a href="/plan" class="nav-link">
      <div class="nav-ico" style="background:rgba(250,204,21,0.10)">&#x1f4cb;</div>
      <div class="nav-text">
        <div class="nav-title">Plano do Dia</div>
        <div class="nav-sub" id="nav-plan">Blocos de estudo</div>
      </div>
      <div class="nav-arrow">&#x203a;</div>
    </a>
    <a href="/speech" class="nav-link">
      <div class="nav-ico" style="background:rgba(248,113,113,0.10)">&#x1f3a4;</div>
      <div class="nav-text">
        <div class="nav-title">Escada da Fala</div>
        <div class="nav-sub" id="nav-speech">Estagio 1 - Eco</div>
      </div>
      <div class="nav-arrow">&#x203a;</div>
    </a>
    <a href="/chunks" class="nav-link">
      <div class="nav-ico" style="background:rgba(167,139,250,0.10)">&#x1f517;</div>
      <div class="nav-text">
        <div class="nav-title">Chunks</div>
        <div class="nav-sub">Familias de expressoes</div>
      </div>
      <div class="nav-arrow">&#x203a;</div>
    </a>
  </div>

  <!-- Progress bar (one line) -->
  <div class="progress-row">
    <div class="progress-bar-wrap">
      <div class="progress-fill" id="prog-fill" style="width:0%"></div>
    </div>
    <div class="progress-label"><b id="prog-pct">0%</b> adquirido</div>
  </div>

</div>

{tab_bar}

<script>
var SPEECH_NAMES = {1:'Eco',2:'Troca',3:'Reconto',4:'Expressao',5:'Semi-Livre',6:'Livre'};

fetch('/api/home-stats').then(function(r){return r.json()}).then(function(d){
  document.getElementById('h-streak').textContent = d.streak||0;
  document.getElementById('h-tier').textContent = d.tier||1;
  var due = d.due||0;
  document.getElementById('due-count').textContent = due + ' pendentes';
  if(d.story_count) document.getElementById('nav-stories').textContent = d.story_count + ' historias';
  if(d.word_of_day){
    document.getElementById('wod-word').textContent = d.word_of_day.text||'';
    document.getElementById('wod-sentence').textContent = d.word_of_day.sentence||'';
    document.getElementById('wod').style.display = 'block';
  }
  if(d.mastery_pct !== undefined){
    document.getElementById('prog-pct').textContent = d.mastery_pct + '%';
    document.getElementById('prog-fill').style.width = d.mastery_pct + '%';
  }
}).catch(function(){});

// Daily stats for effective hours
fetch('/api/daily-stats').then(function(r){return r.json()}).then(function(d){
  var t = d.today||{};
  var mins = t.minutes||0;
  var hrs = (mins/60).toFixed(1);
  document.getElementById('i-hours').textContent = hrs + 'h';
}).catch(function(){});

requestAnimationFrame(function(){
  fetch('/api/dashboard').then(function(r){return r.ok?r.json():null}).then(function(d){
    if(!d) return;
    var tier = d.tier||{};
    document.getElementById('h-tier').textContent = tier.current||1;
    var pct = tier.mastery_pct||0;
    document.getElementById('prog-pct').textContent = pct + '%';
    document.getElementById('prog-fill').style.width = pct + '%';
    var sp = d.speech||{};
    var stg = sp.stage||1;
    document.getElementById('nav-speech').textContent = 'Estagio ' + stg + ' - ' + (SPEECH_NAMES[stg]||'');
    var plan = d.today||{};
    if(plan.completed_pct) document.getElementById('nav-plan').textContent = Math.round(plan.completed_pct) + '% completo';

    // Automatic count
    var acq = d.acquisition_state||{};
    var auto = (acq.automatic_count||0);
    document.getElementById('i-auto').textContent = auto;

    // Next milestone
    var total = acq.acquired_count||0;
    var milestones = [10,25,50,100,250,500,1000,2500,5000];
    var next = '--';
    for(var i=0;i<milestones.length;i++){
      if(total < milestones[i]){ next = milestones[i]; break; }
    }
    document.getElementById('i-milestone').textContent = next === '--' ? next : next + ' acq';

    // Recommended session
    var fat = d.fatigue||{};
    var rec = fat.recommendation||'';
    var banner = document.getElementById('rec-banner');
    var REC_MSGS = {
      'start_session': 'Pronto pra treinar',
      'continue': 'Continue treinando',
      'switch_mode': 'Troca pra escuta passiva',
      'take_break': 'Hora de uma pausa',
      'end_session': 'Melhor parar por hoje'
    };
    if(rec && REC_MSGS[rec]){
      banner.innerHTML = '<b>Agora:</b> ' + REC_MSGS[rec];
      banner.style.display = 'block';
    }

    // Spanish lock — unlocks at AUTOMATIC_NATIVE >= 500 AND speech stage >= 5
    var nativeCount = (acq.distribution||{}).AUTOMATIC_NATIVE||0;
    var outputCount = (acq.distribution||{}).AVAILABLE_OUTPUT||0;
    var esReady = (nativeCount + outputCount) >= 500 && stg >= 5;
    var lockEl = document.getElementById('es-lock');
    if(esReady){
      lockEl.textContent = 'ES liberado';
      lockEl.style.background = 'rgba(52,211,153,0.1)';
      lockEl.style.color = '#34d399';
      lockEl.style.borderColor = 'rgba(52,211,153,0.15)';
    }
  }).catch(function(){});
});
</script>
</body></html>"""


# ── Speech Ladder HTML ────────────────────────────────────────

SPEECH_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Escada da Fala</title>
<style>
  @keyframes fadeIn { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
  @keyframes glowPulse { 0%,100%{box-shadow:0 0 8px rgba(52,211,153,0.4)} 50%{box-shadow:0 0 24px rgba(52,211,153,0.8)} }
  @keyframes celebratePop { 0%{transform:scale(0.5);opacity:0} 50%{transform:scale(1.15)} 100%{transform:scale(1);opacity:1} }
  @keyframes confetti { 0%{transform:translateY(0) rotate(0)} 100%{transform:translateY(80vh) rotate(720deg);opacity:0} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column;
    -webkit-user-select: none; user-select: none;
    padding-bottom: 76px;
  }

  /* ── Top Bar ── */
  .topbar {
    padding: 16px 20px 14px; display: flex; align-items: center; gap: 14px;
    position: sticky; top: 0; z-index: 10; background: #0a0a0b;
    border-bottom: 2px solid transparent;
    border-image: linear-gradient(90deg, #3B82F6, #7C5CFC) 1;
  }
  .back-btn {
    width: 36px; height: 36px; border-radius: 12px; border: none;
    background: rgba(255,255,255,0.06); color: #fafafa; font-size: 1.2em;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
    text-decoration: none;
  }
  .back-btn:active { transform: scale(0.92); }
  .topbar-title {
    font-size: 1.3em; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #4F7BEF, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }

  /* ── Page ── */
  .page { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 0 20px 24px; }

  /* ── Regression Banner ── */
  .regression-banner {
    display: none; padding: 12px 16px; margin: 12px 0;
    background: rgba(248,113,113,0.12); border: 1px solid rgba(248,113,113,0.25);
    border-radius: 14px; font-size: 0.82em; color: #f87171; font-weight: 600;
    text-align: center; animation: fadeIn 0.4s ease-out;
  }

  /* ── Celebration Overlay ── */
  .celebration {
    display: none; position: fixed; inset: 0; z-index: 200;
    background: rgba(10,10,11,0.85); backdrop-filter: blur(12px);
    flex-direction: column; align-items: center; justify-content: center; gap: 16px;
  }
  .celebration.show { display: flex; }
  .celebration .emoji { font-size: 4em; animation: celebratePop 0.5s ease-out; }
  .celebration .msg { font-size: 1.3em; font-weight: 800; color: #34d399; animation: celebratePop 0.5s ease-out 0.1s both; }
  .celebration .sub { font-size: 0.9em; color: #7a7a8e; animation: celebratePop 0.5s ease-out 0.2s both; }
  .confetti-piece {
    position: fixed; top: -20px; width: 10px; height: 10px; border-radius: 2px;
    animation: confetti 2.5s ease-in forwards; z-index: 201;
  }

  /* ── Hero Card ── */
  .hero-card {
    background: rgba(255,255,255,0.03); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.06), 0 4px 12px rgba(0,0,0,0.3);
    border-radius: 20px; padding: 28px 24px; margin: 16px 0;
    animation: fadeIn 0.4s ease-out; text-align: center;
  }
  .stage-circle {
    width: 80px; height: 80px; border-radius: 50%; margin: 0 auto 16px;
    display: flex; align-items: center; justify-content: center;
    font-size: 2em; font-weight: 900; color: #fafafa;
    background: rgba(255,255,255,0.04);
    border: 3px solid transparent; position: relative;
  }
  .stage-circle::before {
    content: ''; position: absolute; inset: -4px; border-radius: 50%;
    padding: 3px; background: var(--stage-gradient);
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude;
  }
  .hero-name { font-size: 1.3em; font-weight: 800; margin-bottom: 4px; }
  .hero-desc { font-size: 0.85em; color: #7a7a8e; margin-bottom: 16px; }
  .activity-pills { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-bottom: 20px; }
  .pill {
    padding: 5px 14px; border-radius: 20px; font-size: 0.68em; font-weight: 600;
    background: rgba(255,255,255,0.06); color: #a0a0b0;
  }
  .btn-conversa {
    display: inline-flex; align-items: center; gap: 8px; padding: 12px 28px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    border: none; border-radius: 14px; font-size: 0.9em; font-weight: 700;
    text-decoration: none; -webkit-tap-highlight-color: transparent;
    transition: transform 0.15s; box-shadow: 0 2px 12px rgba(59,130,246,0.3);
  }
  .btn-conversa:active { transform: scale(0.96); }

  /* ── Gate Progress ── */
  .gate-section {
    background: rgba(255,255,255,0.03); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.06), 0 4px 12px rgba(0,0,0,0.3);
    border-radius: 20px; padding: 24px; margin-bottom: 16px;
    animation: fadeIn 0.4s ease-out 0.1s both;
  }
  .gate-title { font-size: 0.78em; font-weight: 700; color: #60a5fa; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 16px; }
  .criterion-row { margin-bottom: 14px; }
  .criterion-label { font-size: 0.78em; color: #a0a0b0; margin-bottom: 5px; display: flex; justify-content: space-between; }
  .criterion-nums { font-weight: 700; color: #fafafa; }
  .progress-track {
    height: 10px; border-radius: 5px; background: rgba(255,255,255,0.06); overflow: hidden;
  }
  .progress-fill {
    height: 100%; border-radius: 5px; transition: width 0.6s ease; min-width: 0;
  }
  .btn-advance {
    display: none; width: 100%; padding: 14px; margin-top: 8px;
    background: linear-gradient(135deg, #34d399, #10b981); color: #fff;
    border: none; border-radius: 14px; font-size: 1em; font-weight: 800;
    cursor: pointer; animation: glowPulse 2s infinite;
    -webkit-tap-highlight-color: transparent;
  }
  .btn-advance:active { transform: scale(0.97); }

  /* ── Ladder Visualization ── */
  .ladder-section {
    margin: 16px 0; animation: fadeIn 0.4s ease-out 0.2s both;
  }
  .ladder-title { font-size: 0.78em; font-weight: 700; color: #60a5fa; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 16px; padding-left: 4px; }
  .ladder {
    position: relative; padding-left: 36px;
  }
  .ladder::before {
    content: ''; position: absolute; left: 14px; top: 0; bottom: 0; width: 2px;
    background: rgba(255,255,255,0.08);
  }
  .ladder-node {
    position: relative; margin-bottom: 12px; padding: 14px 18px;
    background: rgba(255,255,255,0.03); border-radius: 16px;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.04);
    transition: all 0.3s;
  }
  .ladder-node.current {
    background: rgba(255,255,255,0.06);
    box-shadow: 0 0 0 1px rgba(255,255,255,0.1), 0 4px 16px rgba(0,0,0,0.3);
  }
  .ladder-node.completed { opacity: 0.7; }
  .ladder-node.locked { opacity: 0.35; }
  .ladder-dot {
    position: absolute; left: -29px; top: 18px; width: 12px; height: 12px;
    border-radius: 50%; border: 2px solid rgba(255,255,255,0.15);
    background: #0a0a0b;
  }
  .ladder-node.completed .ladder-dot { background: #34d399; border-color: #34d399; }
  .ladder-node.current .ladder-dot { border-color: var(--stage-color); background: var(--stage-color); box-shadow: 0 0 8px var(--stage-color); }
  .ladder-node.locked .ladder-dot { background: #1a1a1e; border-color: rgba(255,255,255,0.08); }
  .node-header { display: flex; align-items: center; gap: 10px; }
  .node-num { font-size: 0.9em; font-weight: 800; }
  .node-name { font-size: 0.88em; font-weight: 700; }
  .node-desc { font-size: 0.72em; color: #7a7a8e; margin-top: 4px; }
  .node-icon { margin-left: auto; font-size: 0.9em; }
  .node-gate-info {
    display: none; margin-top: 10px; padding-top: 10px;
    border-top: 1px solid rgba(255,255,255,0.06); font-size: 0.72em; color: #7a7a8e;
  }
  .ladder-node.current .node-gate-info { display: block; }
  .ladder-node.locked .node-gate-info.tapped { display: block; }
</style>
</head><body>

<!-- Celebration Overlay -->
<div class="celebration" id="celebration">
  <div class="emoji">&#x1F389;</div>
  <div class="msg" id="celeb-msg">Avancou!</div>
  <div class="sub" id="celeb-sub"></div>
</div>

<div class="topbar">
  <a href="/" class="back-btn">&#x2190;</a>
  <div class="topbar-title">Escada da Fala</div>
</div>

<div class="page">
  <!-- Regression Banner -->
  <div class="regression-banner" id="regression-banner">
    &#x26A0;&#xFE0F; Regressao detectada — pratique mais pra manter o nivel!
  </div>

  <!-- Hero Card -->
  <div class="hero-card" id="hero-card">
    <div class="stage-circle" id="stage-circle" style="--stage-gradient: linear-gradient(135deg, #3B82F6, #60a5fa)">
      <span id="hero-num">1</span>
    </div>
    <div class="hero-name" id="hero-name">Eco</div>
    <div class="hero-desc" id="hero-desc">Repete exatamente o que ouve</div>
    <div class="activity-pills" id="activity-pills"></div>
    <a href="/conversa" class="btn-conversa">&#x1F3A4; Ir pra Conversa</a>
  </div>

  <!-- Gate Progress -->
  <div class="gate-section" id="gate-section">
    <div class="gate-title">Portao de Saida</div>
    <div id="gate-criteria"></div>
    <button class="btn-advance" id="btn-advance" onclick="doAdvance()">&#x1F31F; Avancar!</button>
  </div>

  <!-- Ladder -->
  <div class="ladder-section">
    <div class="ladder-title">Todas as Etapas</div>
    <div class="ladder" id="ladder"></div>
  </div>
</div>

{tab_bar}

<script>
var STAGES = [
  {n:1, name:'Eco', desc:'Repete exatamente o que ouve', color:'#3B82F6',
   gate_labels:{min_EFFORTFUL_AUDIO:'Items Esforco Auditivo', min_biometric_avg:'Biometria Media'}},
  {n:2, name:'Troca de Chunk', desc:'Substitui um chunk na frase modelo', color:'#22d3ee',
   gate_labels:{min_AUTOMATIC_CLEAN:'Items Automaticos Limpos', max_avg_latency_ms:'Latencia Media (ms)', min_shadow_good_pct:'% Shadowing Bom'}},
  {n:3, name:'Reconto Guiado', desc:'Reconta uma historia com prompts visuais', color:'#34d399',
   gate_labels:{min_AUTOMATIC_CLEAN:'Items Automaticos Limpos', min_biometric_avg:'Biometria Media'}},
  {n:4, name:'Expressao Guiada', desc:'Responde perguntas usando chunks conhecidos', color:'#facc15',
   gate_labels:{min_AUTOMATIC_NATIVE:'Items Automaticos Nativos', min_biometric_avg:'Biometria Media', min_output_success:'% Sucesso Output'}},
  {n:5, name:'Semi-Livre', desc:'Conversa com topico definido', color:'#fb923c',
   gate_labels:{min_AUTOMATIC_NATIVE:'Items Automaticos Nativos', min_AVAILABLE_OUTPUT:'Items Output Disponivel', min_biometric_avg:'Biometria Media'}},
  {n:6, name:'Livre', desc:'Conversa livre, qualquer topico', color:'#7C5CFC',
   gate_labels:{min_AVAILABLE_OUTPUT:'Items Output Disponivel', min_biometric_avg:'Biometria Media'}}
];

var currentStage = 1;
var gateData = {};

function stageInfo(n) { return STAGES[n-1] || STAGES[0]; }

function renderHero(stage, activities) {
  var s = stageInfo(stage);
  var circle = document.getElementById('stage-circle');
  circle.style.setProperty('--stage-gradient', 'linear-gradient(135deg, '+s.color+', '+s.color+'88)');
  document.getElementById('hero-num').textContent = stage;
  document.getElementById('hero-name').textContent = s.name;
  document.getElementById('hero-desc').textContent = s.desc;
  var pills = document.getElementById('activity-pills');
  pills.innerHTML = '';
  (activities||[]).forEach(function(a) {
    var pill = document.createElement('span');
    pill.className = 'pill';
    pill.textContent = a.replace(/_/g,' ');
    pills.appendChild(pill);
  });
}

function criterionLabel(key, stage) {
  var s = stageInfo(stage);
  return (s.gate_labels && s.gate_labels[key]) || key.replace(/^(min_|max_)/,'').replace(/_/g,' ');
}

function progressColor(pct, stageColor) {
  if (pct >= 100) return '#34d399';
  if (pct < 30) return '#f87171';
  return stageColor;
}

function renderGate(gates) {
  gateData = gates;
  var criteria = gates.criteria || {};
  var container = document.getElementById('gate-criteria');
  container.innerHTML = '';
  var allMet = true;
  var s = stageInfo(gates.current_stage || currentStage);
  var keys = Object.keys(criteria);

  keys.forEach(function(key) {
    var c = criteria[key];
    var required = c.required;
    var actual = c.actual;
    var met = c.met;
    if (!met) allMet = false;

    // For latency, invert: lower is better
    var isLatency = key.indexOf('latency') >= 0 || key.indexOf('max_') === 0;
    var pct;
    if (isLatency) {
      pct = actual <= 0 ? 0 : (required / actual) * 100;
    } else {
      pct = required <= 0 ? 100 : (actual / required) * 100;
    }
    pct = Math.min(pct, 100);
    pct = Math.max(pct, 0);

    var color = progressColor(pct, s.color);
    var label = criterionLabel(key, gates.current_stage || currentStage);

    // Format display values
    var displayActual = typeof actual === 'number' ? (actual % 1 !== 0 ? actual.toFixed(1) : actual) : actual;
    var displayRequired = typeof required === 'number' ? (required % 1 !== 0 ? required.toFixed(1) : required) : required;

    var row = document.createElement('div');
    row.className = 'criterion-row';
    row.innerHTML =
      '<div class="criterion-label"><span>'+label+'</span><span class="criterion-nums">'
      +displayActual+' / '+displayRequired+'</span></div>'
      +'<div class="progress-track"><div class="progress-fill" style="width:0%;background:'+color+'"></div></div>';
    container.appendChild(row);

    // Animate fill
    setTimeout(function() {
      row.querySelector('.progress-fill').style.width = pct+'%';
    }, 50);
  });

  var advBtn = document.getElementById('btn-advance');
  if (allMet && keys.length > 0) {
    advBtn.style.display = 'block';
  } else {
    advBtn.style.display = 'none';
  }
}

function renderLadder(stage, gates) {
  var ladder = document.getElementById('ladder');
  ladder.innerHTML = '';
  // Render stages in reverse order (6 at top, 1 at bottom)
  for (var i = 6; i >= 1; i--) {
    var s = stageInfo(i);
    var cls = 'ladder-node';
    var icon = '';
    if (i < stage) { cls += ' completed'; icon = '&#x2705;'; }
    else if (i === stage) { cls += ' current'; icon = '&#x25C9;'; }
    else { cls += ' locked'; icon = '&#x1F512;'; }

    var gateInfo = '';
    if (i === stage && gates.criteria) {
      var missing = gates.missing || [];
      if (missing.length > 0) {
        gateInfo = '<div class="node-gate-info">Falta: ' + missing.map(function(m) { return criterionLabel(m, i); }).join(', ') + '</div>';
      } else {
        gateInfo = '<div class="node-gate-info" style="color:#34d399">Portao aberto!</div>';
      }
    } else if (i > stage) {
      var gateKeys = Object.keys(STAGES[i-1].gate_labels || {});
      gateInfo = '<div class="node-gate-info">' + gateKeys.map(function(k) { return criterionLabel(k, i); }).join(', ') + '</div>';
    }

    var node = document.createElement('div');
    node.className = cls;
    node.style.setProperty('--stage-color', s.color);
    node.innerHTML =
      '<div class="ladder-dot"></div>'
      +'<div class="node-header">'
      +'<span class="node-num" style="color:'+s.color+'">'+i+'</span>'
      +'<span class="node-name">'+s.name+'</span>'
      +'<span class="node-icon">'+icon+'</span>'
      +'</div>'
      +'<div class="node-desc">'+s.desc+'</div>'
      +gateInfo;

    // Tap to show gate info on locked stages
    if (i > stage) {
      (function(el) {
        el.addEventListener('click', function() {
          var gi = el.querySelector('.node-gate-info');
          if (gi) gi.classList.toggle('tapped');
        });
      })(node);
    }

    ladder.appendChild(node);
  }
}

function showCelebration(newStage) {
  var s = stageInfo(newStage);
  document.getElementById('celeb-msg').textContent = 'Avancou pra ' + s.name + '!';
  document.getElementById('celeb-sub').textContent = 'Etapa ' + newStage + ' de 6';
  var overlay = document.getElementById('celebration');
  overlay.classList.add('show');
  // Confetti
  var colors = ['#3B82F6','#22d3ee','#34d399','#facc15','#fb923c','#7C5CFC','#f87171'];
  for (var i = 0; i < 30; i++) {
    var piece = document.createElement('div');
    piece.className = 'confetti-piece';
    piece.style.left = Math.random()*100+'%';
    piece.style.background = colors[Math.floor(Math.random()*colors.length)];
    piece.style.animationDelay = (Math.random()*1)+'s';
    piece.style.width = (6+Math.random()*8)+'px';
    piece.style.height = (6+Math.random()*8)+'px';
    document.body.appendChild(piece);
  }
  setTimeout(function() { location.reload(); }, 3000);
}

function doAdvance() {
  var btn = document.getElementById('btn-advance');
  btn.disabled = true;
  btn.textContent = 'Avancando...';
  fetch('/api/speech/advance', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(function(r){return r.json()})
    .then(function(d) {
      if (d.advanced) {
        showCelebration(d.new_stage);
      } else {
        btn.textContent = d.reason || 'Ainda nao atingiu o portao';
        setTimeout(function() { btn.textContent = '\u{1F31F} Avancar!'; btn.disabled = false; }, 2000);
      }
    })
    .catch(function() {
      btn.textContent = 'Erro — tente de novo';
      btn.disabled = false;
    });
}

// ── Load data ──
fetch('/api/speech/stage')
  .then(function(r){return r.json()})
  .then(function(d) {
    currentStage = d.stage || 1;
    renderHero(currentStage, d.activities);
    var gates = d.gates || {};
    renderGate(gates);
    renderLadder(currentStage, gates);

    // Auto-advance check
    if (gates.gate_met) {
      fetch('/api/speech/advance', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
        .then(function(r){return r.json()})
        .then(function(adv) {
          if (adv.advanced) showCelebration(adv.new_stage);
        });
    }
  })
  .catch(function(e) { console.error('speech load error', e); });

// Regression check
fetch('/api/speech/check-regression', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
  .then(function(r){return r.json()})
  .then(function(d) {
    if (d.regressed) {
      document.getElementById('regression-banner').style.display = 'block';
    }
  })
  .catch(function(){});
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
  @keyframes chipFlash { 0%{background:rgba(34,197,94,0.3)} 100%{background:rgba(34,197,94,0.12)} }
  @keyframes badgeFade { 0%{opacity:1;transform:translateY(0)} 100%{opacity:0;transform:translateY(-16px)} }
  @keyframes overlayIn { from{opacity:0} to{opacity:1} }
  @keyframes cardIn { from{opacity:0;transform:scale(0.92)} to{opacity:1;transform:scale(1)} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    display: flex; flex-direction: column; height: 100vh; height: 100dvh;
    overflow: hidden; -webkit-user-select: none; user-select: none;
  }

  /* ── Stage banner ── */
  .stage-banner {
    padding: 10px 20px; background: rgba(255,255,255,0.03);
    border-top: 3px solid #3B82F6;
    display: flex; justify-content: space-between; align-items: center;
    flex-shrink: 0; backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .stage-banner.s1 { border-top-color: #3B82F6; }
  .stage-banner.s2 { border-top-color: #06B6D4; }
  .stage-banner.s3 { border-top-color: #22C55E; }
  .stage-banner.s4 { border-top-color: #EAB308; }
  .stage-banner.s5 { border-top-color: #F97316; }
  .stage-banner.s6 { border-top-color: #7C5CFC; }
  .stage-label {
    font-size: 0.82em; font-weight: 700; color: #fafafa; letter-spacing: -0.3px;
  }
  .stage-hint {
    font-size: 0.72em; color: #7a7a8e; font-weight: 500; max-width: 55%; text-align: right;
  }

  /* ── Header ── */
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
  .hdr-btn.end-btn { color: #f87171; border-color: rgba(248,113,113,0.3); }
  .hdr-btn.end-btn:active { background: rgba(248,113,113,0.1); }

  /* ── Vocab chips ── */
  .chips-bar {
    padding: 8px 16px; display: none; flex-wrap: wrap; gap: 6px;
    flex-shrink: 0; background: rgba(255,255,255,0.02);
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .chips-bar.visible { display: flex; }
  .v-chip {
    padding: 5px 12px; border-radius: 20px; font-size: 0.72em; font-weight: 600;
    background: rgba(255,255,255,0.08); color: #c4c4d4; border: 1px solid rgba(255,255,255,0.06);
    transition: all 0.3s; cursor: default; white-space: nowrap;
  }
  .v-chip.used {
    background: rgba(34,197,94,0.12); color: #4ADE80; border-color: rgba(34,197,94,0.25);
    animation: chipFlash 0.6s ease-out;
  }
  .v-chip.used::after { content: ' \\2713'; }

  /* ── Messages ── */
  .messages {
    flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px;
    -webkit-overflow-scrolling: touch; position: relative;
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

  /* ── Chunk-hit badge ── */
  .chunk-badge {
    align-self: flex-end; padding: 4px 12px; border-radius: 12px;
    font-size: 0.7em; font-weight: 600; color: #4ADE80;
    background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.2);
    animation: badgeFade 2.5s ease-out forwards;
  }

  /* ── Input bar ── */
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

  /* ── Session summary overlay ── */
  .summary-overlay {
    display: none; position: fixed; inset: 0; z-index: 100;
    background: rgba(0,0,0,0.7); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    align-items: center; justify-content: center; padding: 24px;
    animation: overlayIn 0.3s ease-out;
  }
  .summary-overlay.visible { display: flex; }
  .summary-card {
    background: rgba(20,20,22,0.98); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px; padding: 28px 24px; max-width: 420px; width: 100%;
    text-align: center; animation: cardIn 0.3s ease-out;
    max-height: 85vh; overflow-y: auto; -webkit-overflow-scrolling: touch;
    box-shadow: 0 8px 40px rgba(0,0,0,0.5);
  }
  .summary-card h2 {
    font-size: 1.1em; font-weight: 800; margin-bottom: 20px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .summary-stats { display: flex; flex-direction: column; gap: 10px; margin-bottom: 24px; }
  .summary-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.05); border-radius: 12px;
  }
  .summary-row .label { font-size: 0.82em; color: #7a7a8e; }
  .summary-row .value { font-size: 0.95em; font-weight: 700; color: #fafafa; }
  .summary-chips { display: flex; flex-wrap: wrap; gap: 6px; justify-content: center; margin-bottom: 20px; }
  .summary-chips .v-chip { font-size: 0.7em; }
  .summary-nova-btn {
    width: 100%; padding: 14px; border: none; border-radius: 14px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    font-size: 0.95em; font-weight: 700; cursor: pointer; transition: all 0.2s;
    box-shadow: 0 4px 16px rgba(59,130,246,0.25);
  }
  .summary-nova-btn:active { transform: scale(0.97); opacity: 0.9; }

  /* ── Analysis display ── */
  .fluency-ring {
    width: 90px; height: 90px; margin: 0 auto 16px; position: relative;
    display: flex; align-items: center; justify-content: center;
  }
  .fluency-ring svg { position: absolute; top: 0; left: 0; transform: rotate(-90deg); }
  .fluency-ring .score-num { font-size: 1.8em; font-weight: 900; z-index: 1; }
  .fluency-ring .score-num.red { color: #f87171; }
  .fluency-ring .score-num.yellow { color: #FBBF24; }
  .fluency-ring .score-num.green { color: #4ADE80; }
  .analysis-section { margin-bottom: 16px; text-align: left; }
  .analysis-section h3 {
    font-size: 0.82em; font-weight: 700; color: #7a7a8e; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 8px; text-align: left;
  }
  .error-card {
    padding: 10px 12px; border-radius: 12px; margin-bottom: 8px;
    background: rgba(248,113,113,0.06); border: 1px solid rgba(248,113,113,0.15); text-align: left;
  }
  .error-card .err-original { color: #f87171; text-decoration: line-through; font-size: 0.85em; margin-bottom: 2px; }
  .error-card .err-corrected { color: #4ADE80; font-size: 0.9em; font-weight: 600; margin-bottom: 4px; }
  .error-card .err-tipo {
    display: inline-block; padding: 2px 8px; border-radius: 8px; font-size: 0.68em;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px;
    background: rgba(124,92,252,0.15); color: #A78BFA; margin-right: 6px;
  }
  .error-card .err-explain { font-size: 0.78em; color: #9ca3af; margin-top: 4px; line-height: 1.4; }
  .pattern-card {
    padding: 10px 12px; border-radius: 12px; margin-bottom: 8px;
    background: rgba(251,191,36,0.06); border: 1px solid rgba(251,191,36,0.15); text-align: left;
  }
  .pattern-card .pat-name { font-size: 0.88em; font-weight: 700; color: #FBBF24; margin-bottom: 4px; }
  .pattern-card .pat-examples { font-size: 0.78em; color: #9ca3af; margin-bottom: 4px; }
  .pattern-card .pat-drill { font-size: 0.78em; color: #c4c4d4; font-style: italic; }
  .correct-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .correct-pill {
    padding: 4px 12px; border-radius: 20px; font-size: 0.72em; font-weight: 600;
    background: rgba(34,197,94,0.12); color: #4ADE80; border: 1px solid rgba(34,197,94,0.25);
  }
  .nota-geral {
    padding: 12px; border-radius: 12px; font-size: 0.85em; line-height: 1.5;
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06);
    color: #c4c4d4; text-align: left; margin-bottom: 16px; font-style: italic;
  }
  .drill-btn {
    width: 100%; padding: 12px; border: 1px solid rgba(251,191,36,0.3); border-radius: 14px;
    background: rgba(251,191,36,0.08); color: #FBBF24;
    font-size: 0.88em; font-weight: 700; cursor: pointer; transition: all 0.2s; margin-bottom: 10px;
  }
  .drill-btn:active { background: rgba(251,191,36,0.15); transform: scale(0.97); }
  .drill-btn:disabled { opacity: 0.4; cursor: default; }
  .analysis-loading { padding: 20px; text-align: center; color: #525263; font-size: 0.85em; }
</style>
</head><body>

<!-- Stage banner -->
<div class="stage-banner s1" id="stage-banner">
  <span class="stage-label" id="stage-label">Carregando...</span>
  <span class="stage-hint" id="stage-hint"></span>
</div>

<div class="header">
  <h1>CONVERSA</h1>
  <div class="header-btns">
    <button class="hdr-btn end-btn" id="end-btn" onclick="endConversa()" style="display:none">Encerrar</button>
    <button class="hdr-btn" id="nova-btn" onclick="newConversa()">Nova</button>
  </div>
</div>

<!-- Vocabulary chips (stages 3-6) -->
<div class="chips-bar" id="chips-bar"></div>

<div class="messages" id="messages"></div>

<div class="input-bar">
  <input type="text" id="msg-input" placeholder="Fala, parceiro..." autocomplete="off" autocorrect="off">
  <button class="mic-send-btn" id="mic-send-btn" onclick="toggleConvMic()">&#x1F3A4;</button>
  <button class="send-btn" id="send-btn" onclick="sendMessage()">&#x27A4;</button>
</div>

<audio id="conv-player" preload="auto"></audio>

<!-- Session summary overlay -->
<div class="summary-overlay" id="summary-overlay">
  <div class="summary-card">
    <h2>Conversa Encerrada</h2>
    <div id="fluency-ring-container"></div>
    <div class="summary-stats" id="summary-stats"></div>
    <div class="summary-chips" id="summary-chips"></div>
    <div id="analysis-container"></div>
    <button class="summary-nova-btn" onclick="closeSummaryAndNew()">Nova Conversa</button>
  </div>
</div>

<script>
const msgBox = document.getElementById('messages');
const msgInput = document.getElementById('msg-input');
const sendBtn = document.getElementById('send-btn');
const convPlayer = document.getElementById('conv-player');
const micSendBtn = document.getElementById('mic-send-btn');
const stageBanner = document.getElementById('stage-banner');
const stageLabel = document.getElementById('stage-label');
const stageHint = document.getElementById('stage-hint');
const chipsBar = document.getElementById('chips-bar');
const endBtnEl = document.getElementById('end-btn');
const novaBtnEl = document.getElementById('nova-btn');
const summaryOverlay = document.getElementById('summary-overlay');
const summaryStats = document.getElementById('summary-stats');
const summaryChips = document.getElementById('summary-chips');

let convRecording = false;
let convRecorder = null;
let convMicChunks = [];

let sessionId = null;
let sessionStart = null;
let currentStage = 1;
let vocabChunks = [];
let usedChunks = new Set();

const STAGE_NAMES = {
  1: 'Eco', 2: 'Troca', 3: 'Reconto',
  4: 'Expressao', 5: 'Semi-Livre', 6: 'Livre'
};
const STAGE_HINTS = {
  1: 'Repita o que ouviu',
  2: 'Troque o chunk destacado',
  3: 'Reconte com suas palavras',
  4: 'Responda usando seus chunks',
  5: 'Fale livremente sobre o tema',
  6: 'Conversa natural'
};

msgInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

function updateStageBanner(stage) {
  currentStage = stage;
  stageBanner.className = 'stage-banner s' + stage;
  stageLabel.textContent = 'Nivel ' + stage + ' \\u2014 ' + (STAGE_NAMES[stage] || 'Livre');
  stageHint.textContent = STAGE_HINTS[stage] || '';
}

function renderChips(chunks) {
  vocabChunks = chunks || [];
  usedChunks = new Set();
  chipsBar.innerHTML = '';
  if (vocabChunks.length === 0) { chipsBar.classList.remove('visible'); return; }
  vocabChunks.forEach(function(ch) {
    var pill = document.createElement('span');
    pill.className = 'v-chip';
    pill.textContent = ch;
    pill.dataset.chunk = ch;
    chipsBar.appendChild(pill);
  });
  chipsBar.classList.add('visible');
}

function markChipsUsed(hits) {
  if (!hits || hits.length === 0) return;
  hits.forEach(function(ch) {
    usedChunks.add(ch);
    var pills = chipsBar.querySelectorAll('.v-chip');
    pills.forEach(function(p) {
      if (p.dataset.chunk === ch && !p.classList.contains('used')) {
        p.classList.add('used');
      }
    });
  });
}

function showChunkBadge(hits) {
  if (!hits || hits.length === 0) return;
  var badge = document.createElement('div');
  badge.className = 'chunk-badge';
  badge.textContent = 'Usou: ' + hits.join(', ');
  msgBox.appendChild(badge);
  msgBox.scrollTop = msgBox.scrollHeight;
  setTimeout(function() { if (badge.parentNode) badge.remove(); }, 2600);
}

function addMsg(text, role) {
  var div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  msgBox.appendChild(div);
  msgBox.scrollTop = msgBox.scrollHeight;
  return div;
}

async function startSession() {
  sendBtn.disabled = true;
  endBtnEl.style.display = 'none';
  novaBtnEl.style.display = '';
  chipsBar.classList.remove('visible');
  msgBox.innerHTML = '';

  try {
    var stageRes = await fetch('/api/speech/stage');
    var stageData = await stageRes.json();
    updateStageBanner(stageData.stage || 1);
  } catch (e) {
    updateStageBanner(1);
  }

  var typing = addMsg('...', 'ai');
  typing.innerHTML = '<span class="typing">Iniciando conversa...</span>';

  try {
    var res = await fetch('/api/conversa/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ topic: '' }),
    });
    var data = await res.json();

    if (data.error) {
      typing.textContent = 'Erro ao iniciar: ' + data.error;
      return;
    }

    sessionId = data.session_id;
    sessionStart = Date.now();
    if (data.stage) updateStageBanner(data.stage);

    renderChips(data.chunks_vocab || []);

    typing.textContent = data.reply || 'Opa!';
    if (data.audio_file) {
      convPlayer.src = '/audio/' + data.audio_file;
      convPlayer.play().catch(function() {});
    }

    endBtnEl.style.display = '';
    novaBtnEl.style.display = 'none';
    sendBtn.disabled = false;
  } catch (e) {
    typing.textContent = 'Erro de conexao ao iniciar.';
  }
  msgInput.focus();
}

async function sendMessage(text) {
  var msg = text || msgInput.value.trim();
  if (!msg) return;
  if (!sessionId) { await startSession(); return; }
  msgInput.value = '';
  addMsg(msg, 'user');

  sendBtn.disabled = true;
  var typing = addMsg('...', 'ai');
  typing.innerHTML = '<span class="typing">Pensando...</span>';

  try {
    var res = await fetch('/api/conversa/turn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });
    var data = await res.json();
    typing.textContent = data.reply || 'Erro';
    if (data.audio_file) {
      convPlayer.src = '/audio/' + data.audio_file;
      convPlayer.play().catch(function() {});
    }
    if (data.chunks_hit && data.chunks_hit.length > 0) {
      markChipsUsed(data.chunks_hit);
      showChunkBadge(data.chunks_hit);
    }
  } catch (e) {
    typing.textContent = 'Erro de conexao.';
  }
  sendBtn.disabled = false;
  msgInput.focus();
}

async function endConversa() {
  if (!sessionId) return;
  var durationSec = Math.round((Date.now() - (sessionStart || Date.now())) / 1000);

  endBtnEl.style.display = 'none';
  sendBtn.disabled = true;

  try {
    var res = await fetch('/api/conversa/end', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: sessionId, duration_seconds: durationSec }),
    });
    var data = await res.json();
    showSummary(data, durationSec);
  } catch (e) {
    novaBtnEl.style.display = '';
  }
  sessionId = null;
}

function showSummary(data, durationSec) {
  var mins = Math.floor(durationSec / 60);
  var secs = durationSec % 60;
  var timeStr = mins > 0 ? mins + 'min ' + secs + 's' : secs + 's';

  // ── Fluency ring ──
  var ringContainer = document.getElementById('fluency-ring-container');
  ringContainer.innerHTML = '';
  var analysis = data.analysis || null;
  var savedSessionId = data.session_id;

  if (analysis && typeof analysis.fluencia_score === 'number') {
    var score = analysis.fluencia_score;
    var colorClass = score < 40 ? 'red' : (score < 70 ? 'yellow' : 'green');
    var strokeColor = score < 40 ? '#f87171' : (score < 70 ? '#FBBF24' : '#4ADE80');
    var pct = Math.min(score, 100) / 100;
    var circumference = 2 * Math.PI * 38;
    var dashOffset = circumference * (1 - pct);
    ringContainer.innerHTML =
      '<div class="fluency-ring">' +
      '<svg width="90" height="90"><circle cx="45" cy="45" r="38" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="6"/>' +
      '<circle cx="45" cy="45" r="38" fill="none" stroke="' + strokeColor + '" stroke-width="6" ' +
      'stroke-dasharray="' + circumference + '" stroke-dashoffset="' + dashOffset + '" stroke-linecap="round"/></svg>' +
      '<span class="score-num ' + colorClass + '">' + score + '</span></div>';
  }

  summaryStats.innerHTML = [
    sRow('Turnos', data.turns || 0),
    sRow('Duracao', timeStr),
    sRow('Chunks extraidos', data.chunks_extracted || 0),
    sRow('Adicionados ao SRS', data.chunks_introduced || 0),
    sRow('Vocab usados', (data.vocab_chunks_used || []).length + '/' + vocabChunks.length),
  ].join('');

  summaryChips.innerHTML = '';
  var introduced = data.chunks_introduced_list || [];
  var vocabUsed = data.vocab_chunks_used || [];
  var seen = {};
  var allChips = [];
  introduced.concat(vocabUsed).forEach(function(ch) {
    if (!seen[ch]) { seen[ch] = true; allChips.push(ch); }
  });
  allChips.forEach(function(ch) {
    var pill = document.createElement('span');
    pill.className = 'v-chip used';
    pill.textContent = ch;
    summaryChips.appendChild(pill);
  });

  // ── Analysis display ──
  var ac = document.getElementById('analysis-container');
  ac.innerHTML = '';

  if (analysis) {
    var html = '';

    // Errors
    var erros = analysis.erros || [];
    if (erros.length > 0) {
      html += '<div class="analysis-section"><h3>Erros encontrados</h3>';
      erros.forEach(function(e) {
        html += '<div class="error-card">' +
          '<div class="err-original">' + escH(e.original || '') + '</div>' +
          '<div class="err-corrected">' + escH(e.corrigido || '') + '</div>' +
          '<span class="err-tipo">' + escH(e.tipo || '') + '</span>' +
          '<div class="err-explain">' + escH(e.explicacao || '') + '</div></div>';
      });
      html += '</div>';
    }

    // Weak patterns
    var padroes = analysis.padroes_fracos || [];
    if (padroes.length > 0) {
      html += '<div class="analysis-section"><h3>Padroes fracos</h3>';
      padroes.forEach(function(p) {
        var exs = (p.exemplos || []).join(', ');
        html += '<div class="pattern-card">' +
          '<div class="pat-name">' + escH(p.padrao || '') + '</div>' +
          '<div class="pat-examples">' + escH(exs) + '</div>' +
          '<div class="pat-drill">' + escH(p.sugestao_drill || '') + '</div></div>';
      });
      html += '</div>';
    }

    // Correct chunks
    var corretos = analysis.chunks_corretos || [];
    if (corretos.length > 0) {
      html += '<div class="analysis-section"><h3>Chunks corretos</h3><div class="correct-pills">';
      corretos.forEach(function(ch) {
        html += '<span class="correct-pill">' + escH(ch) + '</span>';
      });
      html += '</div></div>';
    }

    // General note
    if (analysis.nota_geral) {
      html += '<div class="nota-geral">' + escH(analysis.nota_geral) + '</div>';
    }

    // Generate drills button
    if (erros.length > 0 && savedSessionId) {
      html += '<button class="drill-btn" id="gen-drills-btn" onclick="generateDrills(' + savedSessionId + ')">' +
        'Criar treinos dos erros</button>';
    }

    ac.innerHTML = html;
  }

  summaryOverlay.classList.add('visible');
}

function escH(s) {
  var d = document.createElement('div');
  d.appendChild(document.createTextNode(s));
  return d.innerHTML;
}

async function generateDrills(sid) {
  var btn = document.getElementById('gen-drills-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Criando treinos...';
  try {
    var res = await fetch('/api/conversa/' + sid + '/generate-drills', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    var data = await res.json();
    if (data.ok) {
      btn.textContent = 'Criados! ' + data.count + ' chunks adicionados';
      btn.style.borderColor = 'rgba(34,197,94,0.3)';
      btn.style.color = '#4ADE80';
      btn.style.background = 'rgba(34,197,94,0.08)';
    } else {
      btn.textContent = 'Erro: ' + (data.error || 'falhou');
    }
  } catch (e) {
    btn.textContent = 'Erro de conexao';
  }
}

function sRow(label, value) {
  return '<div class="summary-row"><span class="label">' + label + '</span><span class="value">' + value + '</span></div>';
}

function closeSummaryAndNew() {
  summaryOverlay.classList.remove('visible');
  newConversa();
}

function newConversa() {
  sessionId = null;
  sessionStart = null;
  vocabChunks = [];
  usedChunks = new Set();
  chipsBar.classList.remove('visible');
  chipsBar.innerHTML = '';
  msgBox.innerHTML = '';
  summaryOverlay.classList.remove('visible');
  startSession();
}

async function toggleConvMic() {
  if (convRecording) {
    convRecording = false;
    micSendBtn.classList.remove('recording');
    if (convRecorder && convRecorder.state === 'recording') convRecorder.stop();
    return;
  }
  try {
    var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    convMicChunks = [];
    convRecorder = new MediaRecorder(stream);
    convRecorder.ondataavailable = function(e) { if (e.data.size > 0) convMicChunks.push(e.data); };
    convRecorder.onstop = async function() {
      stream.getTracks().forEach(function(t) { t.stop(); });
      var blob = new Blob(convMicChunks, { type: convRecorder.mimeType || 'audio/mp4' });
      addMsg('[Gravacao de voz — use texto por enquanto]', 'user');
    };
    convRecorder.start();
    convRecording = true;
    micSendBtn.classList.add('recording');
    setTimeout(function() { if (convRecording) toggleConvMic(); }, 10000);
  } catch (e) {
    // Mic not available
  }
}

// Init — auto-start a session on page load
startSession();
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
  .result-meta { font-size: 0.78em; color: #7a7a8e; margin-bottom: 8px; }
  .state-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
  .state-badge {
    display: inline-block; padding: 3px 10px; border-radius: 10px;
    font-size: 0.68em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .state-badge.unknown { background: rgba(82,82,99,0.2); color: #525263; }
  .state-badge.recognized { background: rgba(248,113,113,0.15); color: #f87171; }
  .state-badge.context { background: rgba(251,146,60,0.15); color: #fb923c; }
  .state-badge.effortful { background: rgba(250,204,21,0.15); color: #facc15; }
  .state-badge.clean { background: rgba(52,211,153,0.15); color: #34d399; }
  .state-badge.native { background: rgba(59,130,246,0.15); color: #3B82F6; }
  .state-badge.output { background: rgba(124,92,252,0.15); color: #7C5CFC; }
  .frag-badge {
    display: inline-block; padding: 3px 10px; border-radius: 10px;
    font-size: 0.68em; font-weight: 600; background: rgba(248,113,113,0.1);
    color: #f87171; border: 1px solid rgba(248,113,113,0.2);
  }
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
    overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
  }
  .tab-row::-webkit-scrollbar { display: none; }
  .tab-btn {
    flex: 0 0 auto; white-space: nowrap; padding: 12px 14px; text-align: center;
    font-size: 0.78em; font-weight: 600;
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

  /* ── Skeleton loader ── */
  @keyframes shimmer { 0%{background-position:-200% 0} 100%{background-position:200% 0} }
  .skeleton {
    padding: 20px;
  }
  .skel-line {
    height: 14px; border-radius: 8px; margin-bottom: 12px;
    background: linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.08) 50%, rgba(255,255,255,0.04) 75%);
    background-size: 200% 100%; animation: shimmer 1.5s ease-in-out infinite;
  }
  .skel-line.w60 { width: 60%; }
  .skel-line.w80 { width: 80%; }
  .skel-line.w40 { width: 40%; }
  .skel-line.w100 { width: 100%; }
  .skel-block {
    height: 80px; border-radius: 12px; margin-bottom: 12px;
    background: linear-gradient(90deg, rgba(255,255,255,0.03) 25%, rgba(255,255,255,0.06) 50%, rgba(255,255,255,0.03) 75%);
    background-size: 200% 100%; animation: shimmer 1.5s ease-in-out infinite;
  }

  /* ── Conjugation ── */
  .conj-badge-irreg {
    display: inline-block; padding: 3px 10px; border-radius: 10px;
    font-size: 0.7em; font-weight: 700; background: rgba(245,158,11,0.15); color: #f59e0b;
    margin-bottom: 16px;
  }
  .conj-grid {
    display: grid; grid-template-columns: 1fr; gap: 16px;
  }
  @media (min-width: 500px) {
    .conj-grid { grid-template-columns: 1fr 1fr; }
  }
  .conj-tense-card {
    background: rgba(255,255,255,0.03); border-radius: 14px; padding: 16px;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 2px 8px rgba(0,0,0,0.2);
  }
  .conj-tense-name {
    font-size: 0.78em; font-weight: 700; color: #60a5fa; text-transform: uppercase;
    letter-spacing: 0.8px; margin-bottom: 10px;
  }
  .conj-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.03);
  }
  .conj-row:last-child { border-bottom: none; }
  .conj-row:nth-child(even) { background: rgba(255,255,255,0.015); margin: 0 -8px; padding: 6px 8px; border-radius: 6px; }
  .conj-person { font-size: 0.78em; color: #525263; min-width: 80px; }
  .conj-person.agente { color: #60a5fa; font-weight: 600; }
  .conj-form {
    display: inline-block; padding: 4px 12px; border-radius: 10px;
    background: rgba(255,255,255,0.06); color: #e0e0e5; font-size: 0.88em; font-weight: 500;
  }

  /* ── Synonyms ── */
  .syn-register {
    display: inline-block; padding: 4px 12px; border-radius: 10px;
    font-size: 0.7em; font-weight: 700; margin-bottom: 16px;
  }
  .syn-register.formal { background: rgba(139,92,246,0.15); color: #a78bfa; }
  .syn-register.informal { background: rgba(59,130,246,0.15); color: #60a5fa; }
  .syn-register.giria { background: rgba(245,158,11,0.15); color: #f59e0b; }
  .syn-register.tecnico { background: rgba(16,185,129,0.15); color: #34d399; }
  .syn-section-label {
    font-size: 0.7em; color: #525263; text-transform: uppercase; letter-spacing: 1px;
    margin-bottom: 10px; margin-top: 18px;
  }
  .syn-section-label:first-child { margin-top: 0; }
  .syn-pills { display: flex; flex-wrap: wrap; gap: 8px; }
  .syn-pill {
    display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 12px;
    background: rgba(255,255,255,0.08); color: #e0e0e5; font-size: 0.88em;
    cursor: pointer; transition: all 0.2s; border: 1px solid transparent;
  }
  .syn-pill:active { transform: scale(0.96); }
  .syn-pill.baiano { border-color: rgba(59,130,246,0.4); }
  .syn-pill .ba-badge {
    font-size: 0.6em; font-weight: 800; color: #60a5fa; background: rgba(59,130,246,0.15);
    padding: 2px 5px; border-radius: 4px;
  }
  .syn-pill .usage-note { font-size: 0.75em; color: #7a7a8e; }
  .ant-pill {
    background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.15);
  }
  .rel-pill .rel-label {
    font-size: 0.6em; color: #7a7a8e; background: rgba(255,255,255,0.05);
    padding: 2px 6px; border-radius: 4px;
  }

  /* ── Chunks ── */
  .chunk-section-label {
    font-size: 0.7em; color: #525263; text-transform: uppercase; letter-spacing: 1px;
    margin-bottom: 12px; margin-top: 20px;
  }
  .chunk-section-label:first-child { margin-top: 0; }
  .chunk-card {
    background: rgba(255,255,255,0.03); border-radius: 14px; padding: 14px 14px 14px 18px;
    margin-bottom: 10px; position: relative; overflow: hidden;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 2px 8px rgba(0,0,0,0.2);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
  }
  .chunk-card::before {
    content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
  }
  .chunk-card.freq-alta::before { background: #22c55e; }
  .chunk-card.freq-media::before { background: #eab308; }
  .chunk-card.freq-baixa::before { background: #525263; }
  .chunk-text { font-size: 1em; font-weight: 600; color: #fafafa; margin-bottom: 8px; }
  .chunk-badges { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chunk-freq {
    font-size: 0.65em; font-weight: 700; padding: 3px 8px; border-radius: 8px; text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .chunk-freq.alta { background: rgba(34,197,94,0.15); color: #22c55e; }
  .chunk-freq.media { background: rgba(234,179,8,0.15); color: #eab308; }
  .chunk-freq.baixa { background: rgba(82,82,99,0.2); color: #7a7a8e; }
  .chunk-type {
    font-size: 0.65em; font-weight: 600; padding: 3px 8px; border-radius: 8px;
    background: rgba(255,255,255,0.06); color: #7a7a8e;
  }
  .chunk-source {
    font-size: 0.65em; font-weight: 600; padding: 3px 8px; border-radius: 8px;
    background: rgba(124,92,252,0.12); color: #a78bfa;
  }
  .chunk-actions { display: flex; gap: 8px; margin-top: 10px; }
  .chunk-add-btn {
    padding: 6px 14px; border-radius: 10px; border: 1px solid rgba(59,130,246,0.25);
    background: rgba(59,130,246,0.08); color: #60a5fa; font-size: 0.75em; font-weight: 600;
    cursor: pointer; transition: all 0.2s;
  }
  .chunk-add-btn:active { transform: scale(0.96); }
  .chunk-add-btn:disabled { opacity: 0.4; }
  .chunk-play-btn {
    width: 30px; height: 30px; border-radius: 50%; border: none;
    background: rgba(59,130,246,0.1); color: #60a5fa; font-size: 0.75em;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
  }

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
    <div class="state-row" id="state-row"></div>
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
      <button class="tab-btn" onclick="showTab(4)" id="tab-btn-conj" style="display:none">Conjugação</button>
      <button class="tab-btn" onclick="showTab(5)">Sinônimos</button>
      <button class="tab-btn" onclick="showTab(6)">Chunks</button>
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

    <!-- Tab: Conjugação -->
    <div class="tab-content" id="tab-4">
      <div id="conj-container">
        <div class="skeleton"><div class="skel-block"></div><div class="skel-block"></div></div>
      </div>
    </div>

    <!-- Tab: Sinônimos -->
    <div class="tab-content" id="tab-5">
      <div id="syn-container">
        <div class="skeleton"><div class="skel-line w60"></div><div class="skel-line w80"></div><div class="skel-line w40"></div></div>
      </div>
    </div>

    <!-- Tab: Chunks -->
    <div class="tab-content" id="tab-6">
      <div id="chunks-container">
        <div class="skeleton"><div class="skel-block"></div><div class="skel-line w80"></div><div class="skel-block"></div></div>
      </div>
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
let tabCache = {};  // {tabIdx: data} — cache per word selection

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
      acBox.innerHTML = data.results.map(r => {
        if (r.is_live_lookup) {
          return '<div class="ac-item" onclick="selectWord(-1,\'' + r.word.replace(/'/g, "\\'") + '\',0,0)">' +
            '<span class="ac-word">' + r.word + '</span>' +
            '<span class="ac-tier" style="color:#3B82F6">Buscar ao vivo</span></div>';
        }
        return '<div class="ac-item" onclick="selectWord(' + r.word_id + ',\'' + r.word.replace(/'/g, "\\'") + '\',' + r.difficulty_tier + ',' + r.frequency_rank + ')">' +
          '<span class="ac-word">' + r.word + '</span>' +
          '<span class="ac-tier">Tier ' + r.difficulty_tier + '</span></div>';
      }).join('');
    }
    acBox.classList.add('visible');
  } catch(e) { acBox.classList.remove('visible'); }
}

let currentWord = '';
let activeTabIndex = 0;

// Map tab indices to API endpoint names
const TAB_ENDPOINTS = {
  0: 'definition', 1: 'examples', 2: 'pronunciation',
  3: 'expressions', 4: 'conjugation', 5: 'synonyms', 6: 'chunks'
};

// Map tab indices to skeleton containers
const TAB_CONTAINERS = {
  0: null, 1: null, 2: null, 3: null,
  4: 'conj-container', 5: 'syn-container', 6: 'chunks-container'
};

async function selectWord(wordId, word, tier, rank) {
  acBox.classList.remove('visible');
  searchInput.value = word;
  currentWordId = wordId;
  currentWord = word;
  currentData = { word: word, word_id: wordId };
  document.getElementById('empty-state').style.display = 'none';

  // Show word header immediately from search result info
  showWordHeader(word, wordId, tier, rank);

  // Reset all tab caches and rendered state
  tabCache = {};
  tabRendered = {};
  resetTabSkeletons();

  // Show the result card and load default tab (definition)
  document.getElementById('result-card').classList.add('visible');
  document.getElementById('loading').style.display = 'none';
  document.getElementById('add-srs-btn').disabled = false;
  document.getElementById('add-srs-btn').textContent = 'Adicionar ao treino';

  // For live lookups (word not in word_bank), fetch all data via /api/search/live
  if (wordId === -1) {
    activeTabIndex = 0;
    showTab(0);
    try {
      const res = await fetch('/api/search/live?q=' + encodeURIComponent(word));
      const data = await res.json();
      if (data.definition) { tabCache[0] = data.definition; renderTabData(0, data.definition); }
      if (data.examples) { tabCache[1] = data.examples; renderTabData(1, data.examples); }
      if (data.pronunciation) { tabCache[2] = data.pronunciation; renderTabData(2, data.pronunciation); }
      if (data.expressions) { tabCache[3] = data.expressions; renderTabData(3, data.expressions); }
      if (data.conjugation) {
        tabCache[4] = data.conjugation;
        renderTabData(4, data.conjugation);
        var conjBtn = document.getElementById('tab-btn-conj');
        conjBtn.style.display = (data.conjugation && data.conjugation.is_verb) ? '' : 'none';
      }
      if (data.synonyms) { tabCache[5] = data.synonyms; renderTabData(5, data.synonyms); }
      if (data.chunks) { tabCache[6] = data.chunks; renderTabData(6, data.chunks); }
    } catch(e) {}
    return;
  }

  // Show tab 0 (definition) and load its data
  activeTabIndex = 0;
  showTab(0);

  // Pre-fetch pronunciation in background (commonly needed for audio button)
  loadTabData(2);

  // Fetch automaticity state + fragility
  fetchWordState(wordId);
}

var STATE_LABELS = {
  'UNKNOWN': ['Desconhecida', 'unknown'],
  'RECOGNIZED': ['Reconhecida', 'recognized'],
  'CONTEXT_KNOWN': ['Contexto', 'context'],
  'EFFORTFUL_AUDIO': ['Esforço', 'effortful'],
  'AUTOMATIC_CLEAN': ['Automática', 'clean'],
  'AUTOMATIC_NATIVE': ['Nativa', 'native'],
  'AVAILABLE_OUTPUT': ['Produção', 'output']
};
var FRAG_LABELS = {
  'familiar_but_fragile': 'Frágil',
  'known_but_slow': 'Lenta',
  'text_only': 'Só texto',
  'clean_audio_only': 'Só áudio limpo',
  'blocked_by_prosody': 'Prosódia'
};

function fetchWordState(wordId) {
  var row = document.getElementById('state-row');
  row.innerHTML = '';
  fetch('/api/word/' + wordId + '/state')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var info = STATE_LABELS[d.state] || ['?', 'unknown'];
      row.innerHTML = '<span class="state-badge ' + info[1] + '">' + info[0] + '</span>';
      if (d.fragilities && d.fragilities.length > 0) {
        for (var i = 0; i < d.fragilities.length; i++) {
          var f = d.fragilities[i];
          var label = FRAG_LABELS[f.type] || f.type;
          row.innerHTML += '<span class="frag-badge">' + label + '</span>';
        }
      }
    })
    .catch(function() {});
}

function showWordHeader(word, wordId, tier, rank) {
  document.getElementById('r-word').textContent = word;
  const tierNames = {1:'Sobrevivência',2:'Cotidiano',3:'Conversação',4:'Fluência',5:'Nuance',6:'Quase Nativo'};
  if (wordId === -1) {
    document.getElementById('r-meta').textContent = 'Busca ao vivo';
  } else if (tier && rank) {
    document.getElementById('r-meta').textContent = 'Tier ' + tier + ' — ' + (tierNames[tier]||'') + ' · #' + rank;
  } else {
    document.getElementById('r-meta').textContent = '';
  }
  // Initially hide conjugation tab until we know it's a verb
  document.getElementById('tab-btn-conj').style.display = 'none';
}

function resetTabSkeletons() {
  // Reset definition tab with skeleton
  document.getElementById('def-text').innerHTML = '<div class="skeleton"><div class="skel-line w80"></div><div class="skel-line w60"></div></div>';
  document.getElementById('def-regional').textContent = '';
  document.getElementById('def-chunk').textContent = '';
  // Reset examples
  document.getElementById('examples-list').innerHTML = '<div class="skeleton"><div class="skel-line w100"></div><div class="skel-line w80"></div><div class="skel-line w100"></div></div>';
  // Reset pronunciation
  document.getElementById('pron-silabas').textContent = '';
  document.getElementById('pron-guide').textContent = '';
  // Reset expressions
  document.getElementById('expressions-list').innerHTML = '<div class="skeleton"><div class="skel-block"></div><div class="skel-block"></div></div>';
  // Reset lazy tabs
  document.getElementById('conj-container').innerHTML = '<div class="skeleton"><div class="skel-block"></div><div class="skel-block"></div></div>';
  document.getElementById('syn-container').innerHTML = '<div class="skeleton"><div class="skel-line w60"></div><div class="skel-line w80"></div><div class="skel-line w40"></div></div>';
  document.getElementById('chunks-container').innerHTML = '<div class="skeleton"><div class="skel-block"></div><div class="skel-line w80"></div><div class="skel-block"></div></div>';
}

let tabRendered = {};

function showTab(idx) {
  activeTabIndex = idx;
  document.querySelectorAll('.tab-btn').forEach((b,i) => {
    if (b.style.display === 'none') return;
    b.classList.toggle('active', i===idx);
  });
  document.querySelectorAll('.tab-content').forEach((c,i) => c.classList.toggle('visible', i===idx));

  // Load tab data on demand if not cached
  if (currentWordId && !tabCache[idx]) {
    loadTabData(idx);
  } else if (currentWordId && tabCache[idx] && !tabRendered[idx]) {
    renderTabData(idx, tabCache[idx]);
  }
}

async function loadTabData(idx) {
  if (!currentWordId) return;
  if (tabCache[idx]) {
    if (!tabRendered[idx]) renderTabData(idx, tabCache[idx]);
    return;
  }
  const ep = TAB_ENDPOINTS[idx];
  if (!ep) return;
  try {
    const res = await fetch('/api/word/' + currentWordId + '/' + ep);
    const data = await res.json();
    tabCache[idx] = data;
    renderTabData(idx, data);
  } catch(e) {
    // Show error in the appropriate container
    const errHtml = '<div style="text-align:center;color:#525263;padding:20px">Erro ao carregar dados</div>';
    if (idx === 0) document.getElementById('def-text').textContent = 'Erro ao carregar definição';
    else if (idx === 1) document.getElementById('examples-list').innerHTML = errHtml;
    else if (idx === 2) document.getElementById('pron-silabas').textContent = 'Erro';
    else if (idx === 3) document.getElementById('expressions-list').innerHTML = errHtml;
    else if (idx === 4) document.getElementById('conj-container').innerHTML = errHtml;
    else if (idx === 5) document.getElementById('syn-container').innerHTML = errHtml;
    else if (idx === 6) document.getElementById('chunks-container').innerHTML = errHtml;
  }
}

function renderTabData(idx, data) {
  tabRendered[idx] = true;
  if (idx === 0) renderDefinition(data);
  else if (idx === 1) renderExamples(data);
  else if (idx === 2) renderPronunciation(data);
  else if (idx === 3) renderExpressions(data);
  else if (idx === 4) renderConjugation(data);
  else if (idx === 5) renderSynonyms(data);
  else if (idx === 6) renderChunks(data);
}

function renderDefinition(def) {
  if (!def) def = {};
  const defEl = document.getElementById('def-text');
  defEl.innerHTML = '';
  defEl.textContent = def.definicao || 'Sem definição disponível';
  document.getElementById('def-regional').textContent = (def.uso_regional || 'geral');
  document.getElementById('def-chunk').textContent = def.exemplo_chunk ? '"' + def.exemplo_chunk + '"' : '';
  // Store in currentData for addToSRS
  currentData.definition = def;

  // Now check conjugation: pre-fetch to see if it's a verb (show/hide tab)
  if (!tabCache[4]) {
    fetch('/api/word/' + currentWordId + '/conjugation')
      .then(r => r.json())
      .then(d => {
        tabCache[4] = d;
        const conjBtn = document.getElementById('tab-btn-conj');
        conjBtn.style.display = (d && d.is_verb) ? '' : 'none';
      }).catch(()=>{});
  }
}

function renderExamples(data) {
  const exList = document.getElementById('examples-list');
  exList.innerHTML = '';
  const examples = Array.isArray(data) ? data : (data.exemplos || data || []);
  currentData.examples = examples;
  examples.forEach(function(ex) {
    if (!ex.texto) return;
    const div = document.createElement('div');
    div.className = 'example-item';
    div.innerHTML = '<button class="ex-audio" onclick="playExAudio(this)">&#x1F50A;</button>' +
      '<div class="ex-text">' + (ex.texto || '').replace(new RegExp('(' + (currentWord||'') + ')', 'gi'), '<span class="ex-chunk">$1</span>') + '</div>';
    exList.appendChild(div);
  });
  if (examples.length === 0 || !examples[0].texto) {
    exList.innerHTML = '<div style="text-align:center;color:#525263;padding:20px">Sem exemplos disponíveis</div>';
  }
}

function renderPronunciation(pron) {
  if (!pron) pron = {};
  document.getElementById('pron-silabas').textContent = pron.silabas || '';
  document.getElementById('pron-guide').textContent = pron.guia_fonetico || '';
  // Pre-fetch audio so the button works immediately
  if (!currentData.audio_file && currentWordId) {
    fetch('/api/word/' + currentWordId + '/audio')
      .then(r => r.json())
      .then(d => {
        var f = d && (d.audio_file || d.filename);
        if (f) currentData.audio_file = f;
      }).catch(()=>{});
  }
}

function renderExpressions(data) {
  const exprList = document.getElementById('expressions-list');
  exprList.innerHTML = '';
  const expressions = Array.isArray(data) ? data : (data.expressoes || data || []);
  currentData.expressions = expressions;
  expressions.forEach(function(expr) {
    if (!expr.expressao) return;
    const div = document.createElement('div');
    div.className = 'expr-item';
    div.innerHTML = '<div class="expr-phrase">' + (expr.expressao || '') + '</div>' +
      '<div class="expr-meaning">' + (expr.significado || '') + '</div>';
    exprList.appendChild(div);
  });
  if (expressions.length === 0) {
    exprList.innerHTML = '<div style="text-align:center;color:#525263;padding:20px">Sem expressões disponíveis</div>';
  }
}

function renderConjugation(d) {
  const c = document.getElementById('conj-container');
  if (!d || !d.tenses || d.tenses.length === 0) {
    c.innerHTML = '<div style="text-align:center;color:#525263;padding:20px">Sem dados de conjugação</div>';
    return;
  }
  const persons = ['eu', 'você', 'ele/ela', 'a gente', 'vocês', 'eles/elas'];
  let html = '';
  if (d.irregular) html += '<span class="conj-badge-irreg">Irregular</span>';
  html += '<div class="conj-grid">';
  d.tenses.forEach(t => {
    html += '<div class="conj-tense-card"><div class="conj-tense-name">' + (t.name || '') + '</div>';
    persons.forEach((p, i) => {
      const form = (t.forms && t.forms[i]) || '—';
      const isAgente = (p === 'a gente');
      html += '<div class="conj-row">' +
        '<span class="conj-person' + (isAgente ? ' agente' : '') + '">' + p + '</span>' +
        '<span class="conj-form">' + form + '</span></div>';
    });
    html += '</div>';
  });
  html += '</div>';
  c.innerHTML = html;
}

function renderSynonyms(d) {
  const c = document.getElementById('syn-container');
  if (!d) { c.innerHTML = '<div style="text-align:center;color:#525263;padding:20px">Sem dados</div>'; return; }
  let html = '';
  // Register badge (PT primary, EN fallback)
  var reg = d.registro || d.register || '';
  if (reg) {
    const regClass = { formal:'formal', informal:'informal', 'gíria':'giria', 'técnico':'tecnico' };
    html += '<span class="syn-register ' + (regClass[reg] || 'informal') + '">' + reg + '</span>';
  }
  // Synonyms (PT primary, EN fallback)
  var syns = d.sinonimos || d.synonyms || [];
  if (syns.length > 0) {
    html += '<div class="syn-section-label">Sinônimos</div><div class="syn-pills">';
    syns.forEach(s => {
      const word = typeof s === 'string' ? s : (s.palavra || s.word || '');
      const note = (typeof s === 'object') ? (s.nota || s.note || '') : '';
      const isBa = (typeof s === 'object' && s.baiano);
      html += '<span class="syn-pill' + (isBa ? ' baiano' : '') + '" onclick="searchSynonym(\'' + word.replace(/'/g, "\\\\'") + '\')">' +
        word + (isBa ? ' <span class="ba-badge">BA</span>' : '') +
        (note ? ' <span class="usage-note">' + note + '</span>' : '') + '</span>';
    });
    html += '</div>';
  }
  // Antonyms (PT primary, EN fallback)
  var ants = d.antonimos || d.antonyms || [];
  if (ants.length > 0) {
    html += '<div class="syn-section-label">Antônimos</div><div class="syn-pills">';
    ants.forEach(a => {
      const word = typeof a === 'string' ? a : (a.palavra || a.word || '');
      html += '<span class="syn-pill ant-pill" onclick="searchSynonym(\'' + word.replace(/'/g, "\\\\'") + '\')">' + word + '</span>';
    });
    html += '</div>';
  }
  // Related (PT primary, EN fallback)
  var rels = d.palavras_relacionadas || d.related || [];
  if (rels.length > 0) {
    html += '<div class="syn-section-label">Palavras relacionadas</div><div class="syn-pills">';
    rels.forEach(r => {
      const word = typeof r === 'string' ? r : (r.palavra || r.word || '');
      const label = (typeof r === 'object') ? (r.relacao || r.label || '') : '';
      html += '<span class="syn-pill rel-pill" onclick="searchSynonym(\'' + word.replace(/'/g, "\\\\'") + '\')">' +
        word + (label ? ' <span class="rel-label">' + label + '</span>' : '') + '</span>';
    });
    html += '</div>';
  }
  if (!html) html = '<div style="text-align:center;color:#525263;padding:20px">Sem sinônimos disponíveis</div>';
  c.innerHTML = html;
}

function renderChunks(d) {
  const c = document.getElementById('chunks-container');
  if (!d) { c.innerHTML = '<div style="text-align:center;color:#525263;padding:20px">Sem dados</div>'; return; }
  let html = '';
  // DB chunks (PT primary, EN fallback)
  var dbChunks = d.chunks_from_db || d.db_chunks || [];
  if (dbChunks.length > 0) {
    html += '<div class="chunk-section-label">No banco de dados</div>';
    dbChunks.forEach(ch => { html += buildChunkCard(ch, true); });
  }
  // Generated chunks (PT primary, EN fallback)
  var genChunks = d.chunks_generated || d.common_chunks || [];
  if (genChunks.length > 0) {
    html += '<div class="chunk-section-label">Chunks comuns</div>';
    genChunks.forEach(ch => { html += buildChunkCard(ch, false); });
  }
  if (!html) html = '<div style="text-align:center;color:#525263;padding:20px">Sem chunks disponíveis</div>';
  c.innerHTML = html;
}

function buildChunkCard(ch, fromDb) {
  const freq = (ch.frequencia || ch.frequency || 'media').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'');
  const freqLabel = { alta:'Alta', media:'Média', baixa:'Baixa' }[freq] || 'Média';
  const freqClass = { alta:'alta', media:'media', baixa:'baixa' }[freq] || 'media';
  let html = '<div class="chunk-card freq-' + freqClass + '">';
  html += '<div class="chunk-text">' + (ch.text || ch.chunk || '') + '</div>';
  html += '<div class="chunk-badges">';
  html += '<span class="chunk-freq ' + freqClass + '">' + freqLabel + '</span>';
  var chType = ch.tipo || ch.type || '';
  if (chType) html += '<span class="chunk-type">' + chType + '</span>';
  if (fromDb && ch.source) html += '<span class="chunk-source">' + ch.source + '</span>';
  html += '</div>';
  html += '<div class="chunk-actions">';
  const chunkText = (ch.text || ch.chunk || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
  html += '<button class="chunk-add-btn" onclick="addChunkToSRS(this,\'' + chunkText + '\')">Adicionar ao SRS</button>';
  if (ch.audio) html += '<button class="chunk-play-btn" onclick="playChunkAudio(\'' + ch.audio + '\')">&#x1F50A;</button>';
  html += '</div></div>';
  return html;
}

async function searchSynonym(word) {
  searchInput.value = word;
  clearBtn.style.display = 'flex';
  try {
    const res = await fetch('/api/search?q=' + encodeURIComponent(word));
    const data = await res.json();
    if (data.results && data.results.length > 0) {
      // Auto-select exact match or first result
      const exact = data.results.find(r => r.word.toLowerCase() === word.toLowerCase());
      const pick = exact || data.results[0];
      selectWord(pick.word_id, pick.word, pick.difficulty_tier, pick.frequency_rank);
    } else {
      fetchSearch(word);
    }
  } catch(e) { fetchSearch(word); }
}

function playWordAudio() {
  if (!currentWordId) return;
  if (currentData && currentData.audio_file) {
    player.src = '/audio/' + currentData.audio_file;
    player.play().catch(()=>{});
  } else {
    fetch('/api/word/' + currentWordId + '/audio')
      .then(r => r.json())
      .then(d => {
        var f = d && (d.audio_file || d.filename);
        if (f) {
          currentData.audio_file = f;
          player.src = '/audio/' + f;
          player.play().catch(()=>{});
        }
      }).catch(()=>{});
  }
}

function playExAudio(btn) {
  playWordAudio();
}

function playChunkAudio(filename) {
  player.src = '/audio/' + filename;
  player.play().catch(()=>{});
}

async function addChunkToSRS(btn, chunkText) {
  if (!currentWordId) return;
  btn.disabled = true;
  btn.textContent = 'Adicionando...';
  try {
    await fetch('/api/search/add-to-srs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ word_id: currentWordId, chunk: chunkText, carrier: chunkText }),
    });
    btn.textContent = 'Adicionado';
  } catch(e) {
    btn.textContent = 'Erro';
    btn.disabled = false;
  }
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


# ── Plan HTML ─────────────────────────────────────────────────

PLAN_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Oxe — Plano de Hoje</title>
<style>
  @keyframes fadeIn { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
  @keyframes pulse { 0%,100%{box-shadow:0 0 0 0 rgba(59,130,246,0.4)} 50%{box-shadow:0 0 0 10px rgba(59,130,246,0)} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    padding: 0 16px 100px 16px; -webkit-font-smoothing: antialiased;
  }
  .top-bar {
    display: flex; align-items: center; gap: 12px;
    padding: 56px 0 12px 0;
  }
  .back-btn {
    width: 36px; height: 36px; border-radius: 12px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
    display: flex; align-items: center; justify-content: center;
    color: #fafafa; font-size: 18px; text-decoration: none;
  }
  .page-title { font-size: 1.3em; font-weight: 700; flex:1; }
  .date-label { color: #7a7a8e; font-size: 0.8em; }
  .glass-card {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px; backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .progress-header {
    padding: 20px 24px; margin-bottom: 16px; animation: fadeIn 0.3s ease-out;
  }
  .progress-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .progress-pct { font-size: 2em; font-weight: 800; color: #3B82F6; }
  .progress-label { color: #7a7a8e; font-size: 0.8em; }
  .progress-track {
    width: 100%; height: 8px; border-radius: 4px; background: rgba(255,255,255,0.08);
  }
  .progress-fill {
    height: 100%; border-radius: 4px; background: linear-gradient(90deg, #3B82F6, #7C5CFC);
    transition: width 0.6s ease;
  }
  .timeline { position: relative; padding-left: 28px; margin-bottom: 16px; }
  .timeline::before {
    content: ''; position: absolute; left: 11px; top: 0; bottom: 0;
    width: 2px; background: rgba(255,255,255,0.08);
  }
  .block-card {
    position: relative; padding: 16px 20px; margin-bottom: 12px;
    animation: fadeIn 0.3s ease-out both;
  }
  .block-card::before {
    content: ''; position: absolute; left: -22px; top: 20px;
    width: 10px; height: 10px; border-radius: 50%;
    background: rgba(255,255,255,0.15); border: 2px solid rgba(255,255,255,0.1);
  }
  .block-card.completed { opacity: 0.5; }
  .block-card.completed::before { background: #34d399; border-color: #34d399; }
  .block-card.current { border-color: #3B82F6 !important; animation: pulse 2s infinite, fadeIn 0.3s ease-out both; }
  .block-card.current::before { background: #3B82F6; border-color: #3B82F6; }
  .block-card.upcoming { opacity: 0.6; }
  .block-card.upcoming::before { background: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.06); }
  .block-top { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .block-icon { font-size: 1.3em; }
  .block-type { font-weight: 600; font-size: 0.95em; flex:1; }
  .block-duration { color: #7a7a8e; font-size: 0.8em; }
  .block-mode { color: #7a7a8e; font-size: 0.78em; margin-top: 2px; }
  .block-status { font-size: 0.75em; margin-top: 6px; }
  .block-status.done { color: #34d399; }
  .block-status.active { color: #3B82F6; }
  .current-detail {
    padding: 20px 24px; margin-bottom: 16px; border: 1px solid #3B82F6;
    animation: fadeIn 0.4s ease-out 0.1s both;
  }
  .current-title { font-size: 1.1em; font-weight: 700; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
  .current-meta { color: #7a7a8e; font-size: 0.82em; margin-bottom: 12px; }
  .current-meta span { margin-right: 16px; }
  .btn-start {
    display: inline-block; padding: 12px 28px; border-radius: 14px;
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
    font-weight: 700; font-size: 0.95em; text-decoration: none; border: none; cursor: pointer;
  }
  .btn-start:active { transform: scale(0.97); }
  .fatigue-widget {
    padding: 16px 20px; margin-bottom: 16px; animation: fadeIn 0.4s ease-out 0.15s both;
  }
  .fatigue-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .fatigue-bar-track {
    flex: 1; height: 6px; border-radius: 3px; background: rgba(255,255,255,0.08);
  }
  .fatigue-bar-fill {
    height: 100%; border-radius: 3px; transition: width 0.5s ease, background 0.5s ease;
  }
  .fatigue-score { font-weight: 700; font-size: 1.1em; min-width: 32px; }
  .fatigue-minutes { color: #7a7a8e; font-size: 0.78em; }
  .btn-adjust {
    padding: 8px 18px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.1);
    background: rgba(255,255,255,0.06); color: #fafafa; font-size: 0.82em; cursor: pointer;
  }
  .btn-adjust:active { background: rgba(255,255,255,0.12); }
  .adjust-msg { color: #34d399; font-size: 0.8em; margin-top: 8px; display: none; }
  .no-plan { text-align: center; padding: 40px 20px; color: #7a7a8e; }
</style>
</head><body>

<div class="top-bar">
  <a href="/" class="back-btn">&larr;</a>
  <div class="page-title">Plano de Hoje</div>
  <div class="date-label" id="plan-date"></div>
</div>

<div class="progress-header glass-card">
  <div class="progress-row">
    <div>
      <div class="progress-pct" id="overall-pct">0%</div>
      <div class="progress-label">completo</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:1.4em;font-weight:700" id="blocks-done">0</div>
      <div class="progress-label" id="blocks-total">de 0 blocos</div>
    </div>
  </div>
  <div class="progress-track">
    <div class="progress-fill" id="progress-fill" style="width:0%"></div>
  </div>
</div>

<div class="timeline" id="timeline"></div>

<div class="current-detail glass-card" id="current-detail" style="display:none">
  <div class="current-title">
    <span id="cur-icon"></span>
    <span id="cur-type"></span>
  </div>
  <div class="current-meta">
    <span id="cur-mode"></span>
    <span id="cur-duration"></span>
    <span id="cur-items"></span>
  </div>
  <button class="btn-start" id="btn-start" onclick="startBlock()">Começar</button>
</div>

<div class="fatigue-widget glass-card">
  <div style="font-weight:600;font-size:0.85em;margin-bottom:10px;color:#7a7a8e">Fadiga</div>
  <div class="fatigue-row">
    <div class="fatigue-score" id="fat-score">0</div>
    <div class="fatigue-bar-track">
      <div class="fatigue-bar-fill" id="fat-fill" style="width:0%;background:#34d399"></div>
    </div>
  </div>
  <div style="display:flex;align-items:center;justify-content:space-between">
    <div class="fatigue-minutes" id="fat-minutes">0 min ativo</div>
    <button class="btn-adjust" onclick="adjustPlan()">Ajustar plano</button>
  </div>
  <div class="adjust-msg" id="adjust-msg">Plano ajustado!</div>
</div>

<div class="no-plan" id="no-plan" style="display:none">
  <div style="font-size:2em;margin-bottom:12px">&#x2615;</div>
  <div>Nenhum bloco para hoje.</div>
</div>

{tab_bar}

<script>
var TYPE_ICONS = {srs_drill:'\u{1F3AF}',listening:'\u{1F3A7}',shadowing:'\u{1F5E3}',break:'\u{2615}',conversa:'\u{1F4AC}'};
var TYPE_LABELS = {srs_drill:'SRS Drill',listening:'Escuta',shadowing:'Sombreamento',break:'Pausa',conversa:'Conversa'};
var currentBlock = null;

function renderPlan(data) {
  document.getElementById('plan-date').textContent = data.date || '';
  var pct = Math.round(data.completed_pct || 0);
  document.getElementById('overall-pct').textContent = pct + '%';
  document.getElementById('progress-fill').style.width = pct + '%';
  document.getElementById('blocks-done').textContent = data.completed_blocks || 0;
  document.getElementById('blocks-total').textContent = 'de ' + (data.total_blocks || 0) + ' blocos';

  var blocks = data.blocks || [];
  if (blocks.length === 0) {
    document.getElementById('no-plan').style.display = 'block';
    document.getElementById('timeline').style.display = 'none';
    document.getElementById('current-detail').style.display = 'none';
    return;
  }

  var html = '';
  for (var i = 0; i < blocks.length; i++) {
    var b = blocks[i];
    var status = b.completed ? 'completed' : (data.current_block && data.current_block.block_id === b.block_id ? 'current' : 'upcoming');
    var icon = TYPE_ICONS[b.type] || '\u{1F4CB}';
    var label = TYPE_LABELS[b.type] || b.type;
    var statusText = b.completed ? '<span class="block-status done">\u2713 Completo</span>' :
      (status === 'current' ? '<span class="block-status active">\u25B6 Agora</span>' : '');
    html += '<div class="block-card glass-card ' + status + '" style="animation-delay:' + (i * 0.05) + 's">'
      + '<div class="block-top"><span class="block-icon">' + icon + '</span>'
      + '<span class="block-type">' + label + '</span>'
      + '<span class="block-duration">' + (b.duration_minutes || 0) + ' min</span></div>'
      + (b.mode ? '<div class="block-mode">' + b.mode + '</div>' : '')
      + statusText + '</div>';
  }
  document.getElementById('timeline').innerHTML = html;

  if (data.current_block) {
    currentBlock = data.current_block;
    var cb = data.current_block;
    document.getElementById('cur-icon').textContent = TYPE_ICONS[cb.type] || '';
    document.getElementById('cur-type').textContent = TYPE_LABELS[cb.type] || cb.type;
    document.getElementById('cur-mode').textContent = cb.mode || '';
    document.getElementById('cur-duration').textContent = (cb.duration_minutes || 0) + ' min';
    document.getElementById('cur-items').textContent = cb.target_items ? cb.target_items + ' itens' : '';
    document.getElementById('current-detail').style.display = 'block';
  } else {
    document.getElementById('current-detail').style.display = 'none';
  }
}

function startBlock() {
  if (!currentBlock) return;
  var t = currentBlock.type;
  if (t === 'srs_drill') window.location.href = '/drill';
  else if (t === 'listening' || t === 'shadowing') window.location.href = '/library';
  else if (t === 'conversa') window.location.href = '/conversa';
  else if (t === 'break') completeBlock();
  else window.location.href = '/drill';
}

function completeBlock() {
  fetch('/api/plan/block/complete', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(function(r){return r.json()})
    .then(function(){loadPlan()});
}

function loadPlan() {
  fetch('/api/plan/today').then(function(r){return r.json()}).then(renderPlan);
}

function loadFatigue() {
  fetch('/api/fatigue/status').then(function(r){return r.json()}).then(function(d) {
    var score = d.fatigue_score || d.score || 0;
    document.getElementById('fat-score').textContent = Math.round(score);
    document.getElementById('fat-fill').style.width = Math.min(score, 100) + '%';
    var color = score < 40 ? '#34d399' : score < 70 ? '#fbbf24' : '#f87171';
    document.getElementById('fat-fill').style.background = color;
    document.getElementById('fat-score').style.color = color;
    var mins = d.minutes_active || d.session_minutes || 0;
    document.getElementById('fat-minutes').textContent = Math.round(mins) + ' min ativo';
  });
}

function adjustPlan() {
  var btn = document.querySelector('.btn-adjust');
  btn.disabled = true; btn.textContent = '...';
  fetch('/api/plan/adjust', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(function(r){return r.json()})
    .then(function() {
      document.getElementById('adjust-msg').style.display = 'block';
      btn.textContent = 'Ajustar plano'; btn.disabled = false;
      setTimeout(function(){document.getElementById('adjust-msg').style.display='none'}, 2000);
      loadPlan();
    })
    .catch(function(){btn.textContent='Ajustar plano';btn.disabled=false;});
}

loadPlan();
loadFatigue();
setInterval(function(){
  fetch('/api/plan/progress').then(function(r){return r.json()}).then(function(d){
    var pct = Math.round(d.completed_pct || 0);
    document.getElementById('overall-pct').textContent = pct + '%';
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('blocks-done').textContent = d.completed_blocks || 0;
    document.getElementById('blocks-total').textContent = 'de ' + (d.total_blocks || 0) + ' blocos';
  });
}, 30000);
</script>
</body></html>"""


# ── Chunks HTML ───────────────────────────────────────────────

CHUNKS_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Oxe — Famílias de Chunks</title>
<style>
  @keyframes fadeIn { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    padding: 0 16px 100px 16px; -webkit-font-smoothing: antialiased;
  }
  .top-bar {
    display: flex; align-items: center; gap: 12px;
    padding: 56px 0 12px 0;
  }
  .back-btn {
    width: 36px; height: 36px; border-radius: 12px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
    display: flex; align-items: center; justify-content: center;
    color: #fafafa; font-size: 18px; text-decoration: none;
  }
  .page-title { font-size: 1.3em; font-weight: 700; flex:1; }
  .count-badge {
    background: rgba(124,92,252,0.15); color: #7C5CFC; padding: 4px 12px;
    border-radius: 20px; font-size: 0.78em; font-weight: 600;
  }
  .glass-card {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px; backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  }
  .search-wrap {
    margin-bottom: 16px; animation: fadeIn 0.3s ease-out;
  }
  .search-input {
    width: 100%; padding: 12px 18px; border-radius: 14px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
    color: #fafafa; font-size: 0.95em; outline: none;
  }
  .search-input::placeholder { color: #7a7a8e; }
  .search-input:focus { border-color: #3B82F6; }
  .seed-bar {
    display: flex; align-items: center; gap: 12px; padding: 14px 20px;
    margin-bottom: 16px; animation: fadeIn 0.3s ease-out 0.05s both;
  }
  .seed-bar label { color: #7a7a8e; font-size: 0.82em; }
  .seed-input {
    width: 56px; padding: 6px 10px; border-radius: 8px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
    color: #fafafa; font-size: 0.9em; text-align: center; outline: none;
  }
  .btn-seed {
    padding: 8px 20px; border-radius: 12px; border: none; cursor: pointer;
    background: linear-gradient(135deg, #7C5CFC, #3B82F6); color: #fff;
    font-weight: 600; font-size: 0.85em; margin-left: auto;
  }
  .btn-seed:active { transform: scale(0.97); }
  .seed-msg { color: #34d399; font-size: 0.8em; display: none; margin: -8px 0 12px 20px; }
  .chunk-card {
    padding: 16px 20px; margin-bottom: 10px; cursor: pointer;
    animation: fadeIn 0.3s ease-out both; transition: border-color 0.2s;
  }
  .chunk-card:active { border-color: rgba(255,255,255,0.15); }
  .chunk-top { display: flex; align-items: center; gap: 12px; }
  .chunk-root { font-size: 1.05em; font-weight: 700; flex: 1; line-height: 1.3; }
  .chunk-wc { color: #7a7a8e; font-size: 0.75em; white-space: nowrap; }
  .rank-bar-track {
    height: 4px; border-radius: 2px; background: rgba(255,255,255,0.08); margin-top: 8px;
  }
  .rank-bar-fill {
    height: 100%; border-radius: 2px;
    background: linear-gradient(90deg, #3B82F6, #7C5CFC);
  }
  .score-row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  .score-badge {
    font-size: 0.7em; padding: 3px 8px; border-radius: 8px;
    background: rgba(255,255,255,0.06); color: #7a7a8e;
  }
  .score-badge .val { color: #fafafa; font-weight: 600; }
  .variant-panel {
    max-height: 0; overflow: hidden; transition: max-height 0.3s ease;
  }
  .variant-panel.open { max-height: 600px; }
  .variant-list { padding: 10px 0 4px 0; }
  .variant-item {
    display: flex; align-items: center; gap: 10px; padding: 8px 0;
    border-top: 1px solid rgba(255,255,255,0.04);
  }
  .variant-form { font-size: 0.9em; flex: 1; }
  .variant-source {
    font-size: 0.68em; padding: 2px 8px; border-radius: 6px;
    background: rgba(59,130,246,0.12); color: #3B82F6;
  }
  .variant-count { color: #7a7a8e; font-size: 0.75em; }
  .loading-more { text-align: center; padding: 20px; color: #7a7a8e; font-size: 0.82em; display: none; }
</style>
</head><body>

<div class="top-bar">
  <a href="/" class="back-btn">&larr;</a>
  <div class="page-title">Famílias de Chunks</div>
  <div class="count-badge" id="family-count">0</div>
</div>

<div class="search-wrap">
  <input type="text" class="search-input" id="search-input" placeholder="Buscar chunk..." oninput="filterChunks()">
</div>

<div class="seed-bar glass-card">
  <label>Seed ao SRS:</label>
  <input type="number" class="seed-input" id="seed-limit" value="10" min="1" max="100">
  <button class="btn-seed" onclick="seedChunks()">Adicionar ao SRS</button>
</div>
<div class="seed-msg" id="seed-msg"></div>

<div class="chunk-list" id="chunk-list"></div>
<div class="loading-more" id="loading-more">Carregando...</div>

{tab_bar}

<script>
var allFamilies = [];
var displayedFamilies = [];
var loadedCount = 0;
var pageSize = 50;
var loading = false;
var expandedId = null;

function renderChunk(f, idx) {
  var rankPct = Math.min((f.composite_rank || 0) * 100, 100);
  var freq = Math.round((f.frequency_score || 0) * 100);
  var nat = Math.round((f.naturalness_score || 0) * 100);
  var bahia = Math.round((f.bahia_relevance || 0) * 100);
  return '<div class="chunk-card glass-card" data-id="' + f.id + '" onclick="toggleVariants(' + f.id + ', this)">'
    + '<div class="chunk-top">'
    + '<div class="chunk-root">' + escHtml(f.root_form) + '</div>'
    + '<div class="chunk-wc">' + (f.word_count || 0) + ' palavras</div>'
    + '</div>'
    + '<div class="rank-bar-track"><div class="rank-bar-fill" style="width:' + rankPct + '%"></div></div>'
    + '<div class="score-row">'
    + '<div class="score-badge">freq <span class="val">' + freq + '</span></div>'
    + '<div class="score-badge">nat <span class="val">' + nat + '</span></div>'
    + '<div class="score-badge">bahia <span class="val">' + bahia + '</span></div>'
    + '</div>'
    + '<div class="variant-panel" id="vp-' + f.id + '"></div>'
    + '</div>';
}

function escHtml(s) {
  var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML;
}

function renderList(families) {
  var html = '';
  for (var i = 0; i < families.length; i++) {
    html += renderChunk(families[i], i);
  }
  document.getElementById('chunk-list').innerHTML = html;
}

function filterChunks() {
  var q = (document.getElementById('search-input').value || '').toLowerCase();
  if (!q) { displayedFamilies = allFamilies.slice(); }
  else {
    displayedFamilies = allFamilies.filter(function(f) {
      return (f.root_form || '').toLowerCase().indexOf(q) !== -1;
    });
  }
  renderList(displayedFamilies);
}

function toggleVariants(familyId, cardEl) {
  var panel = document.getElementById('vp-' + familyId);
  if (!panel) return;
  if (expandedId === familyId) {
    panel.classList.remove('open');
    panel.innerHTML = '';
    expandedId = null;
    return;
  }
  if (expandedId !== null) {
    var old = document.getElementById('vp-' + expandedId);
    if (old) { old.classList.remove('open'); old.innerHTML = ''; }
  }
  expandedId = familyId;
  panel.innerHTML = '<div style="padding:10px 0;color:#7a7a8e;font-size:0.82em">Carregando...</div>';
  panel.classList.add('open');
  fetch('/api/chunks/family/' + familyId + '/variants')
    .then(function(r){return r.json()})
    .then(function(variants) {
      var html = '<div class="variant-list">';
      for (var i = 0; i < variants.length; i++) {
        var v = variants[i];
        var srcColor = {story:'#34d399',podcast:'#fbbf24',conversation:'#f87171',corpus:'#3B82F6'}[v.source] || '#7a7a8e';
        html += '<div class="variant-item">'
          + '<div class="variant-form">' + escHtml(v.variant_form) + '</div>'
          + '<div class="variant-source" style="background:' + srcColor + '22;color:' + srcColor + '">' + (v.source || '?') + '</div>'
          + '<div class="variant-count">' + (v.occurrence_count || 0) + 'x</div>'
          + '</div>';
      }
      html += '</div>';
      panel.innerHTML = html;
    })
    .catch(function() { panel.innerHTML = '<div style="padding:10px 0;color:#f87171;font-size:0.82em">Erro</div>'; });
}

function seedChunks() {
  var limit = parseInt(document.getElementById('seed-limit').value) || 10;
  var btn = document.querySelector('.btn-seed');
  btn.disabled = true; btn.textContent = '...';
  fetch('/api/chunks/seed', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({limit: limit})})
    .then(function(r){return r.json()})
    .then(function(d) {
      var msg = document.getElementById('seed-msg');
      msg.textContent = (d.seeded || 0) + ' chunks adicionados!';
      msg.style.display = 'block';
      btn.textContent = 'Adicionar ao SRS'; btn.disabled = false;
      setTimeout(function(){msg.style.display='none'}, 3000);
    })
    .catch(function(){btn.textContent='Adicionar ao SRS';btn.disabled=false;});
}

function loadFamilies(offset) {
  if (loading) return;
  loading = true;
  document.getElementById('loading-more').style.display = 'block';
  fetch('/api/chunks/families?limit=' + pageSize + '&offset=' + offset)
    .then(function(r){return r.json()})
    .then(function(data) {
      var families = Array.isArray(data) ? data : (data.families || []);
      for (var i = 0; i < families.length; i++) allFamilies.push(families[i]);
      loadedCount = allFamilies.length;
      document.getElementById('family-count').textContent = loadedCount;
      displayedFamilies = allFamilies.slice();
      filterChunks();
      loading = false;
      document.getElementById('loading-more').style.display = 'none';
      if (families.length < pageSize) { window._allLoaded = true; }
    })
    .catch(function(){ loading = false; document.getElementById('loading-more').style.display = 'none'; });
}

window.addEventListener('scroll', function() {
  if (window._allLoaded || loading) return;
  if ((window.innerHeight + window.scrollY) >= (document.body.offsetHeight - 200)) {
    loadFamilies(loadedCount);
  }
});

loadFamilies(0);
</script>
</body></html>"""


# ── Sentence Assembly Page ─────────────────────────────────────

ASSEMBLY_HTML = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Montar Frase</title>
<style>
  @keyframes fadeIn { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
  @keyframes popIn { 0%{transform:scale(0.6);opacity:0} 60%{transform:scale(1.08)} 100%{transform:scale(1);opacity:1} }
  @keyframes glowGreen { 0%,100%{box-shadow:0 0 8px rgba(52,211,153,0.3)} 50%{box-shadow:0 0 20px rgba(52,211,153,0.7)} }
  @keyframes glowYellow { 0%,100%{box-shadow:0 0 8px rgba(250,204,21,0.3)} 50%{box-shadow:0 0 20px rgba(250,204,21,0.7)} }
  @keyframes glowRed { 0%,100%{box-shadow:0 0 8px rgba(248,113,113,0.3)} 50%{box-shadow:0 0 20px rgba(248,113,113,0.7)} }
  @keyframes shake { 0%,100%{transform:translateX(0)} 20%{transform:translateX(-6px)} 40%{transform:translateX(6px)} 60%{transform:translateX(-4px)} 80%{transform:translateX(4px)} }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0b; color: #fafafa; font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column;
    -webkit-user-select: none; user-select: none;
    padding-bottom: 76px;
  }

  .topbar {
    padding: 16px 20px 14px; display: flex; align-items: center; gap: 14px;
    position: sticky; top: 0; z-index: 10; background: #0a0a0b;
    border-bottom: 2px solid transparent;
    border-image: linear-gradient(90deg, #3B82F6, #7C5CFC) 1;
  }
  .back-btn {
    width: 36px; height: 36px; border-radius: 12px; border: none;
    background: rgba(255,255,255,0.06); color: #fafafa; font-size: 1.2em;
    display: flex; align-items: center; justify-content: center;
    -webkit-tap-highlight-color: transparent; cursor: pointer;
  }
  .topbar h1 { font-size: 1.15em; font-weight: 600; }

  .diff-bar {
    display: flex; gap: 8px; padding: 14px 20px; justify-content: center;
  }
  .diff-btn {
    padding: 6px 18px; border-radius: 20px; border: 1px solid rgba(255,255,255,0.1);
    background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.5);
    font-size: 0.82em; font-weight: 500; cursor: pointer;
    -webkit-tap-highlight-color: transparent; transition: all 0.2s;
  }
  .diff-btn.active {
    background: rgba(59,130,246,0.15); color: #3B82F6;
    border-color: rgba(59,130,246,0.4);
  }

  .assembly-zone {
    margin: 12px 20px; min-height: 80px; border: 2px dashed rgba(255,255,255,0.15);
    border-radius: 16px; padding: 14px; display: flex; flex-wrap: wrap;
    gap: 8px; align-items: center; justify-content: center;
    transition: border-color 0.3s, box-shadow 0.3s; position: relative;
  }
  .assembly-zone.empty::after {
    content: 'Toque nos chunks abaixo para montar a frase';
    color: rgba(255,255,255,0.25); font-size: 0.85em; text-align: center;
  }
  .assembly-zone.correct { border-color: #34d399; animation: glowGreen 1.5s infinite; }
  .assembly-zone.close { border-color: #facc15; animation: glowYellow 1.5s infinite; }
  .assembly-zone.wrong { border-color: #f87171; animation: glowRed 1.5s infinite; }
  .assembly-zone.shake { animation: shake 0.4s; }

  .pill {
    padding: 10px 18px; border-radius: 24px;
    background: rgba(255,255,255,0.08); border: 1.5px solid rgba(255,255,255,0.1);
    color: #fafafa; font-size: 0.92em; font-weight: 500;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
    transition: all 0.25s ease; animation: popIn 0.3s ease-out;
  }
  .pill:active { transform: scale(0.95); }
  .pill.in-zone {
    background: rgba(59,130,246,0.15); border-color: rgba(59,130,246,0.5);
    color: #60a5fa;
  }
  .pill.used {
    opacity: 0.25; pointer-events: none; transform: scale(0.9);
  }

  .bank-label {
    padding: 18px 20px 8px; font-size: 0.75em; color: rgba(255,255,255,0.35);
    text-transform: uppercase; letter-spacing: 1px; font-weight: 600;
  }
  .bank {
    display: flex; flex-wrap: wrap; gap: 10px; padding: 0 20px 16px;
    justify-content: center;
  }

  .btn-row {
    display: flex; gap: 10px; padding: 8px 20px; justify-content: center;
  }
  .btn {
    flex: 1; max-width: 200px; padding: 14px 0; border-radius: 14px; border: none;
    font-size: 0.95em; font-weight: 600; cursor: pointer;
    -webkit-tap-highlight-color: transparent; transition: all 0.2s;
  }
  .btn-primary {
    background: linear-gradient(135deg, #3B82F6, #7C5CFC); color: #fff;
  }
  .btn-primary:disabled { opacity: 0.4; pointer-events: none; }
  .btn-secondary {
    background: rgba(255,255,255,0.06); color: rgba(255,255,255,0.6);
    border: 1px solid rgba(255,255,255,0.1);
  }

  .audio-btn {
    width: 48px; height: 48px; border-radius: 50%; border: none;
    background: rgba(59,130,246,0.15); color: #3B82F6;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; margin: 0 auto 8px; transition: all 0.2s;
    -webkit-tap-highlight-color: transparent;
  }
  .audio-btn:active { transform: scale(0.9); }
  .audio-btn svg { width: 24px; height: 24px; fill: currentColor; }
  .audio-btn.hidden { display: none; }

  .feedback {
    margin: 12px 20px; padding: 16px; border-radius: 14px;
    font-size: 0.92em; line-height: 1.5; text-align: center;
    display: none; animation: fadeIn 0.3s;
  }
  .feedback.show { display: block; }
  .feedback.correct { background: rgba(52,211,153,0.1); color: #34d399; border: 1px solid rgba(52,211,153,0.2); }
  .feedback.close { background: rgba(250,204,21,0.1); color: #facc15; border: 1px solid rgba(250,204,21,0.2); }
  .feedback.wrong { background: rgba(248,113,113,0.1); color: #f87171; border: 1px solid rgba(248,113,113,0.2); }

  .score-display {
    text-align: center; padding: 8px; font-size: 1.8em; font-weight: 700;
    display: none;
  }
  .score-display.show { display: block; animation: popIn 0.4s; }
  .score-display.s100 { color: #34d399; }
  .score-display.s90 { color: #34d399; }
  .score-display.s70 { color: #facc15; }
  .score-display.s30 { color: #f87171; }

  .stats-bar {
    display: flex; justify-content: center; gap: 24px; padding: 10px 20px;
    font-size: 0.78em; color: rgba(255,255,255,0.4);
  }
  .stats-bar span { font-weight: 600; color: rgba(255,255,255,0.7); }

  .loader {
    text-align: center; padding: 40px; color: rgba(255,255,255,0.4);
    font-size: 0.9em; display: none;
  }
  .loader.show { display: block; }
</style>
</head><body>

<div class="topbar">
  <button class="back-btn" onclick="location.href='/'">&#8592;</button>
  <h1>Montar Frase</h1>
</div>

<div class="diff-bar">
  <button class="diff-btn" data-diff="easy" onclick="setDifficulty('easy')">F&aacute;cil</button>
  <button class="diff-btn active" data-diff="medium" onclick="setDifficulty('medium')">M&eacute;dio</button>
  <button class="diff-btn" data-diff="hard" onclick="setDifficulty('hard')">Dif&iacute;cil</button>
</div>

<div class="stats-bar" id="statsBar">
  Hoje: <span id="statAttempts">0</span> tentativas &bull; M&eacute;dia: <span id="statAvg">0</span>
</div>

<button class="audio-btn hidden" id="audioBtn" onclick="playAudio()">
  <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0014 8.5v7a4.49 4.49 0 002.5-3.5zM14 3.23v2.06a6.49 6.49 0 010 13.42v2.06A8.49 8.49 0 0014 3.23z"/></svg>
</button>

<div class="assembly-zone empty" id="assemblyZone"></div>

<div class="bank-label">Chunks dispon&iacute;veis</div>
<div class="bank" id="chunkBank"></div>

<div class="btn-row">
  <button class="btn btn-primary" id="checkBtn" onclick="checkAnswer()" disabled>Verificar</button>
  <button class="btn btn-secondary" onclick="newChallenge()">Nova Frase</button>
</div>

<div class="score-display" id="scoreDisplay"></div>
<div class="feedback" id="feedback"></div>
<div class="loader" id="loader">Gerando desafio...</div>

{tab_bar}

<script>
var currentChallenge = null;
var difficulty = 'medium';
var assembledChunks = [];
var audioEl = null;
var bankPillMap = {};

function setDifficulty(d) {
  difficulty = d;
  var btns = document.querySelectorAll('.diff-btn');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = btns[i].dataset.diff === d ? 'diff-btn active' : 'diff-btn';
  }
  newChallenge();
}

function newChallenge() {
  assembledChunks = [];
  currentChallenge = null;
  bankPillMap = {};
  document.getElementById('assemblyZone').innerHTML = '';
  document.getElementById('assemblyZone').className = 'assembly-zone empty';
  document.getElementById('chunkBank').innerHTML = '';
  document.getElementById('feedback').className = 'feedback';
  document.getElementById('feedback').textContent = '';
  document.getElementById('scoreDisplay').className = 'score-display';
  document.getElementById('checkBtn').disabled = true;
  document.getElementById('audioBtn').className = 'audio-btn hidden';
  document.getElementById('loader').className = 'loader show';

  fetch('/api/assembly/challenge?difficulty=' + difficulty)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      document.getElementById('loader').className = 'loader';
      if (data.error) {
        document.getElementById('feedback').className = 'feedback show wrong';
        document.getElementById('feedback').textContent = data.error;
        return;
      }
      currentChallenge = data;
      renderBank(data.all_options);
    })
    .catch(function() {
      document.getElementById('loader').className = 'loader';
      document.getElementById('feedback').className = 'feedback show wrong';
      document.getElementById('feedback').textContent = 'Erro de conexao. Tenta de novo!';
    });
}

function renderBank(options) {
  var bank = document.getElementById('chunkBank');
  bank.innerHTML = '';
  for (var i = 0; i < options.length; i++) {
    var pill = document.createElement('div');
    pill.className = 'pill';
    pill.textContent = options[i];
    pill.dataset.chunk = options[i];
    pill.dataset.idx = String(i);
    pill.onclick = (function(chunk, idx) {
      return function() { addToZone(chunk, idx); };
    })(options[i], i);
    bank.appendChild(pill);
    bankPillMap[i] = pill;
  }
}

function addToZone(chunk, bankIdx) {
  var bp = bankPillMap[bankIdx];
  if (!bp || bp.className.indexOf('used') !== -1) return;
  assembledChunks.push({chunk: chunk, bankIdx: bankIdx});
  bp.className = 'pill used';

  var zone = document.getElementById('assemblyZone');
  zone.classList.remove('empty');

  var pill = document.createElement('div');
  pill.className = 'pill in-zone';
  pill.textContent = chunk;
  pill.style.animation = 'popIn 0.25s ease-out';
  pill.onclick = (function(c, bi, zp) {
    return function() { removeFromZone(c, bi, zp); };
  })(chunk, bankIdx, pill);
  zone.appendChild(pill);

  document.getElementById('checkBtn').disabled = false;
}

function removeFromZone(chunk, bankIdx, zonePill) {
  for (var i = 0; i < assembledChunks.length; i++) {
    if (assembledChunks[i].chunk === chunk && assembledChunks[i].bankIdx === bankIdx) {
      assembledChunks.splice(i, 1);
      break;
    }
  }
  zonePill.parentNode.removeChild(zonePill);
  var bp = bankPillMap[bankIdx];
  if (bp) bp.className = 'pill';

  var zone = document.getElementById('assemblyZone');
  if (assembledChunks.length === 0) {
    zone.className = 'assembly-zone empty';
    document.getElementById('checkBtn').disabled = true;
  }
}

function checkAnswer() {
  if (!currentChallenge || assembledChunks.length === 0) return;
  document.getElementById('checkBtn').disabled = true;

  var order = [];
  for (var i = 0; i < assembledChunks.length; i++) order.push(assembledChunks[i].chunk);

  fetch('/api/assembly/check', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      challenge_id: currentChallenge.challenge_id,
      submitted_order: order
    })
  })
  .then(function(r) { return r.json(); })
  .then(function(result) {
    var zone = document.getElementById('assemblyZone');
    var fb = document.getElementById('feedback');
    var sd = document.getElementById('scoreDisplay');

    var scoreClass = 's30';
    if (result.score >= 100) scoreClass = 's100';
    else if (result.score >= 90) scoreClass = 's90';
    else if (result.score >= 70) scoreClass = 's70';
    sd.className = 'score-display show ' + scoreClass;
    sd.textContent = String(result.score);

    var fbClass = 'wrong';
    if (result.score >= 90) fbClass = 'correct';
    else if (result.score >= 70) fbClass = 'close';
    fb.className = 'feedback show ' + fbClass;
    fb.textContent = result.feedback;

    zone.className = 'assembly-zone';
    if (result.score >= 90) zone.className += ' correct';
    else if (result.score >= 70) zone.className += ' close';
    else zone.className += ' wrong shake';

    if (result.audio_file) {
      currentChallenge._result_audio = result.audio_file;
      document.getElementById('audioBtn').className = 'audio-btn';
      playAudio();
    }

    loadStats();
  });
}

function playAudio() {
  var file = null;
  if (currentChallenge && currentChallenge._result_audio) {
    file = currentChallenge._result_audio;
  } else if (currentChallenge && currentChallenge.audio_file) {
    file = currentChallenge.audio_file;
  }
  if (!file) return;
  if (audioEl) { audioEl.pause(); audioEl = null; }
  audioEl = new Audio('/audio/' + file);
  audioEl.play().catch(function() {});
}

function loadStats() {
  fetch('/api/assembly/stats')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      document.getElementById('statAttempts').textContent = String(data.attempts || 0);
      document.getElementById('statAvg').textContent = String(data.average_score || 0);
    })
    .catch(function() {});
}

newChallenge();
loadStats();
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
        elif path == "/api/search/live":
            q = query.get("q", [""])[0]
            self._dict_live_lookup(q)
        elif path == "/api/search/history":
            self._dict_history()

        # ── Word detail endpoints ──
        elif path.startswith("/api/word/") and path.endswith("/definition"):
            word_id = int(path.split("/")[3])
            self._dict_definition(word_id)
        elif path.startswith("/api/word/") and path.endswith("/examples"):
            word_id = int(path.split("/")[3])
            self._dict_examples(word_id)
        elif path.startswith("/api/word/") and path.endswith("/pronunciation"):
            word_id = int(path.split("/")[3])
            self._dict_pronunciation(word_id)
        elif path.startswith("/api/word/") and path.endswith("/expressions"):
            word_id = int(path.split("/")[3])
            self._dict_expressions(word_id)
        elif path.startswith("/api/word/") and path.endswith("/conjugation"):
            word_id = int(path.split("/")[3])
            self._dict_conjugation(word_id)
        elif path.startswith("/api/word/") and path.endswith("/synonyms"):
            word_id = int(path.split("/")[3])
            self._dict_synonyms(word_id)
        elif path.startswith("/api/word/") and path.endswith("/chunks"):
            word_id = int(path.split("/")[3])
            self._dict_chunks(word_id)
        elif path.startswith("/api/word/") and path.endswith("/audio"):
            word_id = int(path.split("/")[3])
            self._dict_audio(word_id)
        elif path.startswith("/api/word/") and path.endswith("/state"):
            word_id = int(path.split("/")[3])
            self._dict_state(word_id)

        # ── Drill ──
        elif path == "/drill":
            self._html(DRILL_HTML.replace("{tab_bar}", TAB_BAR_HTML("treinar")))
        elif path == "/api/drill/next":
            self._drill_next_chunk()

        # ── Speech Ladder ──
        elif path == "/speech":
            self._html(SPEECH_HTML.replace("{tab_bar}", TAB_BAR_HTML("inicio")))

        # ── Conversa ──
        elif path == "/conversa":
            self._html(CONVERSA_HTML.replace("{tab_bar}", TAB_BAR_HTML("conversa")))
        elif path == "/api/conversa/analysis/history":
            lim = int(query.get("limit", ["10"])[0])
            self._json(get_analysis_history(limit=lim))
        elif path.startswith("/api/conversa/") and path.endswith("/analysis"):
            parts = path.split("/")
            if len(parts) == 5 and parts[3].isdigit():
                sid = int(parts[3])
                result = get_conversation_analysis(sid)
                if result is None:
                    self._json({"error": "Sessão não encontrada"}, status=404)
                else:
                    self._json(result)
            else:
                self.send_error(404)

        # ── Plan ──
        elif path == "/plan":
            self._html(PLAN_HTML.replace("{tab_bar}", TAB_BAR_HTML("inicio")))

        # ── Chunks ──
        elif path == "/chunks":
            self._html(CHUNKS_HTML.replace("{tab_bar}", TAB_BAR_HTML("inicio")))

        # ── Sentence Assembly ──
        elif path == "/assembly":
            self._html(ASSEMBLY_HTML.replace("{tab_bar}", TAB_BAR_HTML("treinar")))

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

        # ── Milestones ──
        elif path == "/api/milestones":
            self._milestones_list()
        elif path == "/api/milestones/unnotified":
            self._milestones_unnotified()

        # ── Content Segments ──
        elif path.startswith("/api/content/segments/"):
            # /api/content/segments/story/5 or /api/content/segments/podcast/3
            parts = path.split("/")
            if len(parts) == 6:
                ct = parts[4]
                cid = int(parts[5])
                self._content_segments(ct, cid)
            else:
                self.send_error(404)

        # ── Content Ladder ──
        elif path == "/api/content/level":
            self._json({"level": get_learner_level()})
        elif path == "/api/content/recommend":
            mode = query.get("mode", ["compression"])[0]
            limit = int(query.get("limit", ["10"])[0])
            self._json(select_content_for_mode(mode, limit=limit))

        # ── Content Router (Re-encounter) ──
        elif path == "/api/content/reencounter":
            lim = int(query.get("limit", ["5"])[0])
            self._json(get_reencounter_queue(limit=lim))
        elif path == "/api/content/reencounter/stats":
            days = int(query.get("days", ["7"])[0])
            self._json(get_reencounter_stats(days=days))

        # ── Listening Layers ──
        elif path == "/api/listening/layers":
            self._json(LISTENING_LAYERS)
        elif path.startswith("/api/listening/drill/") and path.count("/") == 4:
            chunk_id = int(path.split("/")[4])
            self._json(get_listening_drill(chunk_id))

        # ── Sentence Assembly ──
        elif path == "/api/assembly/challenge":
            diff = query.get("difficulty", ["medium"])[0]
            self._json(get_assembly_challenge(diff))
        elif path == "/api/assembly/stats":
            days = int(query.get("days", ["7"])[0])
            self._json(get_assembly_stats(days))

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

        # ── Listening Layers POST ──
        elif path == "/api/listening/advance":
            chunk_id = body.get("chunk_id", 0)
            current_layer = body.get("current_layer", "clean")
            success = body.get("success", False)
            result = advance_listening_layer(chunk_id, current_layer, success)
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

        # ── Milestones POST ──
        elif path == "/api/milestones/notify":
            milestone_id = body.get("milestone_id", 0)
            conn = get_conn()
            conn.execute("UPDATE milestones SET notified = 1 WHERE id = ?", (milestone_id,))
            conn.commit()
            conn.close()
            self._json({"ok": True})

        elif path == "/api/content/segments/log":
            ct = body.get("content_type", "story")
            cid = body.get("content_id", 0)
            segments = body.get("segments", [])
            conn = get_conn()
            for seg in segments:
                conn.execute(
                    """INSERT OR REPLACE INTO content_segments
                       (content_type, content_id, segment_index, text, comprehension_pct, replays, latency_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (ct, cid, seg.get("index", 0), seg.get("text", ""),
                     seg.get("comprehension_pct", 0), seg.get("replays", 0), seg.get("latency_ms")),
                )
            conn.commit()
            conn.close()
            self._json({"ok": True, "logged": len(segments)})

        # ── Conversa (stage-scaffolded) ──
        elif path == "/api/conversa/start":
            self._conversa_start(body)
        elif path == "/api/conversa/turn":
            self._conversa_turn(body)
        elif path == "/api/conversa/end":
            self._conversa_end(body)
        elif path.startswith("/api/conversa/") and path.endswith("/generate-drills"):
            parts = path.split("/")
            if len(parts) == 5 and parts[3].isdigit():
                sid = int(parts[3])
                analysis = get_conversation_analysis(sid)
                if analysis is None:
                    self._json({"error": "Sessão não encontrada"}, status=404)
                else:
                    errors = analysis.get("erros", [])
                    added = generate_correction_drills(errors)
                    self._json({"ok": True, "chunks_added": added, "count": len(added)})
            else:
                self.send_error(404)

        # ── Content Router (Re-encounter) ──
        elif path == "/api/content/reencounter/log":
            ct = body.get("content_type", "story")
            cid = body.get("content_id", 0)
            chunks = body.get("chunks", [])
            row_id = log_reencounter(ct, cid, chunks)
            self._json({"ok": True, "event_id": row_id})

        # ── Sentence Assembly POST ──
        elif path == "/api/assembly/check":
            cid = body.get("challenge_id", "")
            order = body.get("submitted_order", [])
            self._json(check_assembly(cid, order))

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
        now = time.time()
        with _dashboard_cache_lock:
            if _dashboard_cache["data"] and now - _dashboard_cache["ts"] < 10:
                self._json(_dashboard_cache["data"])
                return

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

        result = {
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
            "milestones": self._compute_milestones(acquired, speech_stage),
        }
        with _dashboard_cache_lock:
            _dashboard_cache["data"] = result
            _dashboard_cache["ts"] = time.time()
        self._json(result)

    def _milestones_list(self):
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM milestones ORDER BY achieved_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _milestones_unnotified(self):
        try:
            dist = get_state_distribution()
            acquired = dist.get("AUTOMATIC_CLEAN", 0) + dist.get("AUTOMATIC_NATIVE", 0) + dist.get("AVAILABLE_OUTPUT", 0)
            speech_stage = get_current_stage()
            milestones = self._compute_milestones(acquired, speech_stage)
            self._json(milestones)
        except Exception as e:
            self._json({"error": str(e)})

    def _content_segments(self, content_type, content_id):
        conn = get_conn()
        rows = conn.execute(
            """SELECT * FROM content_segments
               WHERE content_type = ? AND content_id = ?
               ORDER BY segment_index ASC""",
            (content_type, content_id),
        ).fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _compute_milestones(self, acquired_count, speech_stage):
        """Compute milestone progress and record newly achieved ones."""
        milestones_def = [
            ("acquired", "10", 10), ("acquired", "25", 25), ("acquired", "50", 50),
            ("acquired", "100", 100), ("acquired", "250", 250), ("acquired", "500", 500),
            ("acquired", "1000", 1000), ("acquired", "2500", 2500), ("acquired", "5000", 5000),
            ("speech", "2", 2), ("speech", "3", 3), ("speech", "4", 4),
            ("speech", "5", 5), ("speech", "6", 6),
        ]
        achieved = []
        next_milestone = None
        try:
            conn = get_conn()
            for mtype, mkey, threshold in milestones_def:
                current = acquired_count if mtype == "acquired" else speech_stage
                if current >= threshold:
                    # Record if not already
                    conn.execute(
                        "INSERT OR IGNORE INTO milestones (milestone_type, milestone_key, milestone_data) "
                        "VALUES (?, ?, ?)",
                        (mtype, mkey, json.dumps({"threshold": threshold})),
                    )
                    achieved.append({"type": mtype, "key": mkey, "threshold": threshold})
                elif next_milestone is None:
                    next_milestone = {"type": mtype, "key": mkey, "threshold": threshold, "current": current}
            conn.commit()

            # Check for unnotified milestones
            new_rows = conn.execute(
                "SELECT milestone_type, milestone_key FROM milestones WHERE notified = 0"
            ).fetchall()
            new_milestones = [{"type": r[0], "key": r[1]} for r in new_rows]
            if new_rows:
                conn.execute("UPDATE milestones SET notified = 1 WHERE notified = 0")
                conn.commit()
            conn.close()
        except Exception:
            new_milestones = []

        return {
            "achieved_count": len(achieved),
            "next": next_milestone,
            "new": new_milestones,
        }

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
        now = time.time()
        with _home_stats_cache_lock:
            if _home_stats_cache["data"] and now - _home_stats_cache["ts"] < 10:
                self._json(_home_stats_cache["data"])
                return

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
        with _home_stats_cache_lock:
            _home_stats_cache["data"] = resp
            _home_stats_cache["ts"] = time.time()
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

    def _dict_live_lookup(self, word):
        """Live GPT lookup for words not in word_bank."""
        if not word or not word.strip():
            self._json({"error": "Palavra vazia"}, status=400)
            return
        word = word.strip()
        try:
            from dictionary_engine import (
                get_definition, get_examples, get_pronunciation_data,
                get_expressions, get_conjugation, get_synonyms, get_word_chunks,
            )
            result = {
                "word": word,
                "word_id": -1,
                "is_live": True,
                "definition": get_definition(word),
                "examples": get_examples(word),
                "pronunciation": get_pronunciation_data(word),
                "expressions": get_expressions(word),
                "conjugation": get_conjugation(word),
                "synonyms": get_synonyms(word),
                "chunks": get_word_chunks(word),
            }
            self._json(result)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

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

    def _resolve_word(self, word_id):
        """Look up a word by ID. Returns the word string or None."""
        conn = get_conn()
        row = conn.execute(
            "SELECT word FROM word_bank WHERE id = ?", (word_id,)
        ).fetchone()
        conn.close()
        return row["word"] if row else None

    def _dict_definition(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            data = get_definition_cached(word_id, word)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_examples(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            data = get_examples_cached(word_id, word)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_pronunciation(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            data = get_pronunciation_cached(word_id, word)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_expressions(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            data = get_expressions_cached(word_id, word)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_conjugation(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            data = get_conjugation_cached(word_id, word)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_synonyms(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            data = get_synonyms_cached(word_id, word)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_chunks(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            data = get_word_chunks_cached(word_id, word)
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_audio(self, word_id):
        word = self._resolve_word(word_id)
        if not word:
            self._json({"error": "Palavra não encontrada"}, status=404)
            return
        try:
            fname = get_audio_for_word(word)
            if fname:
                self._json({"audio_file": fname, "word": word})
            else:
                self._json({"error": "Falha ao gerar áudio"}, status=500)
        except Exception as e:
            self._json({"error": str(e)}, status=500)

    def _dict_state(self, word_id):
        """Return the automaticity state and fragility flags for a word."""
        try:
            state_row = get_or_create_state('word', word_id)
            state = state_row.get("state", "UNKNOWN") if isinstance(state_row, dict) else "UNKNOWN"
            confidence = state_row.get("confidence", 0) if isinstance(state_row, dict) else 0

            conn = get_conn()
            frag_rows = conn.execute(
                "SELECT fragility_type, fragility_score FROM fragile_items "
                "WHERE item_type = 'word' AND item_id = ? AND resolved_at IS NULL",
                (word_id,),
            ).fetchall()
            conn.close()

            fragilities = [
                {"type": r["fragility_type"], "score": r["fragility_score"]}
                for r in frag_rows
            ]

            self._json({
                "word_id": word_id,
                "state": state,
                "confidence": round(confidence, 2),
                "fragilities": fragilities,
            })
        except Exception as e:
            self._json({"state": "UNKNOWN", "confidence": 0, "fragilities": [], "error": str(e)})

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

        # Query fragility info for this chunk
        fragility_types = []
        try:
            frag_conn = get_conn()
            frag_rows = frag_conn.execute(
                """SELECT fragility_type FROM fragile_items
                   WHERE item_type='chunk' AND item_id=? AND resolved_at IS NULL""",
                (chunk["id"],)
            ).fetchall()
            frag_conn.close()
            fragility_types = [r["fragility_type"] for r in frag_rows]
        except Exception:
            pass

        # Get current acquisition state for the chunk
        chunk_state = None
        try:
            cs = get_or_create_state('chunk', chunk["id"])
            chunk_state = cs.get("state", "UNKNOWN")
        except Exception:
            pass

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
            "fragility_types": fragility_types,
            "current_state": chunk_state,
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

        # Read updated state for transition feedback
        state_info = None
        try:
            chunk_state = get_or_create_state('chunk', chunk_id)
            state_info = {
                "state": chunk_state["state"],
                "confidence": round(chunk_state.get("confidence", 0), 2),
                "avg_latency_ms": chunk_state.get("avg_latency_ms"),
                "latency_trend": chunk_state.get("latency_trend", 0),
            }
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
            "state_info": state_info,
            "biometric_score": biometric,
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

            if stage >= 2:
                # ── Stages 2+: introduce i+1 chunks (just beyond learner's level) ──
                i_plus_one = []
                try:
                    # Get chunks at CONTEXT_KNOWN or EFFORTFUL — known but not yet automatic
                    stretch_items = get_items_in_state('CONTEXT_KNOWN', 'chunk', limit=15)
                    stretch_items.extend(get_items_in_state('EFFORTFUL_AUDIO', 'chunk', limit=10))

                    if stretch_items:
                        conn = get_conn()
                        stretch_chunks = []
                        for item in stretch_items:
                            row = conn.execute(
                                "SELECT target_chunk FROM chunk_queue WHERE id = ?",
                                (item['item_id'],),
                            ).fetchone()
                            if row:
                                tc = row['target_chunk']
                                # Avoid duplicates with known vocab
                                if tc not in chunks_used_list:
                                    stretch_chunks.append(tc)
                        conn.close()

                        if stretch_chunks:
                            n = min(2, len(stretch_chunks))
                            selected_new = random.sample(stretch_chunks, n)
                            i_plus_one = selected_new
                            system_prompt += (
                                f" Chunks novos pra introduzir naturalmente (i+1): "
                                f"{', '.join(selected_new)}. "
                                f"Usa esses chunks na conversa pra o aprendiz ouvir em contexto."
                            )
                except Exception as e:
                    print(f"[Conversa Start] i+1 chunk warning: {e}")

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
                 prompt_data, json.dumps([]), json.dumps(chunks_used_list + i_plus_one)),
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
                "chunks_i_plus_one": i_plus_one,
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

            # ── 6. Run error analysis on learner messages ──
            analysis = None
            if _conversa_history:
                try:
                    analysis = analyze_conversation(_conversa_history)
                    # Store analysis inside post_extraction
                    post_extraction["analysis"] = analysis
                    if session_id:
                        conn = get_conn()
                        conn.execute(
                            "UPDATE conversa_sessions SET post_extraction = ? WHERE id = ?",
                            (json.dumps(post_extraction, ensure_ascii=False), session_id),
                        )
                        conn.commit()
                        conn.close()
                except Exception as e:
                    print(f"[Conversa End] Analysis error: {e}")

            # ── Clean up session globals ──
            history_copy = list(_conversa_history)
            _conversa_history = []
            _conversa_chunks_vocab = []

            response = {
                "ok": True,
                "session_id": session_id,
                "turns": turn_count,
                "chunks_extracted": len(extracted_chunks),
                "chunks_introduced": len(chunks_introduced),
                "chunks_introduced_list": chunks_introduced,
                "vocab_chunks_used": vocab_chunks_used,
                "vocab_chunks_used_count": len(vocab_chunks_used),
            }
            if analysis:
                response["analysis"] = analysis
            self._json(response)
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
        # Log segments from the result body
        segments = body.get("segments", [])
        if segments:
            for seg in segments:
                conn.execute(
                    """INSERT OR REPLACE INTO content_segments
                       (content_type, content_id, segment_index, text, comprehension_pct, replays, latency_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ('story', story_id, seg.get("index", 0), seg.get("text", ""),
                     seg.get("comprehension_pct", 0), seg.get("replays", 0), seg.get("latency_ms")),
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
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=60")
        accept_enc = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc and len(body) > 1024:
            body = gzip.compress(body)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        accept_enc = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc and len(body) > 512:
            body = gzip.compress(body)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), OxeHandler)

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
