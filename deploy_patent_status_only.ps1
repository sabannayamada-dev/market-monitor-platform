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

& scp.exe -i $Key "$Source\patent_backfill_status.py" "${Server}:/tmp/patent_backfill_status.py"
if ($LASTEXITCODE -ne 0) {
    throw "File upload failed."
}

$RemoteScript = @'
set -e
APP=/opt/patent-news-monitor/app
BACKUP=/opt/patent-news-monitor/backups/patent-status-only-$(date +%Y%m%d-%H%M%S)

sudo mkdir -p "$BACKUP"
if [ -f "$APP/patent_backfill_status.py" ]; then
    sudo cp "$APP/patent_backfill_status.py" "$BACKUP/patent_backfill_status.py"
fi
sudo install -o app -g app -m 0755 /tmp/patent_backfill_status.py "$APP/patent_backfill_status.py"

if ! sudo -u app /opt/patent-news-monitor/venv/bin/python -m py_compile "$APP/patent_backfill_status.py"; then
    if [ -f "$BACKUP/patent_backfill_status.py" ]; then
        sudo cp "$BACKUP/patent_backfill_status.py" "$APP/patent_backfill_status.py"
    fi
    echo "patent-status deployment failed; previous file was restored."
    exit 1
fi

sudo ln -sfn "$APP/patent_backfill_status.py" /usr/local/bin/patent-status
patent-status || true
'@

& ssh.exe -t -i $Key $Server $RemoteScript
if ($LASTEXITCODE -ne 0) {
    throw "Remote deployment failed. Check the output above."
}
