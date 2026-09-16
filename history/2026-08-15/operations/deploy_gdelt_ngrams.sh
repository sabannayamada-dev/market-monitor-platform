set -eu

app=/opt/patent-news-monitor/app
source_dir=/tmp/gdelt-pdca-20260816/deploy
backup=/opt/patent-news-monitor/backups/20260816_2308_gdelt_web_ngrams
test -d "$app/gdelt_monitor"
test -f "$source_dir/gdelt_monitor/ngram_collector.py"
if pgrep -f '[p]ython.*gdelt_news_daily' >/dev/null; then
    echo "gdelt service is currently running; deployment aborted" >&2
    exit 2
fi
mkdir -p "$backup/gdelt_monitor" "$backup/data" "$app/data"

for name in analysis.py config.py database.py service.py; do
    cp -a "$app/gdelt_monitor/$name" "$backup/gdelt_monitor/$name"
done
cp -a "$app/gdelt_monitor_config.json" "$backup/gdelt_monitor_config.json"
if test -f "$app/data/current_market_cap_snapshot.csv"; then
    cp -a "$app/data/current_market_cap_snapshot.csv" "$backup/data/current_market_cap_snapshot.csv"
fi

install -m 0644 "$source_dir/gdelt_monitor/ngram_collector.py" "$app/gdelt_monitor/ngram_collector.py"
for name in analysis.py config.py database.py service.py; do
    install -m 0644 "$source_dir/gdelt_monitor/$name" "$app/gdelt_monitor/$name"
done
install -m 0644 "$source_dir/gdelt_monitor_config.json" "$app/gdelt_monitor_config.json"
install -m 0644 "$source_dir/data/current_market_cap_snapshot.csv" "$app/data/current_market_cap_snapshot.csv"

cd "$app"
/opt/patent-news-monitor/venv/bin/python -m py_compile \
    gdelt_monitor/ngram_collector.py gdelt_monitor/analysis.py gdelt_monitor/config.py \
    gdelt_monitor/database.py gdelt_monitor/service.py

echo "backup=$backup"
