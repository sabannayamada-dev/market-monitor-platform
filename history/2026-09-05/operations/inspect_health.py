import sqlite3, json, pathlib, gzip
c = sqlite3.connect('file:/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3?mode=ro', uri=True)
c.row_factory = sqlite3.Row
def show(label, query):
    print(label, json.dumps([dict(r) for r in c.execute(query)], ensure_ascii=False))
show('CANDIDATES', "SELECT * FROM emergency_candidates WHERE discovered_at >= '2026-09-05' AND (lower(title) LIKE '%trump%' OR lower(title) LIKE '%president%') ORDER BY discovered_at DESC LIMIT 40")
show('ARTICLES', "SELECT title,original_url,first_seen_at FROM articles WHERE first_seen_at >= '2026-09-05' AND (lower(title) LIKE '%trump%' OR lower(title) LIKE '%health%') ORDER BY first_seen_at DESC LIMIT 40")
show('SCOUT', "SELECT * FROM emergency_scout_runs ORDER BY started_at DESC LIMIT 8")
show('REVIEWS', "SELECT event_key,status,event_confirmed,market_impact,urgency,japanese_summary,reason,reviewed_at FROM emergency_ai_reviews WHERE reviewed_at >= '2026-09-05' ORDER BY reviewed_at DESC LIMIT 20")
show('ALERTS', "SELECT * FROM emergency_alerts ORDER BY alerted_at DESC LIMIT 5")
p = pathlib.Path('/opt/patent-news-monitor/app/gdelt_monitor/emergency.py')
lines = p.read_text().splitlines()
print('LIVE_RULE', '\n'.join(lines[65:112]))
root = pathlib.Path('/var/lib/gdelt-news-monitor/toc_cache')
print('CACHE_COUNT', len(list(root.glob('*'))))
hits = 0
for path in sorted(root.glob('*.gz')):
    try:
        content = gzip.open(path, 'rt', encoding='utf-8').read()
        data = json.loads(content)
        rows = data if isinstance(data,list) else data.get('articles', data.get('data', []))
        for row in rows:
            if not isinstance(row,dict): continue
            title = str(row.get('title',''))
            if 'trump' in title.lower() and any(w in title.lower() for w in ['health','hospital','ill','stroke','sick','medical','dead','death','deteriorat']):
                print('CACHE_HIT', path.name, json.dumps(row,ensure_ascii=False)); hits += 1
    except Exception as e:
        print('CACHE_ERROR',path.name,type(e).__name__)
print('CACHE_HITS',hits)
