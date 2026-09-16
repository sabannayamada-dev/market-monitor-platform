set -eu

root=/tmp/gdelt-pdca-20260816
app=/opt/patent-news-monitor/app
pid_file="$root/full_validation.pid"
output="$root/full_validation.out"
timing="$root/full_validation.time"

if test -f "$pid_file"; then
    old_pid=$(cat "$pid_file")
    if test -n "$old_pid" && kill -0 "$old_pid" 2>/dev/null; then
        echo "validation already running: $old_pid" >&2
        exit 2
    fi
fi

cd "$app"
/opt/patent-news-monitor/venv/bin/python -B "$root/enable_full_validation.py"
: > "$output"
: > "$timing"
nohup env \
    PYTHONPATH="$app" \
    PYTHONDONTWRITEBYTECODE=1 \
    GDELT_CONFIG_PATH="$root/validation_config.json" \
    GDELT_STATE_DB="$root/emergency_validation.sqlite3" \
    GDELT_TOC_CACHE_DIR="$root/emergency_toc_cache" \
    GDELT_OPERATION_STAGE=2 \
    GDELT_EMAIL_ENABLED=0 \
    GDELT_AI_ENABLED=0 \
    /usr/bin/time -o "$timing" -f 'ELAPSED=%e MAX_RSS_KB=%M' \
    /opt/patent-news-monitor/venv/bin/python -B gdelt_news_daily.py \
    > "$output" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$pid_file"
echo "pid=$pid"
