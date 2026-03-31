#!/bin/bash
# Sync Railway production DB to local
# Usage: ./sync_railway.sh [pull|push|status]

RAILWAY_URL="${RAILWAY_URL:-https://oxe-protocol-production.up.railway.app}"
LOCAL_DB="voca_20k.db"
BACKUP_DIR=".db_backups"

mkdir -p "$BACKUP_DIR"

case "${1:-pull}" in
  pull)
    echo "Pulling Railway DB to local..."
    # Backup current local DB
    if [ -f "$LOCAL_DB" ]; then
      cp "$LOCAL_DB" "$BACKUP_DIR/local_$(date +%Y%m%d_%H%M%S).db"
      echo "  Backed up local DB to $BACKUP_DIR/"
    fi
    # Download Railway DB
    curl -fSL --progress-bar -o "$LOCAL_DB" "$RAILWAY_URL/api/sync/download-db"
    if [ $? -eq 0 ]; then
      SIZE=$(ls -lh "$LOCAL_DB" | awk '{print $5}')
      REVIEWS=$(sqlite3 "$LOCAL_DB" "SELECT COUNT(*) FROM review_history" 2>/dev/null || echo "?")
      QUEUE=$(sqlite3 "$LOCAL_DB" "SELECT COUNT(*) FROM chunk_queue" 2>/dev/null || echo "?")
      echo "  Downloaded: $SIZE"
      echo "  Reviews: $REVIEWS"
      echo "  Queue: $QUEUE"
      echo "  Done!"
    else
      echo "  ERROR: Failed to download. Is Railway running?"
      # Restore backup
      LATEST=$(ls -t "$BACKUP_DIR"/local_*.db 2>/dev/null | head -1)
      if [ -n "$LATEST" ]; then
        cp "$LATEST" "$LOCAL_DB"
        echo "  Restored from backup."
      fi
    fi
    ;;

  status)
    echo "Railway DB status:"
    curl -s "$RAILWAY_URL/api/health" | python3 -m json.tool 2>/dev/null || echo "  Cannot reach Railway"
    echo ""
    echo "Local DB status:"
    if [ -f "$LOCAL_DB" ]; then
      SIZE=$(ls -lh "$LOCAL_DB" | awk '{print $5}')
      REVIEWS=$(sqlite3 "$LOCAL_DB" "SELECT COUNT(*) FROM review_history" 2>/dev/null || echo "?")
      QUEUE=$(sqlite3 "$LOCAL_DB" "SELECT COUNT(*) FROM chunk_queue" 2>/dev/null || echo "?")
      echo "  Size: $SIZE"
      echo "  Reviews: $REVIEWS"
      echo "  Queue: $QUEUE"
    else
      echo "  No local DB found"
    fi
    ;;

  *)
    echo "Usage: ./sync_railway.sh [pull|status]"
    echo "  pull   - Download Railway DB to local (backs up current)"
    echo "  status - Show Railway and local DB stats"
    ;;
esac
