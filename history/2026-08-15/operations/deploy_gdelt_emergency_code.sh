set -eu

app=/opt/patent-news-monitor/app
source_dir=/tmp/gdelt-pdca-20260816/deploy_2333/gdelt_monitor
backup=/opt/patent-news-monitor/backups/20260816_2333_gdelt_emergency_code
pycache=/tmp/gdelt-pdca-20260816/pycache_2333

test -d "$app/gdelt_monitor"
test -f "$source_dir/emergency.py"
if pgrep -f '[p]ython.*gdelt_news_daily' >/dev/null; then
    echo "gdelt service is currently running; deployment aborted" >&2
    exit 2
fi

mkdir -p "$backup/gdelt_monitor" "$pycache"
for name in adaptive.py config.py database.py ngram_collector.py notifications.py service.py; do
    cp -a "$app/gdelt_monitor/$name" "$backup/gdelt_monitor/$name"
done

# Install dependency modules first and the service entry point last.
for name in emergency.py database.py config.py adaptive.py ngram_collector.py notifications.py service.py; do
    install -m 0644 "$source_dir/$name" "$app/gdelt_monitor/$name"
done

cd "$app"
PYTHONPYCACHEPREFIX="$pycache" /opt/patent-news-monitor/venv/bin/python -m py_compile \
    gdelt_monitor/emergency.py gdelt_monitor/database.py gdelt_monitor/config.py \
    gdelt_monitor/adaptive.py gdelt_monitor/ngram_collector.py \
    gdelt_monitor/notifications.py gdelt_monitor/service.py

echo "backup=$backup"
