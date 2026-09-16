import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/opt/patent-news-monitor/app")

from patent_monitor.pipeline import EPOOPSProvider, PipelineConfig
from patent_monitor.secure_store import load_credentials


config = PipelineConfig.from_json("patent_monitor_config.json")
secrets = load_credentials()
provider = EPOOPSProvider(
    secrets["epo_ops_key"],
    secrets["epo_ops_secret"],
    config.ops_requests_per_minute,
    config.request_timeout_seconds,
)
today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
start = (datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=2)).isoformat()
records, stats = provider.search_technologies(
    config.technology_discovery_queries,
    start,
    today,
    max_records_per_query=1,
)
result = json.dumps({
    "date": today,
    "start": start,
    "executed": stats["executed_query_count"],
    "successful": stats["successful_query_count"],
    "errors": stats["error_count"],
    "unique_records": len(records),
    "queries": stats["queries"],
}, ensure_ascii=False, indent=2)
print(result)
with open("/var/lib/gdelt-news-monitor/patent-tech-probe-result.json", "w", encoding="utf-8") as handle:
    handle.write(result)
