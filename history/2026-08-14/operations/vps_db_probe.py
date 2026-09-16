import json
import sqlite3
from pathlib import Path

paths = [
    Path('/var/lib/market-monitor/platform.sqlite3'),
    Path('/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3'),
    Path('/var/lib/stock-bottom-monitor/notifications.sqlite3'),
    Path('/var/lib/stock-bottom-monitor/stock_cache.db'),
    Path('/var/lib/patent-news-monitor/patent_monitor.sqlite3'),
    Path('/var/lib/patent-news-monitor/backfill_v2/collector.sqlite3'),
    Path('/var/lib/patent-news-monitor/backfill_v2/patent_monitor_backfill_collector.sqlite3'),
]

for path in paths:
    result = {'path': str(path), 'bytes': path.stat().st_size if path.exists() else None}
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        )]
        counts = {}
        for table in tables:
            counts[table] = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        result['counts'] = counts
        if path.name == 'platform.sqlite3':
            result['latest_runs'] = [dict(r) for r in con.execute(
                "select service_id,started_at,completed_at,status,phase,exit_code from service_runs "
                "where (service_id,started_at) in (select service_id,max(started_at) from service_runs group by service_id) "
                "order by service_id"
            )]
        if path.name == 'gdelt_monitor.sqlite3':
            for table in ('service_state', 'collection_runs'):
                if table in tables:
                    rows = con.execute(f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT 20').fetchall()
                    result[table + '_latest'] = [dict(r) for r in rows]
        con.close()
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    print(json.dumps(result, ensure_ascii=False, default=str))
