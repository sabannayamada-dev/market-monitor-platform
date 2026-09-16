set -eu
export PYTHONPYCACHEPREFIX=/tmp/deepl-deploy-pycache
python3 -m py_compile /tmp/deepl_translation.py /tmp/gdelt_notifications.py /tmp/patent_notifications.py
test -w /opt/patent-news-monitor/app/monitor_core
test -w /opt/patent-news-monitor/app/gdelt_monitor
test -w /opt/patent-news-monitor/app/patent_monitor
test -w /var/lib/market-monitor
backup=/opt/patent-news-monitor/backups/20260817_deepl_translation
mkdir -p "$backup/monitor_core" "$backup/gdelt_monitor" "$backup/patent_monitor"
cp /opt/patent-news-monitor/app/gdelt_monitor/notifications.py "$backup/gdelt_monitor/notifications.py"
cp /opt/patent-news-monitor/app/patent_monitor/notifications.py "$backup/patent_monitor/notifications.py"

# New module first: interruption here leaves every production import unchanged.
cp /tmp/deepl_translation.py /opt/patent-news-monitor/app/monitor_core/deepl_translation.py.new
mv /opt/patent-news-monitor/app/monitor_core/deepl_translation.py.new /opt/patent-news-monitor/app/monitor_core/deepl_translation.py
python3 -m py_compile /opt/patent-news-monitor/app/monitor_core/deepl_translation.py

# Each existing module is replaced atomically. All translation failures fall back to source text.
cp /tmp/gdelt_notifications.py /opt/patent-news-monitor/app/gdelt_monitor/notifications.py.new
mv /opt/patent-news-monitor/app/gdelt_monitor/notifications.py.new /opt/patent-news-monitor/app/gdelt_monitor/notifications.py
PYTHONPATH=/opt/patent-news-monitor/app python3 -m py_compile /opt/patent-news-monitor/app/gdelt_monitor/notifications.py

cp /tmp/patent_notifications.py /opt/patent-news-monitor/app/patent_monitor/notifications.py.new
mv /opt/patent-news-monitor/app/patent_monitor/notifications.py.new /opt/patent-news-monitor/app/patent_monitor/notifications.py
PYTHONPATH=/opt/patent-news-monitor/app python3 -m py_compile /opt/patent-news-monitor/app/patent_monitor/notifications.py
echo deploy_complete
