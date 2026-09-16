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
    "${Server}:/tmp/"
if ($LASTEXITCODE -ne 0) {
    throw "File upload failed."
}

$RemoteScript = @'
set -e
SERVICE=patent-monitor-backfill
APP=/opt/patent-news-monitor/app
BACKUP=/opt/patent-news-monitor/backups/runtime-diagnostics-$(date +%Y%m%d-%H%M%S)

sudo mkdir -p "$BACKUP"
sudo mkdir -p "$BACKUP/patent_monitor"
sudo cp "$APP/patent_monitor/pipeline.py" "$BACKUP/patent_monitor/pipeline.py"
sudo cp "$APP/collect_patent_backfill_forever.py" "$BACKUP/collect_patent_backfill_forever.py"
if [ -f "$APP/patent_backfill_status.py" ]; then
    sudo cp "$APP/patent_backfill_status.py" "$BACKUP/patent_backfill_status.py"
fi

sudo systemctl stop "$SERVICE"
sudo install -o app -g app -m 0644 /tmp/pipeline.py "$APP/patent_monitor/pipeline.py"
sudo install -o app -g app -m 0644 /tmp/collect_patent_backfill_forever.py "$APP/collect_patent_backfill_forever.py"
sudo install -o app -g app -m 0755 /tmp/patent_backfill_status.py "$APP/patent_backfill_status.py"

if ! sudo -u app /opt/patent-news-monitor/venv/bin/python -m py_compile \
    "$APP/patent_monitor/pipeline.py" "$APP/collect_patent_backfill_forever.py" "$APP/patent_backfill_status.py"; then
    sudo cp "$BACKUP/patent_monitor/pipeline.py" "$APP/patent_monitor/pipeline.py"
    sudo cp "$BACKUP/collect_patent_backfill_forever.py" "$APP/collect_patent_backfill_forever.py"
    if [ -f "$BACKUP/patent_backfill_status.py" ]; then
        sudo cp "$BACKUP/patent_backfill_status.py" "$APP/patent_backfill_status.py"
    fi
    sudo systemctl start "$SERVICE"
    echo "Runtime diagnostics deployment failed; previous files were restored."
    exit 1
fi

sudo ln -sfn "$APP/patent_backfill_status.py" /usr/local/bin/patent-status
sudo systemctl start "$SERVICE"
sudo systemctl is-active "$SERVICE"
sleep 10
patent-status || true
'@

& ssh.exe -t -i $Key $Server $RemoteScript
if ($LASTEXITCODE -ne 0) {
    throw "Remote deployment failed. Check the output above."
}
