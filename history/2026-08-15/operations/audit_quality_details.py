import json
import sqlite3


path = "/tmp/gdelt-pdca-20260816/quality_validation.sqlite3"
connection = sqlite3.connect(path)
connection.row_factory = sqlite3.Row
print(json.dumps({
    "article_columns": [row[1] for row in connection.execute("PRAGMA table_info(articles)")],
    "discoveries": [dict(row) for row in connection.execute(
        "SELECT a.article_id,a.title,a.seen_date,a.original_url,a.raw_json,d.query_id,d.profile,d.severity "
        "FROM articles a JOIN article_discoveries d ON d.article_id=a.article_id ORDER BY a.article_id"
    )],
}, ensure_ascii=False, indent=2))
connection.close()
