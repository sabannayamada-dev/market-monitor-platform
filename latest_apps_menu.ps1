$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Start-App([string]$relativePath) {
    $target = Join-Path $root $relativePath
    if (-not (Test-Path -LiteralPath $target)) {
        Write-Host "起動ファイルが見つかりません: $target" -ForegroundColor Red
        Read-Host "Enterキーで戻る"
        return
    }
    Start-Process -FilePath $target -WorkingDirectory $root
}

while ($true) {
    Clear-Host
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host "  最新版アプリ選択メニュー" -ForegroundColor Cyan
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host "  1. 株価と底検知の分析"
    Write-Host "  2. 企業情報の収集とスコアリング"
    Write-Host "  3. 米国株の大量底検知とSEC財務研究"
    Write-Host "  4. SEC財務データ結合"
    Write-Host "  5. 回帰分析と重み最適化"
    Write-Host "  6. 時価総額別の底検知モデル比較"
    Write-Host "  7. 特許の材料性モニター"
    Write-Host "  8. 企業ランキング・クラウド版フォルダ"
    Write-Host "  9. 終了"
    Write-Host "================================================" -ForegroundColor Cyan

    $choice = Read-Host "番号を入力してください"
    switch ($choice) {
        "1" { Start-App "start_stock_app_v5_7_0.bat" }
        "2" { Start-App "launch_company_scoring_tool.bat" }
        "3" { Start-App "launch_us_stock_research_tool.bat" }
        "4" { Start-App "launch_sec_financial_enrichment_tool.bat" }
        "5" { Start-App "launch_regularized_regression_tool.bat" }
        "6" { Start-App "launch_market_cap_model_analysis_tool.bat" }
        "7" { Start-App "launch_patent_monitor.bat" }
        "8" { Start-Process -FilePath (Join-Path $root "streamlit_cloud_app") }
        "9" { return }
        default {
            Write-Host "1から9の番号を入力してください。" -ForegroundColor Yellow
            Start-Sleep -Seconds 1
        }
    }
}

