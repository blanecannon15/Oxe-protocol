#!/usr/bin/env python3
"""Push local dictionary_cache to Railway production in compressed batches."""

import sqlite3
import json
import gzip
import sys
import time
import urllib.request
import urllib.error

RAILWAY_URL = "https://oxe-protocol-production.up.railway.app"
DB_PATH = "voca_20k.db"
BATCH_SIZE = 500  # rows per request

def main():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    total = db.execute("SELECT COUNT(*) FROM dictionary_cache").fetchone()[0]
    print(f"Total local cache rows: {total}")

    # Check what's already on Railway
    try:
        req = urllib.request.Request(f"{RAILWAY_URL}/api/search?q=teste")
        resp = urllib.request.urlopen(req, timeout=10)
        print("Railway is reachable")
    except Exception as e:
        print(f"Railway unreachable: {e}")
        return

    offset = 0
    total_pushed = 0
    errors = 0

    while offset < total:
        rows = db.execute(
            "SELECT word_id, tab_name, data_json FROM dictionary_cache LIMIT ? OFFSET ?",
            (BATCH_SIZE, offset)
        ).fetchall()

        if not rows:
            break

        payload = {
            "rows": [
                {"word_id": r["word_id"], "tab_name": r["tab_name"], "data_json": r["data_json"]}
                for r in rows
            ]
        }

        data = json.dumps(payload).encode("utf-8")
        compressed = gzip.compress(data)

        try:
            req = urllib.request.Request(
                f"{RAILWAY_URL}/api/cache/bulk",
                data=compressed,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=60)
            result = json.loads(resp.read().decode())
            inserted = result.get("inserted", 0)
            total_pushed += inserted
        except Exception as e:
            errors += 1
            print(f"  Error at offset {offset}: {e}")
            if errors > 5:
                print("Too many errors, stopping")
                break
            time.sleep(2)
            continue

        offset += BATCH_SIZE
        pct = min(100, round(offset / total * 100))
        sys.stdout.write(f"\r  [{pct:3d}%] Pushed {total_pushed} rows ({offset}/{total})  ")
        sys.stdout.flush()

        # Small delay to not overload Railway
        time.sleep(0.2)

    print(f"\nDone! Pushed {total_pushed} rows to Railway ({errors} errors)")
    db.close()


if __name__ == "__main__":
    main()
