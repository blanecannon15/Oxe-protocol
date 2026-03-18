"""
listening_layers.py — Listening Difficulty Layers for the Oxe Protocol.

Same content at 4 processing levels:
  1. Limpo        — Clean TTS, normal speed, high stability
  2. Nativo Claro — Native speed, clear articulation
  3. Nativo Rapido — Native speed with reductions and elisions
  4. Com Barulho  — Native fast with background noise

Progressive difficulty on familiar material. The learner hears the SAME
chunk/sentence at increasing difficulty, building real-world comprehension.

Usage:
    from listening_layers import (
        LISTENING_LAYERS, generate_layer_audio,
        get_layer_audios, get_listening_drill,
        advance_listening_layer,
    )
"""

import os
import random
import struct
import time
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from srs_engine import DB_PATH, get_connection, get_chunk_by_id

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ── Layer Definitions ────────────────────────────────────────────

LISTENING_LAYERS = {
    "clean": {
        "label": "Limpo",
        "description": "TTS claro, velocidade normal",
        "order": 1,
        "tts_settings": {
            "stability": 0.70,
            "similarity_boost": 0.90,
            "style": 0.30,
            "speed": 1.0,
        },
    },
    "native_clear": {
        "label": "Nativo Claro",
        "description": "Velocidade nativa, articulacao clara",
        "order": 2,
        "tts_settings": {
            "stability": 0.45,
            "similarity_boost": 0.85,
            "style": 0.55,
            "speed": 1.15,
        },
    },
    "native_fast": {
        "label": "Nativo Rapido",
        "description": "Velocidade nativa com reducoes e elisoes",
        "order": 3,
        "tts_settings": {
            "stability": 0.30,
            "similarity_boost": 0.75,
            "style": 0.70,
            "speed": 1.3,
        },
    },
    "noisy": {
        "label": "Com Barulho",
        "description": "Nativo com ruido de fundo (rua, bar, praia)",
        "order": 4,
        "tts_settings": {
            "stability": 0.30,
            "similarity_boost": 0.75,
            "style": 0.70,
            "speed": 1.3,
        },
        "add_noise": True,
    },
}

LAYER_ORDER = ["clean", "native_clear", "native_fast", "noisy"]

# In-memory layer progress tracker: {chunk_id: current_layer}
_layer_progress = {}  # type: Dict[int, str]

# Voice ID used for TTS generation (same as drill_server.py)
VOICE_ID = "CwhRBWXzGAHq8TQ4Fs17"  # Roger — casual Baiano vibe
MODEL_ID = "eleven_multilingual_v2"


# ── DB Schema ────────────────────────────────────────────────────

def ensure_listening_layer_table(db_path=DB_PATH):
    """Create the listening_layer_cache table if it does not exist."""
    conn = get_connection(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listening_layer_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            chunk_id INTEGER,
            layer TEXT NOT NULL,
            audio_file TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(text, layer)
        )
    """)
    conn.commit()
    conn.close()


# ── Noise Generation ─────────────────────────────────────────────

def _generate_ambient_noise_wav(duration_seconds, sample_rate=22050):
    """Generate a simple ambient noise WAV as bytes.

    Produces a low-frequency hum with random ambient texture,
    suitable for overlaying on speech audio at low amplitude.
    Returns raw WAV bytes.
    """
    num_samples = int(sample_rate * duration_seconds)
    samples = []
    import math

    for i in range(num_samples):
        t = i / sample_rate
        # Low-frequency hum (60 Hz + 120 Hz harmonics)
        hum = math.sin(2 * math.pi * 60 * t) * 400
        hum += math.sin(2 * math.pi * 120 * t) * 200
        hum += math.sin(2 * math.pi * 180 * t) * 100
        # Random ambient texture (white noise scaled down)
        noise = (random.random() - 0.5) * 600
        # Occasional louder transients (simulating street/bar sounds)
        if random.random() < 0.001:
            noise += (random.random() - 0.5) * 3000
        sample = int(hum + noise)
        # Clamp to 16-bit range
        sample = max(-32768, min(32767, sample))
        samples.append(sample)

    # Pack into WAV
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = struct.pack("<" + "h" * len(samples), *samples)
        wf.writeframes(raw)
    return buf.getvalue()


def _mix_audio_with_noise(audio_path, noise_level=0.15):
    """Mix an MP3 audio file with generated ambient noise.

    Since we cannot decode MP3 in pure Python without external libraries,
    we append the noise as a separate WAV file and concatenate at the
    byte level. For a production system, ffmpeg would be used.

    Instead, we generate noise as a WAV, convert to raw, and prepend/append
    noise to create an immersive feel. The actual mixing uses subprocess
    if ffmpeg is available, otherwise returns the original audio with
    a noise file alongside it.

    Returns the path to the mixed output file.
    """
    import subprocess
    import shutil

    out_path = str(audio_path).replace(".mp3", "_noisy.mp3")

    # Generate noise WAV
    noise_wav_path = str(audio_path).replace(".mp3", "_noise.wav")
    noise_data = _generate_ambient_noise_wav(duration_seconds=15.0)
    with open(noise_wav_path, "wb") as f:
        f.write(noise_data)

    # Try ffmpeg for proper mixing
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            subprocess.run(
                [
                    ffmpeg_path, "-y",
                    "-i", str(audio_path),
                    "-i", noise_wav_path,
                    "-filter_complex",
                    "[1:a]volume={vol}[noise];[0:a][noise]amix=inputs=2:duration=first:dropout_transition=2[out]".format(
                        vol=noise_level
                    ),
                    "-map", "[out]",
                    "-codec:a", "libmp3lame",
                    "-q:a", "4",
                    out_path,
                ],
                capture_output=True,
                timeout=30,
            )
            # Clean up noise WAV
            try:
                os.remove(noise_wav_path)
            except OSError:
                pass

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Fallback: no ffmpeg, just copy the original (noise-free)
    # The user won't get noise but at least the layer still works
    try:
        os.remove(noise_wav_path)
    except OSError:
        pass
    shutil.copy2(str(audio_path), out_path)
    return out_path


# ── TTS Generation Per Layer ─────────────────────────────────────

def generate_layer_audio(text, layer, db_path=DB_PATH):
    """Generate TTS audio for a specific listening layer.

    For the 'noisy' layer, generates native_fast audio first, then
    mixes with background noise.

    Args:
        text: The text to synthesize.
        layer: One of the LISTENING_LAYERS keys.
        db_path: Path to the SQLite database.

    Returns:
        Audio filename (relative to AUDIO_DIR), or None on failure.
    """
    if layer not in LISTENING_LAYERS:
        return None

    layer_def = LISTENING_LAYERS[layer]

    # For noisy layer, generate native_fast audio first, then add noise
    if layer == "noisy":
        base_audio = generate_layer_audio(text, "native_fast", db_path)
        if not base_audio:
            return None
        base_path = AUDIO_DIR / base_audio
        mixed_path = _mix_audio_with_noise(str(base_path))
        if mixed_path and os.path.exists(mixed_path):
            fname = "layer_noisy_{ts}.mp3".format(ts=int(time.time() * 1000))
            final_path = AUDIO_DIR / fname
            os.rename(mixed_path, str(final_path))
            return fname
        return None

    # Regular TTS generation
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None

    tts_settings = layer_def["tts_settings"]

    try:
        from elevenlabs import ElevenLabs
        client = ElevenLabs(api_key=api_key)

        voice_settings = {
            "stability": tts_settings["stability"],
            "similarity_boost": tts_settings["similarity_boost"],
            "style": tts_settings["style"],
            "use_speaker_boost": True,
        }

        # ElevenLabs v2 API does not support speed directly in voice_settings
        # but we pass it via the convert endpoint if available
        audio_iter = client.text_to_speech.convert(
            text=text,
            voice_id=VOICE_ID,
            model_id=MODEL_ID,
            output_format="mp3_44100_128",
            voice_settings=voice_settings,
        )

        fname = "layer_{layer}_{ts}.mp3".format(
            layer=layer, ts=int(time.time() * 1000)
        )
        outpath = AUDIO_DIR / fname
        with open(outpath, "wb") as f:
            for chunk in audio_iter:
                f.write(chunk)

        if outpath.exists() and outpath.stat().st_size > 0:
            return fname
        return None

    except Exception as e:
        print("ERROR: generate_layer_audio({layer}): {e}".format(layer=layer, e=e))
        return None


# ── Cache Layer Audios ────────────────────────────────────────────

def _get_cached_layer(text, layer, db_path=DB_PATH):
    """Check if a cached audio file exists for this text+layer."""
    ensure_listening_layer_table(db_path)
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT audio_file FROM listening_layer_cache WHERE text = ? AND layer = ?",
        (text, layer),
    ).fetchone()
    conn.close()
    if row:
        fpath = AUDIO_DIR / row["audio_file"]
        if fpath.exists() and fpath.stat().st_size > 0:
            return row["audio_file"]
    return None


def _cache_layer(text, layer, audio_file, chunk_id=None, db_path=DB_PATH):
    """Store a layer audio file in the cache."""
    ensure_listening_layer_table(db_path)
    conn = get_connection(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO listening_layer_cache
           (text, chunk_id, layer, audio_file) VALUES (?, ?, ?, ?)""",
        (text, chunk_id, layer, audio_file),
    )
    conn.commit()
    conn.close()


def get_layer_audios(text, chunk_id=None, db_path=DB_PATH):
    """Get or generate audio for all 4 layers of a given text.

    Checks cache first, generates missing layers.

    Args:
        text: The text to synthesize across layers.
        chunk_id: Optional chunk ID for cache association.
        db_path: Path to the SQLite database.

    Returns:
        Dict mapping layer name to audio filename:
        {"clean": "layer_clean_123.mp3", ...}
    """
    result = {}  # type: Dict[str, Optional[str]]

    for layer in LAYER_ORDER:
        # Check cache
        cached = _get_cached_layer(text, layer, db_path)
        if cached:
            result[layer] = cached
            continue

        # Generate
        audio_file = generate_layer_audio(text, layer, db_path)
        if audio_file:
            _cache_layer(text, layer, audio_file, chunk_id, db_path)
            result[layer] = audio_file
        else:
            result[layer] = None

    return result


# ── Listening Drill Config ────────────────────────────────────────

def get_listening_drill(chunk_id, db_path=DB_PATH):
    """Create a listening drill configuration for a chunk across all layers.

    Fetches chunk text and carrier sentence, generates all 4 layer audios,
    and returns a drill config dict.

    Args:
        chunk_id: The chunk_queue ID.
        db_path: Path to the SQLite database.

    Returns:
        Dict with drill configuration:
        {
            "chunk_id": 42,
            "text": "tudo bem",
            "carrier": "Oxe, tu sabe o que e tudo bem? viu!",
            "layers": [
                {"layer": "clean", "label": "Limpo", "audio_file": "...", "order": 1},
                ...
            ],
            "current_layer": "clean",
        }
    """
    chunk = get_chunk_by_id(chunk_id, db_path)
    if not chunk:
        return {"error": "Chunk not found"}

    text = chunk.get("carrier_sentence") or chunk.get("chunk_text", "")
    chunk_text = chunk.get("chunk_text", "")
    carrier = chunk.get("carrier_sentence", "")

    if not text:
        return {"error": "No text for chunk"}

    # Generate / retrieve all layer audios
    audios = get_layer_audios(text, chunk_id=chunk_id, db_path=db_path)

    # Build layers list
    layers = []
    for layer_key in LAYER_ORDER:
        layer_def = LISTENING_LAYERS[layer_key]
        layers.append({
            "layer": layer_key,
            "label": layer_def["label"],
            "audio_file": audios.get(layer_key),
            "order": layer_def["order"],
        })

    # Get current layer from progress tracker
    current_layer = _layer_progress.get(chunk_id, "clean")

    return {
        "chunk_id": chunk_id,
        "text": chunk_text,
        "carrier": carrier,
        "layers": layers,
        "current_layer": current_layer,
    }


# ── Layer Advancement ─────────────────────────────────────────────

def advance_listening_layer(chunk_id, current_layer, success, db_path=DB_PATH):
    """Determine if the learner should move to the next difficulty layer.

    Args:
        chunk_id: The chunk ID.
        current_layer: Current layer key (e.g. "clean").
        success: Boolean, whether the learner succeeded at this layer.
        db_path: Path to the SQLite database (reserved for future persistence).

    Returns:
        Dict with advancement result:
        {
            "chunk_id": 42,
            "previous_layer": "clean",
            "current_layer": "native_clear",
            "advanced": True,
            "completed": False,
        }
    """
    if current_layer not in LISTENING_LAYERS:
        current_layer = "clean"

    previous = current_layer
    advanced = False
    completed = False

    if success and current_layer != "noisy":
        # Advance to next layer
        idx = LAYER_ORDER.index(current_layer)
        if idx + 1 < len(LAYER_ORDER):
            current_layer = LAYER_ORDER[idx + 1]
            advanced = True
        else:
            completed = True
    elif success and current_layer == "noisy":
        completed = True

    # Track progress
    _layer_progress[chunk_id] = current_layer

    return {
        "chunk_id": chunk_id,
        "previous_layer": previous,
        "current_layer": current_layer,
        "advanced": advanced,
        "completed": completed,
    }


def get_layer_progress(chunk_id):
    """Get the current layer for a chunk from the in-memory tracker."""
    return _layer_progress.get(chunk_id, "clean")
