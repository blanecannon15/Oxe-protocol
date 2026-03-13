"""
serve_audio.py — Local HTTP server for mobile audio delivery.

Serves voca_vault/audios/ over your local network so the phone can
play ElevenLabs clips during Remote Control 1+T drills.

Usage:
    python3 serve_audio.py              # default port 7777
    python3 serve_audio.py --port 9000  # custom port

Then on phone (same Wi-Fi):
    http://<your-mac-ip>:7777/latest    → auto-plays most recent TTS clip
    http://<your-mac-ip>:7777/files/    → browse all audio files
"""

import http.server
import json
import os
import socket
import sys
import threading
from functools import partial
from pathlib import Path
from urllib.parse import unquote

AUDIO_DIR = Path(__file__).parent / "voca_vault" / "audios"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def get_local_ip():
    """Get the Mac's local network IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_latest_audio():
    """Return the most recently modified .mp3 in AUDIO_DIR."""
    mp3s = sorted(AUDIO_DIR.glob("tts_*.mp3"), key=lambda p: p.stat().st_mtime)
    if mp3s:
        return mp3s[-1]
    # Fallback to any mp3
    mp3s = sorted(AUDIO_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    return mp3s[-1] if mp3s else None


class AudioHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, audio_dir, **kwargs):
        self.audio_dir = audio_dir
        super().__init__(*args, directory=str(audio_dir), **kwargs)

    def do_GET(self):
        path = unquote(self.path)

        # /latest — auto-play page for the most recent TTS clip
        if path == "/latest" or path == "/":
            latest = get_latest_audio()
            if not latest:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>No audio yet.</h2>"
                                 b"<script>setTimeout(()=>location.reload(),2000)</script>"
                                 b"</body></html>")
                return

            fname = latest.name
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = f"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oxe Protocol — Audio</title>
<style>
  body {{ background:#1a1a2e; color:#e0e0e0; font-family:system-ui;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; min-height:100vh; margin:0; }}
  h1 {{ color:#f7931e; font-size:1.4em; }}
  .status {{ font-size:1.1em; margin:1em 0; }}
  audio {{ width:90%; max-width:400px; }}
  .file {{ color:#888; font-size:0.8em; word-break:break-all; }}
</style>
</head><body>
<h1>Oxe Protocol</h1>
<p class="status" id="status">Reproduzindo...</p>
<audio id="player" autoplay controls>
  <source src="/files/{fname}" type="audio/mpeg">
</audio>
<p class="file">{fname}</p>
<script>
  const player = document.getElementById('player');
  const status = document.getElementById('status');
  let lastFile = '{fname}';

  player.onended = () => {{ status.textContent = 'Esperando próximo áudio...'; poll(); }};
  player.onerror = () => {{ status.textContent = 'Erro. Tentando de novo...'; setTimeout(poll, 2000); }};

  function poll() {{
    fetch('/api/latest')
      .then(r => r.json())
      .then(data => {{
        if (data.file && data.file !== lastFile) {{
          lastFile = data.file;
          player.src = '/files/' + data.file;
          player.play();
          status.textContent = 'Reproduzindo...';
          document.querySelector('.file').textContent = data.file;
        }} else {{
          setTimeout(poll, 1500);
        }}
      }})
      .catch(() => setTimeout(poll, 2000));
  }}

  // Start polling even while playing, to catch new files
  setInterval(() => {{
    fetch('/api/latest')
      .then(r => r.json())
      .then(data => {{
        if (data.file && data.file !== lastFile) {{
          lastFile = data.file;
          player.src = '/files/' + data.file;
          player.play();
          status.textContent = 'Reproduzindo...';
          document.querySelector('.file').textContent = data.file;
        }}
      }}).catch(() => {{}});
  }}, 2000);
</script>
</body></html>"""
            self.wfile.write(html.encode())
            return

        # /api/latest — JSON endpoint for polling
        if path == "/api/latest":
            latest = get_latest_audio()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            data = {"file": latest.name if latest else None}
            self.wfile.write(json.dumps(data).encode())
            return

        # /files/* — serve actual audio files
        if path.startswith("/files/"):
            self.path = path[len("/files"):]
            return super().do_GET()

        # Anything else → redirect to /latest
        self.send_response(302)
        self.send_header("Location", "/latest")
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress noisy HTTP logs unless error."""
        if args and str(args[0]).startswith("4"):
            super().log_message(format, *args)


def main():
    port = 7777
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    ip = get_local_ip()
    handler = partial(AudioHandler, audio_dir=AUDIO_DIR)
    server = http.server.HTTPServer(("0.0.0.0", port), handler)

    print(f"\n  Oxe Protocol — Audio Server")
    print(f"  {'='*40}")
    print(f"  Local:   http://localhost:{port}")
    print(f"  Phone:   http://{ip}:{port}")
    print(f"  Audio:   {AUDIO_DIR}")
    print(f"  {'='*40}")
    print(f"  Open the Phone URL on your mobile (same Wi-Fi).")
    print(f"  Audio auto-plays and polls for new clips every 2s.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
