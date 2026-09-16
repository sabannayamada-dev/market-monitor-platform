set -eu
sudo -u app env STOCK_CACHE_DB=/var/lib/stock-bottom-monitor/stock_cache.db /opt/patent-news-monitor/venv/bin/python -m py_compile /opt/patent-news-monitor/app/app.py
cd /opt/patent-news-monitor/app
sudo -u app env STOCK_CACHE_DB=/var/lib/stock-bottom-monitor/stock_cache.db /opt/patent-news-monitor/venv/bin/python -c 'import app
try:
    with app.batch_timing_step("production-test"):
        raise RuntimeError("expected")
except RuntimeError as exc:
    assert str(exc) == "expected"
    print("production exception propagation: OK")
else:
    raise AssertionError("batch_timing_step suppressed the exception")'
sha256sum /opt/patent-news-monitor/app/app.py /tmp/market-monitor-fix-20260816_2212/app.py
