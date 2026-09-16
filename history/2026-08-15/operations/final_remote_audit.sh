set -eu
echo STOCK_RUN
sudo sqlite3 -json /var/lib/stock-bottom-monitor/notifications.sqlite3 "SELECT run_id,started_at,completed_at,target_count,completed_count,updated_count,detected_count,new_signal_count,sent_count,error_count,status,detail_json FROM monitor_runs ORDER BY started_at DESC LIMIT 1;"
echo STOCK_SENT
sudo sqlite3 -json /var/lib/stock-bottom-monitor/notifications.sqlite3 "SELECT symbol,stable_date,delivery_status,sent_at,last_error FROM bottom_signals WHERE delivery_status='sent' ORDER BY sent_at DESC;"
echo PLATFORM_SERVICES
sudo sqlite3 -json /var/lib/market-monitor/platform.sqlite3 "SELECT service_id,count(*) AS runs,MAX(started_at) AS latest FROM service_runs GROUP BY service_id ORDER BY service_id;"
echo LATEST_STOCK_PLATFORM
sudo sqlite3 -json /var/lib/market-monitor/platform.sqlite3 "SELECT service_id,run_id,started_at,completed_at,status,phase,exit_code,detail_json FROM service_runs WHERE service_id LIKE '%stock%' ORDER BY started_at DESC LIMIT 2;"
echo LATEST_PATENT_PLATFORM
sudo sqlite3 -json /var/lib/market-monitor/platform.sqlite3 "SELECT service_id,run_id,started_at,completed_at,status,phase,exit_code,detail_json FROM service_runs WHERE service_id LIKE '%patent%' ORDER BY started_at DESC LIMIT 2;"
echo TIMERS
systemctl list-timers --all --no-pager | grep -E 'stock-bottom|patent-monitor'
echo HASHES
sha256sum /opt/patent-news-monitor/app/app.py /opt/patent-news-monitor/app/patent_monitor/market_feedback.py /opt/patent-news-monitor/app/patent_monitor/pipeline.py /opt/patent-news-monitor/app/analyze_patent_monitor_run.py
