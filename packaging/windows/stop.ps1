# LeadIntel Friend Edition - stop the LeadIntel server and its database.
param([switch]$Force)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    $null = Assert-Installed
    $procs = @(Get-LeadIntelProcesses)
    $servers = @($procs | Where-Object { $_.CommandLine -match 'uvicorn' })
    $others = @($procs | Where-Object { $_.CommandLine -notmatch 'uvicorn' })
    foreach ($p in $servers) { Stop-Process -Id $p.ProcessId -ErrorAction SilentlyContinue; Write-Host "Stopped the LeadIntel server (process $($p.ProcessId))." }
    if ($servers.Count -eq 0) { Write-Host 'The LeadIntel server was not running.' }
    Remove-Item -LiteralPath $LI.Run -Force -ErrorAction SilentlyContinue
    if ($others.Count -gt 0 -and -not $Force) {
        Write-Host "Load Data or Export is still working ($($others.Count) process(es)); the database was left running so it can finish." -ForegroundColor Yellow
        Write-Host 'Run Stop LeadIntel again when it has finished.'
    } else {
        Start-Sleep -Seconds 1
        if (Test-PgRunning) { Stop-Pg; Write-Host 'Stopped the LeadIntel database.' } else { Write-Host 'The LeadIntel database was not running.' }
    }
} catch {
    if ($_.Exception.Message -notlike 'LeadIntel:*') { Write-Host $_.Exception.Message -ForegroundColor Red }
}
Start-Sleep -Seconds 3
