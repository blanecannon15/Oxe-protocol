"""
voice_profiles.py — Multi-accent voice management for pan-Brazilian TTS.

Default weighting: 60% Paulista, 25% Carioca, 15% Baiano.
Manages voice selection, TTS generation per accent, and profile configuration.
"""

import os
import time
from pathlib import Path

from srs_engine import DB_PATH, get_connection

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"


def get_profiles(db_path=DB_PATH):
    """Return all voice profiles ordered by weight DESC."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM voice_profiles ORDER BY weight DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_default_profile(db_path=DB_PATH):
    """Return the default (highest weight) voice profile."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM voice_profiles WHERE is_default = 1 LIMIT 1"
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM voice_profiles ORDER BY weight DESC LIMIT 1"
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_profile(accent, db_path=DB_PATH):
    """Return a specific accent profile."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM voice_profiles WHERE accent = ?", (accent,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_profile(accent, **kwargs):
    """Update voice profile fields. Only updates provided kwargs."""
    db_path = kwargs.pop("db_path", DB_PATH)
    allowed = {"voice_id", "label", "model_id", "stability", "similarity",
               "style", "speaker_boost", "weight", "tts_prefix", "is_default"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [accent]
    conn = get_connection(db_path)
    conn.execute(f"UPDATE voice_profiles SET {set_clause} WHERE accent = ?", values)
    conn.commit()
    conn.close()


def generate_tts_for_accent(text, accent=None, db_path=DB_PATH):
    """Generate TTS using a specific accent profile. Returns filename or None."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None

    profile = get_profile(accent, db_path) if accent else get_default_profile(db_path)
    if not profile:
        return None

    try:
        from elevenlabs import ElevenLabs
        client = ElevenLabs(api_key=api_key)

        # Apply accent-specific prefix (e.g., "Oxe, " for Baiano)
        tts_text = text
        if profile["tts_prefix"] and len(text.split()) <= 3:
            tts_text = profile["tts_prefix"] + text

        audio_iter = client.text_to_speech.convert(
            text=tts_text,
            voice_id=profile["voice_id"],
            model_id=profile["model_id"],
            output_format="mp3_44100_128",
            voice_settings={
                "stability": profile["stability"],
                "similarity_boost": profile["similarity"],
                "style": profile["style"],
                "use_speaker_boost": bool(profile["speaker_boost"]),
            },
        )

        accent_tag = profile["accent"]
        fname = f"tts_{accent_tag}_{int(time.time() * 1000)}.mp3"
        outpath = AUDIO_DIR / fname
        with open(outpath, "wb") as f:
            for chunk in audio_iter:
                f.write(chunk)
        return fname
    except Exception as e:
        print(f"[TTS:{accent}] Error: {e}")
        return None


def get_accent_weights(db_path=DB_PATH):
    """Return accent weights as dict: {accent: weight}."""
    profiles = get_profiles(db_path)
    return {p["accent"]: p["weight"] for p in profiles}
