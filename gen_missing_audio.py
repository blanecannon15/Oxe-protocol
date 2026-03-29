"""Generate audio for all stories missing audio_chunks."""
import sys
import time
from story_gen import generate_story_audio
from srs_engine import get_connection

conn = get_connection()
rows = conn.execute(
    "SELECT id, title, level FROM story_library WHERE audio_chunks IS NULL OR audio_chunks = '' OR audio_chunks = '[]' ORDER BY id"
).fetchall()
conn.close()

if not rows:
    print("All stories have audio!")
    sys.exit(0)

print(f"Generating audio for {len(rows)} stories...")
print("=" * 60)

for i, r in enumerate(rows, 1):
    sid, title, level = r["id"], r["title"], r["level"]
    print(f"\n[{i}/{len(rows)}] Story {sid}: [{level}] {title}")
    try:
        result = generate_story_audio(sid)
        if result is None:
            print(f"  FAILED — no result")
    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(1)

print("\n" + "=" * 60)
print("Done!")
