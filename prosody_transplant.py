"""
prosody_transplant.py — Golden Speaker generator for the Oxe Protocol.

Takes a recording of your voice and a native Baiano audio clip.
Uses ElevenLabs Speech-to-Speech API to transplant native rhythm
and pitch onto your voice clone — creating a "golden speaker" version
of YOU speaking with perfect Soteropolitano prosody.

Usage:
    python3 prosody_transplant.py clone <your_voice_sample>
        Register your voice as a clone with ElevenLabs.

    python3 prosody_transplant.py transplant <native_audio> [--word-id N]
        Take native Baiano audio, re-synthesize it with your cloned voice.
        Saves to voca_vault/audios/golden_speaker.mp3 (or golden_{word_id}.mp3).

    python3 prosody_transplant.py golden <native_audio> <your_voice_sample>
        One-shot: clone voice + transplant in a single step.

    python3 prosody_transplant.py list-voices
        List available ElevenLabs voices (to find your clone ID).
"""

import os
import sys
import time
from pathlib import Path

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
CLONE_VOICE_NAME = "oxe-protocol-minha-voz"


def get_client():
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set. Export it in ~/.profile")
        sys.exit(1)
    from elevenlabs import ElevenLabs
    return ElevenLabs(api_key=api_key)


def clone_voice(voice_sample_path):
    """
    Register your voice as an Instant Voice Clone with ElevenLabs.
    Returns the voice_id of the clone.
    """
    client = get_client()
    sample_path = Path(voice_sample_path)
    if not sample_path.exists():
        print(f"ERROR: Voice sample not found: {sample_path}")
        sys.exit(1)

    print(f"Cloning voice from: {sample_path}")
    print(f"Clone name: {CLONE_VOICE_NAME}")

    with open(sample_path, "rb") as f:
        voice = client.clone(
            name=CLONE_VOICE_NAME,
            description="Oxe Protocol — my voice clone for Golden Speaker drills",
            files=[f],
        )

    print(f"Voice cloned successfully!")
    print(f"  Voice ID: {voice.voice_id}")
    print(f"  Name:     {voice.name}")

    # Save voice ID for future use
    id_file = AUDIO_DIR / "clone_voice_id.txt"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    id_file.write_text(voice.voice_id)
    print(f"  Saved ID to: {id_file}")

    return voice.voice_id


def get_clone_voice_id():
    """Retrieve the saved clone voice ID."""
    id_file = AUDIO_DIR / "clone_voice_id.txt"
    if id_file.exists():
        return id_file.read_text().strip()
    return None


def find_clone_voice_id(client):
    """Search ElevenLabs voices for our clone by name."""
    voices = client.voices.get_all()
    for voice in voices.voices:
        if voice.name == CLONE_VOICE_NAME:
            return voice.voice_id
    return None


def transplant_prosody(native_audio_path, word_id=None, voice_id=None):
    """
    Speech-to-Speech: take native Baiano audio and re-synthesize it
    using your cloned voice. The result has native prosody + your timbre.

    This is the "Golden Speaker" — you, but with perfect Baiano rhythm.
    """
    client = get_client()
    native_path = Path(native_audio_path)
    if not native_path.exists():
        print(f"ERROR: Native audio not found: {native_path}")
        sys.exit(1)

    # Resolve voice ID
    if voice_id is None:
        voice_id = get_clone_voice_id()
    if voice_id is None:
        voice_id = find_clone_voice_id(client)
    if voice_id is None:
        print("ERROR: No voice clone found. Run 'clone' first:")
        print("  python3 prosody_transplant.py clone <your_voice_sample.mp3>")
        sys.exit(1)

    print(f"Transplanting prosody...")
    print(f"  Native audio: {native_path}")
    print(f"  Voice clone:  {voice_id}")

    with open(native_path, "rb") as audio_file:
        result = client.speech_to_speech.convert(
            voice_id=voice_id,
            audio=audio_file,
            model_id="eleven_multilingual_sts_v2",
            output_format="mp3_44100_128",
        )

    # Determine output path
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    if word_id is not None:
        out_path = AUDIO_DIR / f"golden_{word_id}.mp3"
    else:
        out_path = AUDIO_DIR / "golden_speaker.mp3"

    with open(out_path, "wb") as f:
        for chunk in result:
            f.write(chunk)

    print(f"  Golden Speaker saved: {out_path}")
    return out_path


def golden_one_shot(native_audio_path, voice_sample_path, word_id=None):
    """Clone voice + transplant prosody in one step."""
    voice_id = clone_voice(voice_sample_path)
    return transplant_prosody(native_audio_path, word_id=word_id, voice_id=voice_id)


def list_voices():
    """List all ElevenLabs voices, highlighting the clone."""
    client = get_client()
    voices = client.voices.get_all()
    print(f"\nElevenLabs Voices ({len(voices.voices)} total):\n")
    for v in voices.voices:
        marker = " ★ YOUR CLONE" if v.name == CLONE_VOICE_NAME else ""
        print(f"  {v.voice_id}  {v.name}{marker}")
    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "clone" and len(sys.argv) >= 3:
        clone_voice(sys.argv[2])

    elif cmd == "transplant" and len(sys.argv) >= 3:
        native_audio = sys.argv[2]
        word_id = None
        if "--word-id" in sys.argv:
            idx = sys.argv.index("--word-id")
            if idx + 1 < len(sys.argv):
                word_id = int(sys.argv[idx + 1])
        transplant_prosody(native_audio, word_id=word_id)

    elif cmd == "golden" and len(sys.argv) >= 4:
        native_audio = sys.argv[2]
        voice_sample = sys.argv[3]
        word_id = None
        if "--word-id" in sys.argv:
            idx = sys.argv.index("--word-id")
            if idx + 1 < len(sys.argv):
                word_id = int(sys.argv[idx + 1])
        golden_one_shot(native_audio, voice_sample, word_id=word_id)

    elif cmd == "list-voices":
        list_voices()

    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
