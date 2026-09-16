#!/usr/bin/env bash
set -u

echo "=== timestamp ==="
date --iso-8601=seconds

echo "=== timers ==="
systemctl list-timers --all --no-pager | grep -E 'stock-bottom|patent-monitor|gdelt-news' || true

echo "=== unit status ==="
for unit in stock-bottom-daily.service patent-monitor-daily.service gdelt-news-monitor.service; do
  echo "--- $unit"
  systemctl show "$unit" --no-pager \
    -p ActiveState -p SubState -p Result -p ExecMainStatus \
    -p ExecMainStartTimestamp -p ExecMainExitTimestamp
done

echo "=== monitor journal ==="
cd /opt/patent-news-monitor/app
MONITOR_PLATFORM_DB=/var/lib/market-monitor/platform.sqlite3 \
  /opt/patent-news-monitor/venv/bin/python monitorctl.py status || true

echo "=== recent gdelt logs ==="
journalctl -u gdelt-news-monitor.service -n 35 --no-pager -o short-iso 2>/dev/null || true

echo "=== database and data sizes ==="
find /var/lib/market-monitor /opt/patent-news-monitor -maxdepth 4 -type f \
  \( -name '*.sqlite3' -o -name '*.db' -o -name '*.json' \) \
  -printf '%s\t%TY-%Tm-%TdT%TH:%TM:%TS%Tz\t%p\n' 2>/dev/null | sort -n
du -sh /var/lib/market-monitor /opt/patent-news-monitor 2>/dev/null || true
df -h / /var/lib/market-monitor 2>/dev/null || true

echo "=== sqlite summaries ==="
/opt/patent-news-monitor/venv/bin/python - <<'PY'
import json, sqlite3
from pathlib import Path

roots = [Path('/var/lib/market-monitor'), Path('/opt/patent-news-monitor')]
dbs = []
for root in roots:
    if root.exists():
        dbs.extend(root.rglob('*.sqlite3'))
        dbs.extend(root.rglob('*.db'))
seen = set()
for path in sorted(dbs):
    real = str(path.resolve())
    if real in seen:
        continue
    seen.add(real)
    try:
        con = sqlite3.connect(f'file:{real}?mode=ro', uri=True, timeout=3)
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        counts = {}
        for table in tables:
            try:
                counts[table] = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except Exception as exc:
                counts[table] = f'ERROR:{type(exc).__name__}'
        state = {}
        if 'service_state' in tables:
            cols = [r[1] for r in con.execute('pragma table_info(service_state)')]
            rows = con.execute('select * from service_state order by 1').fetchall()
            state = {'columns': cols, 'rows': rows}
        print(json.dumps({'path': real, 'counts': counts, 'service_state': state}, ensure_ascii=False, default=str))
        con.close()
    except Exception as exc:
        print(json.dumps({'path': real, 'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
PY

echo "=== backfill unit presence ==="
systemctl list-unit-files --no-pager | grep -Ei 'backfill|patent.*collect' || true
systemctl list-units --all --no-pager | grep -Ei 'backfill|patent.*collect' || true
