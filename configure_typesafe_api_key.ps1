$ErrorActionPreference = "Stop"

$Server = "app@160.251.252.249"
$Downloads = Join-Path $env:USERPROFILE "Downloads"
$KeyCandidates = @(
    Get-ChildItem -LiteralPath $Downloads -Recurse -Force -File `
        -Filter "patent-news-monitor-key.pem" -ErrorAction SilentlyContinue
)
if ($KeyCandidates.Count -ne 1) {
    throw "Expected exactly one patent-news-monitor-key.pem under Downloads; found $($KeyCandidates.Count)."
}
$SourceKey = $KeyCandidates[0].FullName
$TemporaryKey = Join-Path ([System.IO.Path]::GetTempPath()) (
    "gdelt-typesafe-" + [guid]::NewGuid().ToString("N") + ".pem"
)
$Helper = Join-Path $PSScriptRoot "configure_typesafe_api_key_remote.py"
$RemoteHelper = "/tmp/configure_typesafe_api_key_remote.py"
$Pointer = [IntPtr]::Zero
$PlainApiKey = $null
$Payload = $null
$SecureApiKey = $null

try {
    if (-not (Test-Path -LiteralPath $SourceKey -PathType Leaf)) {
        throw "SSH key was not found: $SourceKey"
    }
    if (-not (Test-Path -LiteralPath $Helper -PathType Leaf)) {
        throw "TypeSafe setup helper was not found: $Helper"
    }

    Copy-Item -LiteralPath $SourceKey -Destination $TemporaryKey
    $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $TemporaryKey /inheritance:r | Out-Null
    & icacls.exe $TemporaryKey /grant:r "${Identity}:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to secure the temporary SSH key."
    }

    Write-Host "Checking the VPS connection..."
    & ssh.exe -o BatchMode=yes -o ConnectTimeout=15 -i $TemporaryKey $Server "true"
    if ($LASTEXITCODE -ne 0) {
        throw "VPS connection failed. The TypeSafe API key was not requested or transmitted."
    }

    $SecureApiKey = Read-Host "TypeSafe API key (hidden)" -AsSecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureApiKey)
    $PlainApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    $Payload = @{ typesafe_api_key = $PlainApiKey } | ConvertTo-Json -Compress

    Write-Host "Uploading the temporary setup helper to the VPS..."
    & scp.exe -o BatchMode=yes -i $TemporaryKey $Helper "${Server}:$RemoteHelper"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload the TypeSafe setup helper to the VPS."
    }

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = "ssh.exe"
    $RemoteCommand = "sudo -n python3 $RemoteHelper; result=`$?; rm -f $RemoteHelper; exit `$result"
    $StartInfo.Arguments = '-o BatchMode=yes -i "{0}" {1} "{2}"' -f $TemporaryKey, $Server, $RemoteCommand
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    [void]$Process.Start()
    $Process.StandardInput.Write($Payload)
    $Process.StandardInput.Close()
    $Output = $Process.StandardOutput.ReadToEnd()
    $ErrorOutput = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()

    if ($Process.ExitCode -ne 0) {
        if ($ErrorOutput) { Write-Host $ErrorOutput -ForegroundColor Red }
        throw "TypeSafe API test or VPS configuration failed. The key was not saved."
    }

    Write-Host $Output
    Write-Host "TypeSafe API key setup is complete." -ForegroundColor Green
}
finally {
    if ($Pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
    $PlainApiKey = $null
    $Payload = $null
    $SecureApiKey = $null
    if (Test-Path -LiteralPath $TemporaryKey) {
        Remove-Item -LiteralPath $TemporaryKey -Force
    }
}
