import json
import sqlite3
import sys


path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gdelt-pdca-20260816/emergency_validation.sqlite3"
connection = sqlite3.connect(path)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    """SELECT a.title,a.domain,a.original_url,s.score,
              GROUP_CONCAT(DISTINCT m.company_name) AS companies,
              GROUP_CONCAT(DISTINCT m.alias) AS aliases,
              GROUP_CONCAT(DISTINCT d.profile) AS profiles
       FROM articles a
       JOIN article_scores s ON s.article_id=a.article_id
       LEFT JOIN article_company_matches m ON m.article_id=a.article_id
       LEFT JOIN article_discoveries d ON d.article_id=a.article_id
       GROUP BY a.article_id
       ORDER BY s.score DESC,a.title"""
).fetchall()
print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
connection.close()
