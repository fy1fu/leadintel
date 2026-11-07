# LeadIntel Friend Edition - Load Data: update the prospects with fresh public business data.
# Runs LeadIntel's existing `discover` command (downloads, imports, duplicate matching and scoring) with progress.
# Safe to run any time and repeatedly: a second load at the same time exits without changing anything; an
# interrupted or failed load keeps everything loaded before. LeadIntel can stay open; refresh the page afterwards.
# -Auto: started by the launcher in a minimized window to retry or refresh; closes itself when done.
param([switch]$Auto)
. (Join-Path $PSScriptRoot 'common.ps1')
$exit = 1
try {
    $state = Assert-Installed
    Write-Host 'LeadIntel - Load Data' -ForegroundColor Green
    Write-Host 'Downloads public data (business listings, state registrations and licences, county boundaries) for the'
    Write-Host 'target counties, then matches and scores the prospects. A first load can take 10-60+ minutes and download'
    Write-Host 'several hundred MB; later updates reuse unchanged files and are usually much faster.'
    if (-not (Get-SunbizLogin)) {
        Write-Host 'Note: no Sunbiz login available, so new Sunbiz registrations are skipped (see README).' -ForegroundColor Yellow
    }
    Start-Pg | Out-Null
    Write-Step 'Loading data'
    Set-AppEnvironment $state
    try { $res = Invoke-DataLoad $state } finally { Clear-AppEnvironment }
    $outcome = Show-DataLoadResult $state $res
    Update-LoadState $state $outcome
    Save-State $state
    if ($outcome -eq 'complete' -or $outcome -eq 'warnings') {
        Write-Host "`nDone. Open (or refresh) LeadIntel to see the prospects." -ForegroundColor Green
        $exit = 0
    }
} catch {
    if ($_.Exception.Message -notlike 'LeadIntel:*') { Write-Host "`nLoad Data stopped: $($_.Exception.Message)" -ForegroundColor Red }
    Write-Host 'Prospects and files from earlier loads are kept. Run Load Data again to retry.'
} finally {
    Clear-AppEnvironment
    try { Stop-PgIfIdle } catch { }
}
if ($Auto) { Write-Host 'This window closes by itself in 30 seconds.'; Start-Sleep -Seconds 30 }
else { Write-Host 'Press Enter to close.'; [void](Read-Host) }
exit $exit
