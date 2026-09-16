#!/usr/bin/env bash
set -euo pipefail

backup=/opt/patent-news-monitor/backups/20260902_other_improvements
install -d -o app -g app -m 0750 "$backup"
cp -a /opt/patent-news-monitor/app/monitor_core/journal.py "$backup/journal.py"
cp -a /opt/patent-news-monitor/app/monitorctl.py "$backup/monitorctl.py"
cp -a /opt/patent-news-monitor/app/patent_monitor/pipeline.py "$backup/pipeline.py"
cp -a /opt/patent-news-monitor/app/patent_monitor_daily.py "$backup/patent_monitor_daily.py"
cp -a /opt/patent-news-monitor/app/patent_monitor/notifications.py "$backup/notifications.py"
cp -a /opt/patent-news-monitor/app/patent_monitor_config.json "$backup/patent_monitor_config.json"
cp -a /opt/patent-news-monitor/app/gdelt_monitor_config.json "$backup/gdelt_monitor_config.json"
cp -a /etc/systemd/system/gdelt-news-monitor.timer "$backup/gdelt-news-monitor.timer"
cp -a /etc/systemd/system/stock-daily-path-features.service "$backup/stock-daily-path-features.service"

install -o app -g app -m 0640 /tmp/improve_journal.py /opt/patent-news-monitor/app/monitor_core/journal.py
install -o app -g app -m 0640 /tmp/improve_monitorctl.py /opt/patent-news-monitor/app/monitorctl.py
install -o app -g app -m 0640 /tmp/improve_pipeline.py /opt/patent-news-monitor/app/patent_monitor/pipeline.py
install -o app -g app -m 0640 /tmp/improve_patent_daily.py /opt/patent-news-monitor/app/patent_monitor_daily.py
install -o app -g app -m 0640 /tmp/improve_notifications.py /opt/patent-news-monitor/app/patent_monitor/notifications.py
install -o app -g app -m 0640 /tmp/improve_patent_config.json /opt/patent-news-monitor/app/patent_monitor_config.json
install -o app -g app -m 0640 /tmp/improve_gdelt_config.json /opt/patent-news-monitor/app/gdelt_monitor_config.json
install -o root -g root -m 0644 /tmp/improve_gdelt.timer /etc/systemd/system/gdelt-news-monitor.timer
install -o root -g root -m 0644 /tmp/improve_stock_features.service /etc/systemd/system/stock-daily-path-features.service

install -d -o app -g app -m 0750 /var/lib/stock-daily-path-features
install -d -o app -g app -m 0750 /var/lib/stock-daily-path-features/cache
install -d -o app -g app -m 0750 /var/lib/stock-daily-path-features/output
install -d -o app -g app -m 0750 /var/lib/stock-daily-path-features/input
chown -R app:app /var/lib/stock-daily-path-features/cache /var/lib/stock-daily-path-features/output

systemctl daemon-reload
systemctl restart gdelt-news-monitor.timer
systemctl reset-failed stock-daily-path-features.service
echo deployment-ok
