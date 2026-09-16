from pathlib import Path
import sqlite3

from patent_monitor.market_feedback import MarketFeedbackService
from patent_monitor.pipeline import read_companies


source_path = Path("/var/lib/patent-news-monitor/patent_monitor.sqlite3")
work_dir = Path("/tmp/patent-market-validation-20260816")
work_dir.mkdir(parents=True, exist_ok=True)
copy_path = work_dir / "patent_monitor_validation.sqlite3"
if copy_path.exists():
    copy_path.unlink()

source = sqlite3.connect(source_path)
target = sqlite3.connect(copy_path)
source.backup(target)
source.close()

companies = read_companies("/opt/patent-news-monitor/app/patent_company_master.csv")
by_id = {company.company_id: company for company in companies if company.ticker}
placeholders = ",".join("?" for _ in by_id)
row = target.execute(
    f"""SELECT patent_id,company_id,publication_date FROM patents
        WHERE company_id IN ({placeholders})
          AND length(publication_date)=8
          AND publication_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
          AND publication_date<='20260715'
        ORDER BY publication_date DESC LIMIT 1""",
    tuple(by_id),
).fetchone()
if row is None:
    raise RuntimeError("検証対象となるYYYYMMDD形式の特許がありません")

patent_id, company_id, publication_date = row
target.execute("DELETE FROM patents WHERE patent_id<>?", (patent_id,))
target.execute("DELETE FROM market_outcomes")
target.commit()
target.close()

company = by_id[company_id]
print(
    {
        "patent_id": patent_id,
        "company_id": company_id,
        "ticker": company.ticker,
        "source_publication_date": publication_date,
    }
)
result = MarketFeedbackService(copy_path, companies, work_dir / "outputs").run(
    progress=lambda message, ratio: print(f"{ratio:.2f} {message}")
)
print(result)

connection = sqlite3.connect(copy_path)
print(
    connection.execute(
        "SELECT publication_date,window_days,base_date,target_date FROM market_outcomes ORDER BY window_days"
    ).fetchall()
)
connection.close()
