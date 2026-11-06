# LeadIntel Friend Edition - start LeadIntel and open it in its own app window. Press Ctrl+C in this window to stop it.
#   -Fullscreen  open the app window full screen (--start-fullscreen; under test, see docs/DELIVERY_STATUS.md)
#   -Browser     use the default browser instead of an Edge app window
# If LeadIntel is already running, this only opens the browser at the running server: it starts no second
# server, keeps run.json, and leaves the database and the running server alone.
param([switch]$Fullscreen, [switch]$Browser)
. (Join-Path $PSScriptRoot 'common.ps1')

$server = $null
$secondLaunch = $false          # true when another LeadIntel window already serves; then nothing is cleaned up here
try {
    $state = Assert-Installed
    Write-Host 'LeadIntel' -ForegroundColor Green

    if (Test-Path $LI.Run) {
        $run = Get-Content -Raw -LiteralPath $LI.Run | ConvertFrom-Json
        $alive = $false
        try { $alive = [bool](Invoke-RestMethod -Uri "http://127.0.0.1:$($run.port)/api/health" -TimeoutSec 3).ok } catch { }
        if ($alive) {
            $secondLaunch = $true
            Write-Host "LeadIntel is already running; opening http://127.0.0.1:$($run.port)/"
            $null = Open-LeadIntelWindow "http://127.0.0.1:$($run.port)/" -Fullscreen:$Fullscreen -Browser:$Browser
            Start-Sleep -Seconds 3
            return
        }
        Remove-Item -LiteralPath $LI.Run -Force -ErrorAction SilentlyContinue      # stale: its server is gone
    }

    Write-Host 'Starting the database...'
    Start-Pg | Out-Null
    $port = Find-FreePort 8765 8785
    $url = "http://127.0.0.1:$port/"
    Set-AppEnvironment $state
    Write-Host "Starting LeadIntel on $url ..."
    $server = Start-Process -FilePath (Get-PyExe) -NoNewWindow -PassThru -WorkingDirectory (Join-Path $LI.App 'backend') -ArgumentList @(
        '-m', 'uvicorn', '--app-dir', "`"$(Join-Path $LI.App 'backend')`"", 'app.api.main:create_app', '--factory',
        '--host', '127.0.0.1', '--port', "$port", '--log-level', 'warning')
    $null = $server.Handle
    $ready = $false
    for ($i = 0; $i -lt 120 -and -not $server.HasExited; $i++) {
        Start-Sleep -Milliseconds 500
        try { if ((Invoke-RestMethod -Uri "${url}api/health" -TimeoutSec 2).ok) { $ready = $true; break } } catch { }
    }
    if (-not $ready) { Stop-WithError 'LeadIntel did not start within 60 seconds (see the messages above).' }
    $json = New-Object PSObject -Property @{ port = $port; pid = $server.Id; started_at = (Get-Date).ToString('o') } | ConvertTo-Json
    [IO.File]::WriteAllText($LI.Run, $json, (New-Object Text.UTF8Encoding $false))

    Write-Host "`nLeadIntel is running at $url" -ForegroundColor Green
    Write-Host 'Keep this window open while you use LeadIntel. Press Ctrl+C here to stop LeadIntel and its database.'
    $how = Open-LeadIntelWindow $url -Fullscreen:$Fullscreen -Browser:$Browser
    if ($how -eq 'edge-app') { Write-Host 'Opened in its own window. Full screen: F11 or the full-screen button; Esc or F11 to leave.' }
    # Retry an incomplete load, or refresh week-old data, without the salesperson having to do anything. Runs in its
    # own minimized window; the database lock makes a second load at the same time impossible.
    $why = Get-DataRefreshReason $state (Get-Date)
    $loading = @(Get-LeadIntelProcesses | Where-Object { $_.CommandLine -notmatch 'uvicorn' }).Count -gt 0
    if ($why -and -not $loading) {
        Write-Host "Updating the data in the background ($why). Refresh the page when it has finished." -ForegroundColor Cyan
        $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        Start-Process -FilePath $ps -WindowStyle Minimized -WorkingDirectory $LI.Root -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $LI.App 'packaging\windows\load-data.ps1')`"", '-Auto')
    }
    $server.WaitForExit()
} catch {
    if ($_.Exception.Message -notlike 'LeadIntel:*') { Write-Host "`nLeadIntel stopped: $($_.Exception.Message)" -ForegroundColor Red }
    Write-Host 'Press Enter to close.'; [void](Read-Host)
} finally {
    Clear-AppEnvironment
    if (-not $secondLaunch) {
        # Only the window that started the server cleans up: its own server, its own run.json, then the
        # database if no other LeadIntel process (another server, Load Data, Export) still uses it.
        if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -ErrorAction SilentlyContinue; $server.WaitForExit(10000) | Out-Null }
        if ($server -and (Test-Path $LI.Run)) {
            try { $mine = (Get-Content -Raw -LiteralPath $LI.Run | ConvertFrom-Json).pid -eq $server.Id } catch { $mine = $false }
            if ($mine) { Remove-Item -LiteralPath $LI.Run -Force -ErrorAction SilentlyContinue }
        }
        try { Stop-PgIfIdle } catch { }
    }
}
