set -eu
cd /opt/patent-news-monitor/app
sudo -u app env STOCK_CACHE_DB=/var/lib/stock-bottom-monitor/stock_cache.db /opt/patent-news-monitor/venv/bin/python -c 'from pathlib import Path
from stock_bottom_daily import analyze_symbol, load_stock_app
app = load_stock_app(Path("/opt/patent-news-monitor/app/app.py"), Path("/var/lib/stock-bottom-monitor/stock_cache.db"))
for symbol in ("4384.T", "6173.T", "7098.T"):
    print(analyze_symbol(app, symbol, None))'
