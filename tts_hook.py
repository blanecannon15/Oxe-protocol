"""
tts_hook.py — Zero-latency TTS stop hook for the Oxe Protocol.

Called by Claude Code's Stop hook. Reads the assistant response JSON from
stdin, extracts last_assistant_message, streams it through ElevenLabs TTS,
and fires afplay in a detached subprocess so Claude never blocks.

Stdin JSON (from Claude Code):
    {"last_assistant_message": "...", "stop_hook_active": false, ...}

Guard: if stop_hook_active is true, exit immediately to prevent loops.
"""

import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ElevenLabs config — Soteropolitano voice
VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # Replace with your Baiano voice ID
MODEL_ID = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"

VOICE_SETTINGS = {
    "stability": 0.55,
    "similarity_boost": 0.90,
    "style": 0.45,
    "use_speaker_boost": True,
}


def main():
    # ── 1. Read hook input from stdin ──────────────────────────────
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    # ── 2. Loop guard ─────────────────────────────────────────────
    if payload.get("stop_hook_active", False):
        return

    text = (payload.get("last_assistant_message") or "").strip()
    if not text:
        return

    # ── 3. API key ────────────────────────────────────────────────
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return

    # ── 4. Stream TTS → temp file → afplay (detached) ────────────
    try:
        from elevenlabs import ElevenLabs

        client = ElevenLabs(api_key=api_key)

        audio_iter = client.text_to_speech.convert(
            text=text,
            voice_id=VOICE_ID,
            model_id=MODEL_ID,
            output_format=OUTPUT_FORMAT,
            voice_settings=VOICE_SETTINGS,
        )

        # Write to a temp file that persists after this process exits.
        # Using delete=False so afplay can read it after we're gone.
        tmp = tempfile.NamedTemporaryFile(
            suffix=".mp3", dir=str(AUDIO_DIR), delete=False, prefix="tts_"
        )
        for chunk in audio_iter:
            tmp.write(chunk)
        tmp.close()

        # If running locally (not remote), play via afplay.
        # If remote/mobile, skip afplay — the phone polls serve_audio.py
        # for new files via HTTP. The temp file stays for the server to find.
        is_remote = os.environ.get("CLAUDE_CODE_REMOTE") == "true"

        if is_remote:
            # Keep the file (no cleanup) — serve_audio.py delivers it to phone.
            # Rename with timestamp so the polling endpoint detects it as new.
            import time as _t
            final = AUDIO_DIR / f"tts_{int(_t.time() * 1000)}.mp3"
            os.rename(tmp.name, str(final))
        else:
            # Fire afplay completely detached — double-fork via shell so
            # neither Claude Code nor this script wait on it.
            subprocess.Popen(
                f'afplay "{tmp.name}" && rm -f "{tmp.name}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    except Exception:
        pass  # Never block Claude Code


if __name__ == "__main__":
    main()
