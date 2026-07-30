#!/usr/bin/env bash
# scripts/setup_crontab.sh
# Bu script mevcut crontab'a update_prices.py görevlerini ekler.
# Sunucuya taşındıktan sonra tekrar çalıştırılmalıdır.
#
# Kullanım: bash scripts/setup_crontab.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$(command -v python3)"

LOG_DIR="/var/log/florence"
mkdir -p "$LOG_DIR"

CRON_ENTRIES=(
    "*/10 * * * * cd $PROJECT_DIR && $PYTHON scripts/update_prices.py --tier bist30 >> $LOG_DIR/update_bist30.log 2>&1"
    "0 * * * * cd $PROJECT_DIR && $PYTHON scripts/update_prices.py --tier popular >> $LOG_DIR/update_popular.log 2>&1"
    "0 */12 * * * cd $PROJECT_DIR && $PYTHON scripts/update_prices.py --tier rest >> $LOG_DIR/update_rest.log 2>&1"
    "0 0 * * * cd $PROJECT_DIR && $PYTHON scripts/credit_refiller.py > $LOG_DIR/credit_refiller.log 2>&1"
    "0 2 * * * cd $PROJECT_DIR && $PYTHON scripts/seed_vectors.py --count 200 >> $LOG_DIR/seed_vectors.log 2>&1"
    "0 6 * * * cd $PROJECT_DIR && $PYTHON scripts/warm_price_cache.py >> $LOG_DIR/warm_price_cache.log 2>&1"
)

TEMP_CRON=$(mktemp)
crontab -l > "$TEMP_CRON" 2>/dev/null || true

added=0
for entry in "${CRON_ENTRIES[@]}"; do
    if grep -Fq "$entry" "$TEMP_CRON"; then
        echo "Zaten var: $entry"
    else
        echo "$entry" >> "$TEMP_CRON"
        echo "Eklendi: $entry"
        added=$((added + 1))
    fi
done

if [ $added -gt 0 ]; then
    crontab "$TEMP_CRON"
    echo "$added crontab gorevi eklendi."
else
    echo "Guncelleme gerekmiyor."
fi

rm "$TEMP_CRON"
