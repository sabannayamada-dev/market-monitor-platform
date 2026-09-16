set -u
SINCE="2026-08-19 00:00:00"
echo '=== CLOCK ==='
date --iso-8601=seconds
uptime
echo '=== TIMERS ==='
systemctl list-timers gdelt-news-monitor.timer patent-monitor-daily.timer stock-bottom-daily.timer --all --no-pager || true
echo '=== UNIT STATES ==='
for unit in gdelt-news-monitor.service gdelt-news-monitor.timer patent-monitor-daily.service patent-monitor-daily.timer stock-bottom-daily.service stock-bottom-daily.timer; do
  printf '%s active=' "$unit"
  systemctl is-active "$unit" 2>/dev/null || true
  printf '%s enabled=' "$unit"
  systemctl is-enabled "$unit" 2>/dev/null || true
done
echo '=== UNIT RESULTS ==='
for unit in gdelt-news-monitor.service patent-monitor-daily.service stock-bottom-daily.service; do
  systemctl show "$unit" -p Result -p ExecMainStatus -p NRestarts -p ActiveEnterTimestamp -p InactiveEnterTimestamp --no-pager || true
done
echo '=== LOG FILES ==='
find /var/log/gdelt-news-monitor /var/log/patent-news-monitor /var/log/stock-bottom-monitor -maxdepth 2 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' 2>/dev/null | sort || true
echo '=== STATE FILES ==='
find /var/lib/gdelt-news-monitor /var/lib/patent-news-monitor /var/lib/stock-bottom-monitor /var/lib/market-monitor -maxdepth 3 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' 2>/dev/null | sort || true
echo '=== JOURNAL COUNTS SINCE ==='
for unit in gdelt-news-monitor.service patent-monitor-daily.service stock-bottom-daily.service; do
  total=$(sudo -n journalctl -u "$unit" --since "$SINCE" --no-pager 2>/dev/null | wc -l)
  failures=$(sudo -n journalctl -u "$unit" --since "$SINCE" --no-pager 2>/dev/null | grep -Eci 'failed|failure|error|exception|traceback|timeout|429|403' || true)
  echo "$unit lines=$total suspicious=$failures"
done
echo '=== FAILED/ERROR JOURNAL EXCERPTS ==='
for unit in gdelt-news-monitor.service patent-monitor-daily.service stock-bottom-daily.service; do
  echo "--- $unit ---"
  sudo -n journalctl -u "$unit" --since "$SINCE" --no-pager 2>/dev/null | grep -Ei 'failed|failure|error|exception|traceback|timeout|429|403' | tail -n 80 || true
done
