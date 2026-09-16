$ErrorActionPreference = "Stop"

$Downloads = "C:\Users\saban\Downloads"
$Files = @(
    Get-ChildItem -LiteralPath $Downloads -Recurse -Force -File `
        -ErrorAction SilentlyContinue
)
$Keys = @($Files | Where-Object Name -eq "patent-news-monitor-key.pem")
if ($Keys.Count -ne 1) {
    throw "Expected exactly one patent-news-monitor-key.pem under $Downloads; found $($Keys.Count)."
}

$Key = $Keys[0].FullName
$Server = "app@160.251.252.249"
$Report = "C:\Users\saban\Documents\Codex\2026-07-03\gem\vps_data_verification.txt"

$RemoteScript = @'
set -u

echo "=== VPS patent collector verification ==="
date --iso-8601=seconds
echo
echo "=== Service ==="
systemctl is-active patent-monitor-backfill || true
systemctl show patent-monitor-backfill \
  -p MainPID -p ActiveEnterTimestamp -p ExecMainStartTimestamp -p NRestarts \
  -p User -p WorkingDirectory -p ExecStart --no-pager || true

PID=$(systemctl show patent-monitor-backfill -p MainPID --value 2>/dev/null || true)
if [ -n "$PID" ] && [ "$PID" != "0" ]; then
  echo "process_cwd=$(readlink -f "/proc/$PID/cwd" 2>/dev/null || true)"
  printf 'process_command='
  tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true
  echo
fi

DB=$(find /opt/patent-news-monitor /home/app /var/lib /srv \
  -type f -name collector.sqlite3 -print -quit 2>/dev/null)
if [ -z "$DB" ]; then
  DB=$(sudo -n find / -xdev -type f -name collector.sqlite3 -print -quit 2>/dev/null || true)
fi
if [ -z "$DB" ]; then
  echo
  echo "ERROR: collector.sqlite3 was not found on the VPS"
  echo "SQLite files visible in likely storage locations:"
  find /opt/patent-news-monitor /home/app /var/lib /srv \
    -type f -name '*.sqlite3' -print 2>/dev/null | head -n 50 || true
  echo
  echo "=== Recent service log ==="
  sudo -n journalctl -u patent-monitor-backfill -n 80 --no-pager 2>/dev/null \
    || journalctl -u patent-monitor-backfill -n 80 --no-pager 2>&1 \
    || true
  exit 2
fi

echo
echo "=== Storage ==="
echo "database=$DB"
stat -c 'size_bytes=%s modified_at=%y' "$DB"
STORAGE_DIR=$(dirname "$DB")
du -sh "$STORAGE_DIR" 2>/dev/null || true
echo "Largest files in storage directory:"
find "$STORAGE_DIR" -maxdepth 2 -type f -printf '%s\t%p\n' 2>/dev/null \
  | sort -nr | head -n 20 || true
df -h /opt/patent-news-monitor | tail -n 1

python3 - "$DB" <<'PY'
import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1])
connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
connection.row_factory = sqlite3.Row

tables = {
    row[0]
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
}

print("\n=== Table counts ===")
wanted = [
    "collected_patents",
    "collector_windows",
    "collector_company_progress",
    "collector_staged_records",
    "epo_raw_payloads",
    "epo_raw_observations",
    "normalized_patent_records",
    "collector_company_quality_history",
]
for table in wanted:
    if table not in tables:
        print(f"{table}=MISSING")
        continue
    count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    print(f"{table}={count}")

if "collector_windows" in tables:
    print("\n=== Latest collection windows ===")
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(collector_windows)")
    }
    selected = [
        name for name in (
            "window_start", "window_end", "status", "started_at", "completed_at",
            "input_records", "kept_records", "matched_count", "epo_failure_count",
            "epo_throttle_count", "capped_company_count", "error",
        ) if name in columns
    ]
    sql = (
        "SELECT " + ",".join(selected)
        + " FROM collector_windows ORDER BY window_end DESC, window_start DESC LIMIT 5"
    )
    for row in connection.execute(sql):
        print(json.dumps(dict(row), ensure_ascii=True, separators=(",", ":")))

if "epo_raw_observations" in tables:
    print("\n=== Raw observation recency and coverage ===")
    row = connection.execute(
        """
        SELECT MIN(retrieved_at) AS oldest, MAX(retrieved_at) AS newest,
               SUM(source_endpoint <> '') AS endpoint_filled,
               SUM(query_window_start <> '' AND query_window_end <> '') AS window_filled,
               SUM(company_id <> '') AS company_filled,
               SUM(publication_number <> '') AS publication_filled
        FROM epo_raw_observations
        """
    ).fetchone()
    print(json.dumps(dict(row), ensure_ascii=True, separators=(",", ":")))

if "epo_raw_payloads" in tables:
    print("\n=== Raw payload integrity sample ===")
    rows = connection.execute(
        "SELECT payload_hash,content_gzip,response_size,parser_version "
        "FROM epo_raw_payloads ORDER BY first_retrieved_at DESC LIMIT 20"
    ).fetchall()
    valid = 0
    invalid = 0
    parser_versions = set()
    for row in rows:
        try:
            content = gzip.decompress(row["content_gzip"])
            ok = (
                len(content) == row["response_size"]
                and hashlib.sha256(content).hexdigest() == row["payload_hash"]
            )
        except Exception:
            ok = False
        valid += int(ok)
        invalid += int(not ok)
        parser_versions.add(row["parser_version"] or "EMPTY")
    print(
        f"sample_size={len(rows)} valid={valid} invalid={invalid} "
        f"parser_versions={','.join(sorted(parser_versions))}"
    )

if "normalized_patent_records" in tables:
    print("\n=== Normalized field coverage ===")
    columns = [
        "publication_number", "application_number", "family_id", "publication_date",
        "filing_date", "priority_date", "retrieved_at", "source_endpoint",
        "query_window_start", "query_window_end", "payload_hash", "parser_version",
        "citation_count", "family_size", "claim_count", "legal_status",
    ]
    total = connection.execute(
        "SELECT COUNT(*) FROM normalized_patent_records"
    ).fetchone()[0]
    print(f"total={total}")
    if total:
        for column in columns:
            if column in {"citation_count", "claim_count"}:
                predicate = f"{column} > 0"
            elif column == "family_size":
                predicate = "family_size > 1"
            else:
                predicate = f"{column} <> ''"
            filled = connection.execute(
                f"SELECT SUM({predicate}) FROM normalized_patent_records"
            ).fetchone()[0] or 0
            print(f"{column}: filled={filled} pct={filled * 100.0 / total:.1f}")

        samples = connection.execute(
            "SELECT record_json FROM normalized_patent_records "
            "ORDER BY last_seen_at DESC LIMIT 1000"
        ).fetchall()
        nested = {
            "priority_numbers": 0,
            "family_members": 0,
            "country_codes": 0,
            "citations": 0,
            "cited_by": 0,
            "non_patent_citations": 0,
            "legal_events": 0,
            "ownership_events": 0,
        }
        parsed = 0
        for sample in samples:
            try:
                record = json.loads(sample[0])
            except Exception:
                continue
            parsed += 1
            for field in nested:
                nested[field] += int(bool(record.get(field)))
        print(f"record_json_sample_parsed={parsed}")
        for field, filled in nested.items():
            pct = filled * 100.0 / parsed if parsed else 0.0
            print(f"{field}: filled={filled} pct={pct:.1f}")

if "collector_company_quality_history" in tables:
    print("\n=== Quality history summary ===")
    row = connection.execute(
        """
        SELECT COUNT(*) AS rows,
               SUM(status = 'completed') AS completed,
               SUM(capped <> 0) AS capped,
               SUM(failure_count) AS failures,
               SUM(retry_count) AS retries,
               SUM(throttle_count) AS throttles,
               MAX(updated_at) AS newest
        FROM collector_company_quality_history
        """
    ).fetchone()
    print(json.dumps(dict(row), ensure_ascii=True, separators=(",", ":")))

connection.close()
PY

echo
echo "=== Recent service log ==="
sudo -n journalctl -u patent-monitor-backfill -n 60 --no-pager 2>/dev/null \
  || journalctl -u patent-monitor-backfill -n 60 --no-pager 2>&1 \
  || true
'@

$Output = $RemoteScript | & ssh.exe -o BatchMode=yes -o ConnectTimeout=15 `
    -i $Key $Server "bash -s" 2>&1
$ExitCode = $LASTEXITCODE
$Output | Tee-Object -LiteralPath $Report
Write-Host ""
Write-Host "Verification report: $Report"
if ($ExitCode -ne 0) {
    throw "VPS verification failed with exit code $ExitCode."
}
