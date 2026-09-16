$ErrorActionPreference = "Stop"

$Downloads = "C:\Users\saban\Downloads"
$DownloadFiles = @(
    Get-ChildItem -LiteralPath $Downloads -Recurse -Force -File `
        -ErrorAction SilentlyContinue
)
$KeyCandidates = @(
    $DownloadFiles | Where-Object Name -eq "patent-news-monitor-key.pem"
)

if ($KeyCandidates.Count -eq 0) {
    throw "SSH private key was not found under: $Downloads"
}
if ($KeyCandidates.Count -gt 1) {
    $FoundPaths = ($KeyCandidates.FullName -join [Environment]::NewLine)
    throw "Multiple SSH private keys were found. Remove duplicates or set the path explicitly:`n$FoundPaths"
}

$Key = $KeyCandidates[0].FullName
$Source = "C:\Users\saban\Documents\Codex\2026-07-03\gem"
$Server = "app@160.251.252.249"

& scp.exe -i $Key `
    "$Source\patent_monitor\pipeline.py" `
    "$Source\collect_patent_backfill_forever.py" `
    "$Source\patent_backfill_status.py" `
    "$Source\patent_monitor_config.json" `
    "${Server}:/tmp/"
if ($LASTEXITCODE -ne 0) {
    throw "File upload failed."
}

$RemoteScript = @'
set -e
SERVICE=patent-monitor-backfill
APP=/opt/patent-news-monitor/app
BACKUP=/opt/patent-news-monitor/backups/20260718-bottom-detection-v2
DATA=/var/lib/patent-news-monitor/backfill_v2

sudo systemctl stop "$SERVICE"
sudo mkdir -p "$BACKUP/patent_monitor"
sudo cp "$APP/patent_monitor/pipeline.py" "$BACKUP/patent_monitor/pipeline.py"
sudo cp "$APP/collect_patent_backfill_forever.py" "$BACKUP/collect_patent_backfill_forever.py"
if [ -f "$APP/patent_backfill_status.py" ]; then
    sudo cp "$APP/patent_backfill_status.py" "$BACKUP/patent_backfill_status.py"
fi
sudo cp "$APP/patent_monitor_config.json" "$BACKUP/patent_monitor_config.json"
sudo install -o app -g app -m 0644 /tmp/pipeline.py "$APP/patent_monitor/pipeline.py"
sudo install -o app -g app -m 0644 /tmp/collect_patent_backfill_forever.py "$APP/collect_patent_backfill_forever.py"
sudo install -o app -g app -m 0755 /tmp/patent_backfill_status.py "$APP/patent_backfill_status.py"
sudo install -o app -g app -m 0644 /tmp/patent_monitor_config.json "$APP/patent_monitor_config.json"

if ! sudo -u app /opt/patent-news-monitor/venv/bin/python -m py_compile \
    "$APP/patent_monitor/pipeline.py" "$APP/collect_patent_backfill_forever.py" \
    "$APP/patent_backfill_status.py"; then
    sudo cp "$BACKUP/patent_monitor/pipeline.py" "$APP/patent_monitor/pipeline.py"
    sudo cp "$BACKUP/collect_patent_backfill_forever.py" "$APP/collect_patent_backfill_forever.py"
    if [ -f "$BACKUP/patent_backfill_status.py" ]; then
        sudo cp "$BACKUP/patent_backfill_status.py" "$APP/patent_backfill_status.py"
    else
        sudo rm -f "$APP/patent_backfill_status.py"
    fi
    sudo cp "$BACKUP/patent_monitor_config.json" "$APP/patent_monitor_config.json"
    sudo chown app:app "$APP/patent_monitor/pipeline.py" \
        "$APP/collect_patent_backfill_forever.py" "$APP/patent_monitor_config.json"
    sudo systemctl start "$SERVICE"
    echo "Deployment failed; previous files were restored."
    exit 1
fi

sudo ln -sfn "$APP/patent_backfill_status.py" /usr/local/bin/patent-status
sudo install -o app -g app -d "$DATA"
sudo mkdir -p /etc/systemd/system/"$SERVICE".service.d
sudo tee /etc/systemd/system/"$SERVICE".service.d/bottom-detection-v2.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=/opt/patent-news-monitor/venv/bin/python $APP/collect_patent_backfill_forever.py --output-dir $DATA --company-master $APP/patent_company_master.csv --config $APP/patent_monitor_config.json --window-days 30 --adaptive-company-windows --low-volume-window-days 90 --split-window-days 7 --min-window-days 1 --low-volume-max-records 10 --detail-max-records 0 --company-stall-skip-seconds 1800 --gpt-top-rate 0.12 --gpt-audit-rate 0.005 --gemini-top-rate 0.12 --annual-ai-budget-jpy 6000 --claims-fulltext-top-rate 0.02
EOF
sudo systemctl daemon-reload
sudo systemctl start "$SERVICE"
sudo systemctl is-active "$SERVICE"
sleep 2
patent-status || true
sudo journalctl -u "$SERVICE" -n 30 --no-pager
'@

& ssh.exe -t -i $Key $Server $RemoteScript
if ($LASTEXITCODE -ne 0) {
    throw "Remote deployment failed. Check the output above."
}
