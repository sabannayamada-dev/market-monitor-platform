set -u
echo '=== PATENT TIMEOUT DAY TAILS ==='
for day in 2026-08-19 2026-08-21 2026-08-27; do
  echo "--- $day ---"
  tail -n 35 "/var/lib/patent-news-monitor/outputs/daily_logs/$day.log" 2>/dev/null || true
done
echo '=== PATENT WARNING FREQUENCIES ==='
grep -Ehi 'warning|warn|警告|error|失敗|timeout|上限|deadline|停止' /var/lib/patent-news-monitor/outputs/daily_logs/2026-08-{19,20,21,22,23,24,25,26,27,28,29,30,31}.log /var/lib/patent-news-monitor/outputs/daily_logs/2026-09-01.log 2>/dev/null \
  | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+//g; s/[0-9]+\/[0-9]+/N\/N/g; s/[0-9]+件/N件/g' \
  | sort | uniq -c | sort -nr | head -n 80 || true
echo '=== STOCK ERROR/WARNING FREQUENCIES ==='
grep -Ehi 'warning|warn|警告|error|失敗|exception|timeout|HTTP' /var/log/stock-bottom-monitor/daily.log 2>/dev/null \
  | tail -n 2000 | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+//g' \
  | sort | uniq -c | sort -nr | head -n 80 || true
echo '=== STOCK LOG TAIL ==='
tail -n 100 /var/log/stock-bottom-monitor/daily.log 2>/dev/null || true
echo '=== GDELT LOG KEY COUNTS ==='
for pattern in '429' '403' 'timeout' 'DeepL translation unavailable' 'email_error' 'emergency_ai' 'approved' 'ready_alerts'; do
  count=$(grep -Fic "$pattern" /var/log/gdelt-news-monitor/service.log 2>/dev/null || true)
  echo "$pattern=$count"
done
