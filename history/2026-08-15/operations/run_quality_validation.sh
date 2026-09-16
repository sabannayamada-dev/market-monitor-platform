set -eu

root=/tmp/gdelt-pdca-20260816
app=/opt/patent-news-monitor/app
cd "$app"
/opt/patent-news-monitor/venv/bin/python -B "$root/prepare_quality_validation.py"
: > "$root/quality_validation.out"
: > "$root/quality_validation.time"
nohup env \
    PYTHONPATH="$app" \
    PYTHONDONTWRITEBYTECODE=1 \
    GDELT_CONFIG_PATH="$root/quality_validation_config.json" \
    GDELT_STATE_DB="$root/quality_validation.sqlite3" \
    GDELT_TOC_CACHE_DIR="$root/quality_toc_cache" \
    GDELT_OPERATION_STAGE=2 \
    GDELT_EMAIL_ENABLED=0 \
    GDELT_AI_ENABLED=0 \
    /usr/bin/time -o "$root/quality_validation.time" -f 'ELAPSED=%e MAX_RSS_KB=%M' \
    /opt/patent-news-monitor/venv/bin/python -B gdelt_news_daily.py \
    > "$root/quality_validation.out" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$root/quality_validation.pid"
echo "pid=$pid"
