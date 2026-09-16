import json
import sys

sys.path.insert(0, "/opt/patent-news-monitor/app")

from monitor_core.journal import RunJournal


journal = RunJournal("/var/lib/market-monitor/platform.sqlite3")
result = {
    service_id: journal.recover_interrupted(service_id)
    for service_id in ("gdelt-news", "patent-materiality", "stock-bottom")
}
print(json.dumps(result, ensure_ascii=False))
