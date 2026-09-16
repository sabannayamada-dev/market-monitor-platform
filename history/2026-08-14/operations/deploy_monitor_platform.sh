#!/usr/bin/env bash
set -euo pipefail

stage=/tmp/monitor-platform-stage-20260814
app_dir=/opt/patent-news-monitor/app
backup_root=/opt/patent-news-monitor/backups
stamp="$(date +%Y%m%d_%H%M%S)"
backup_dir="$backup_root/monitor-platform-$stamp"

install -d -o app -g app -m 0755 "$app_dir/monitor_core"
install -d -o app -g app -m 0755 "$app_dir/patent_monitor"
install -d -o app -g app -m 0750 /var/lib/market-monitor /var/lib/market-monitor/locks
install -d -o root -g root -m 0750 "$backup_dir/app/monitor_core" "$backup_dir/app/patent_monitor" "$backup_dir/systemd"

backup_if_present() {
    local source="$1"
    local destination="$2"
    if [[ -e "$source" ]]; then
        cp -a "$source" "$destination"
    fi
}

backup_if_present "$app_dir/monitorctl.py" "$backup_dir/app/"
backup_if_present "$app_dir/monitor_services.json" "$backup_dir/app/"
backup_if_present "$app_dir/stock_bottom_daily.py" "$backup_dir/app/"
backup_if_present "$app_dir/stock_bottom_notifications.py" "$backup_dir/app/"
backup_if_present "$app_dir/patent_monitor_daily.py" "$backup_dir/app/"
backup_if_present "$app_dir/patent_monitor/notifications.py" "$backup_dir/app/patent_monitor/"
for file in __init__.py mail.py locking.py registry.py journal.py; do
    backup_if_present "$app_dir/monitor_core/$file" "$backup_dir/app/monitor_core/"
done
backup_if_present /etc/systemd/system/stock-bottom-daily.service "$backup_dir/systemd/"
backup_if_present /etc/systemd/system/patent-monitor-daily.service "$backup_dir/systemd/"
cp -a /etc/patent-news-monitor.env "$backup_dir/patent-news-monitor.env"
chmod 0600 "$backup_dir/patent-news-monitor.env"

install -o app -g app -m 0644 "$stage/monitorctl.py" "$app_dir/monitorctl.py"
install -o app -g app -m 0644 "$stage/monitor_services.json" "$app_dir/monitor_services.json"
install -o app -g app -m 0644 "$stage/stock_bottom_daily.py" "$app_dir/stock_bottom_daily.py"
install -o app -g app -m 0644 "$stage/stock_bottom_notifications.py" "$app_dir/stock_bottom_notifications.py"
install -o app -g app -m 0644 "$stage/patent_monitor_daily.py" "$app_dir/patent_monitor_daily.py"
install -o app -g app -m 0644 "$stage/patent_notifications.py" "$app_dir/patent_monitor/notifications.py"
install -o app -g app -m 0644 "$stage/MONITOR_PLATFORM_ARCHITECTURE.md" "$app_dir/MONITOR_PLATFORM_ARCHITECTURE.md"
for file in __init__.py mail.py locking.py registry.py journal.py; do
    install -o app -g app -m 0644 "$stage/monitor_core/$file" "$app_dir/monitor_core/$file"
done
install -o root -g root -m 0644 "$stage/stock-bottom-daily.service" /etc/systemd/system/stock-bottom-daily.service
install -o root -g root -m 0644 "$stage/patent-monitor-daily.service" /etc/systemd/system/patent-monitor-daily.service

env_tmp="$(mktemp)"
trap 'rm -f "$env_tmp"' EXIT
grep -Ev '^(MONITOR_REGISTRY_PATH|MONITOR_PLATFORM_STATE_DB|MONITOR_PLATFORM_LOCK_DIR)=' /etc/patent-news-monitor.env > "$env_tmp" || true
cat >> "$env_tmp" <<'EOF'
MONITOR_REGISTRY_PATH=/opt/patent-news-monitor/app/monitor_services.json
MONITOR_PLATFORM_STATE_DB=/var/lib/market-monitor/platform.sqlite3
MONITOR_PLATFORM_LOCK_DIR=/var/lib/market-monitor/locks
EOF
install -o root -g root -m 0600 "$env_tmp" /etc/patent-news-monitor.env

cd "$app_dir"
/opt/patent-news-monitor/venv/bin/python -m py_compile \
    monitorctl.py \
    monitor_core/__init__.py monitor_core/mail.py monitor_core/locking.py monitor_core/registry.py monitor_core/journal.py \
    stock_bottom_daily.py stock_bottom_notifications.py patent_monitor_daily.py patent_monitor/notifications.py

run_as_app_with_env() (
    set -a
    # shellcheck disable=SC1091
    source /etc/patent-news-monitor.env
    set +a
    exec runuser -u app -- "$@"
)

run_as_app_with_env /opt/patent-news-monitor/venv/bin/python monitorctl.py validate
run_as_app_with_env /opt/patent-news-monitor/venv/bin/python monitorctl.py preflight stock-bottom
run_as_app_with_env /opt/patent-news-monitor/venv/bin/python monitorctl.py preflight patent-materiality

systemd-analyze verify /etc/systemd/system/stock-bottom-daily.service /etc/systemd/system/patent-monitor-daily.service
systemctl daemon-reload
systemctl enable stock-bottom-daily.timer patent-monitor-daily.timer >/dev/null

echo "DEPLOY_OK"
echo "BACKUP_DIR=$backup_dir"
systemctl is-enabled stock-bottom-daily.timer patent-monitor-daily.timer
systemctl is-active stock-bottom-daily.timer patent-monitor-daily.timer
systemctl list-timers stock-bottom-daily.timer patent-monitor-daily.timer --no-pager
