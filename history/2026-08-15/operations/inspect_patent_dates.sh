sqlite3 -json /var/lib/patent-news-monitor/patent_monitor.sqlite3 "SELECT company_id,publication_date,count(*) AS n FROM patents WHERE length(publication_date)=8 GROUP BY company_id,publication_date ORDER BY publication_date DESC LIMIT 20;"
head -n 3 /opt/patent-news-monitor/app/patent_company_master.csv
