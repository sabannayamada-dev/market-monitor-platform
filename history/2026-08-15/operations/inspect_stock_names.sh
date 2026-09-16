sudo sqlite3 -json /var/lib/stock-bottom-monitor/stock_cache.db "SELECT symbol,name FROM stocks WHERE symbol IN ('6758.T','7203.T','6702.T','9432.T','7735.T','6146.T') ORDER BY symbol;"
