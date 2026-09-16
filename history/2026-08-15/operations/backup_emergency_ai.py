import sqlite3


source = sqlite3.connect("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3")
target = sqlite3.connect(
    "/opt/patent-news-monitor/backups/20260817_emergency_ai/gdelt_monitor.sqlite3"
)
source.backup(target)
target.close()
source.close()
print("database_backup_ok")
