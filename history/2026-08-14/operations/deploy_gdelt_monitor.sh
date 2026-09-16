#!/usr/bin/env bash
set -euo pipefail

stage=/tmp/gdelt-monitor-stage-20260815
app_dir=/opt/patent-news-monitor/app
stamp="$(date +%Y%m%d_%H%M%S)"
backup_dir="/opt/patent-news-monitor/backups/gdelt-monitor-$stamp"

install -d -o root -g root -m 0750 "$backup_dir/app/gdelt_monitor" "$backup_dir/systemd"
install -d -o app -g app -m 0750 /var/lib/gdelt-news-monitor /var/log/gdelt-news-monitor
install -d -o app -g app -m 0755 "$app_dir/gdelt_monitor"

backup_if_present() {
    local source="$1"
    local destination="$2"
    if [[ -e "$source" ]]; then cp -a "$source" "$destination"; fi
}

for file in gdelt_news_daily.py gdelt_news_preflight.py gdelt_monitor_config.json GDELT_MONITOR_README.md monitor_services.json; do
    backup_if_present "$app_dir/$file" "$backup_dir/app/"
done
for file in __init__.py config.py database.py collector.py analysis.py ai.py notifications.py service.py; do
    backup_if_present "$app_dir/gdelt_monitor/$file" "$backup_dir/app/gdelt_monitor/"
done
backup_if_present /etc/systemd/system/gdelt-news-monitor.service "$backup_dir/systemd/"
backup_if_present /etc/systemd/system/gdelt-news-monitor.timer "$backup_dir/systemd/"
cp -a /etc/patent-news-monitor.env "$backup_dir/patent-news-monitor.env"
chmod 0600 "$backup_dir/patent-news-monitor.env"

for file in gdelt_news_daily.py gdelt_news_preflight.py gdelt_monitor_config.json GDELT_MONITOR_README.md monitor_services.json; do
    install -o app -g app -m 0644 "$stage/$file" "$app_dir/$file"
done
for file in __init__.py config.py database.py collector.py analysis.py ai.py notifications.py service.py; do
    install -o app -g app -m 0644 "$stage/gdelt_monitor/$file" "$app_dir/gdelt_monitor/$file"
done
install -o root -g root -m 0644 "$stage/gdelt-news-monitor.service" /etc/systemd/system/gdelt-news-monitor.service
install -o root -g root -m 0644 "$stage/gdelt-news-monitor.timer" /etc/systemd/system/gdelt-news-monitor.timer

env_tmp="$(mktemp)"
trap 'rm -f "$env_tmp"' EXIT
grep -Ev '^(GDELT_CONFIG_PATH|GDELT_COMPANY_MASTER|GDELT_STATE_DB|GDELT_EMAIL_ENABLED|GDELT_AI_ENABLED)=' /etc/patent-news-monitor.env > "$env_tmp" || true
cat >> "$env_tmp" <<'EOF'
GDELT_CONFIG_PATH=/opt/patent-news-monitor/app/gdelt_monitor_config.json
GDELT_COMPANY_MASTER=/opt/patent-news-monitor/app/patent_company_master.csv
GDELT_STATE_DB=/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3
GDELT_EMAIL_ENABLED=false
GDELT_AI_ENABLED=false
EOF
install -o root -g root -m 0600 "$env_tmp" /etc/patent-news-monitor.env

cd "$app_dir"
/opt/patent-news-monitor/venv/bin/python -m compileall -q gdelt_monitor gdelt_news_daily.py gdelt_news_preflight.py

run_as_app_with_env() (
    set -a
    # shellcheck disable=SC1091
    source /etc/patent-news-monitor.env
    set +a
    exec runuser -u app -- "$@"
)

run_as_app_with_env /opt/patent-news-monitor/venv/bin/python gdelt_news_preflight.py
run_as_app_with_env /opt/patent-news-monitor/venv/bin/python monitorctl.py validate
systemd-analyze verify /etc/systemd/system/gdelt-news-monitor.service /etc/systemd/system/gdelt-news-monitor.timer
systemctl daemon-reload
systemctl enable --now gdelt-news-monitor.timer >/dev/null

echo "DEPLOY_OK"
echo "BACKUP_DIR=$backup_dir"
systemctl is-enabled gdelt-news-monitor.timer
systemctl is-active gdelt-news-monitor.timer
systemctl list-timers gdelt-news-monitor.timer --no-pager
