# Simulated tests for the Friend Edition data-load logic (development only; included in TEST builds, not releases).
#   powershell -NoProfile -ExecutionPolicy Bypass -File test-dataload.ps1
# Isolated: LOCALAPPDATA points to a new temporary folder, so no real installation, database or network is used.
# The real `discover` is replaced by a fake process that waits, writes a 2 MB file, prints a result and exits with
# a chosen code (0 complete, 4 partial, 5 lock held, 1 error, -1 crash).
$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('leadintel-dataload-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$env:LOCALAPPDATA = $testRoot
. (Join-Path $PSScriptRoot 'common.ps1')
$ErrorActionPreference = 'Stop'
$script:fails = 0; $script:passes = 0
function Assert([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { $script:passes++; Write-Host "PASS  $name" -ForegroundColor Green }
    else { $script:fails++; Write-Host "FAIL  $name  $detail" -ForegroundColor Red }
}
New-Item -ItemType Directory -Path $LI.Data, $LI.Logs -Force | Out-Null
Write-Host "Test folder: $testRoot`n"

# Data from an "earlier load": must survive every outcome and must not count as downloaded by a new run.
$old = Join-Path $LI.Data 'overture\earlier.parquet'
New-Item -ItemType Directory -Path (Split-Path $old) -Force | Out-Null
[IO.File]::WriteAllBytes($old, (New-Object byte[] (3MB)))
(Get-Item $old).LastWriteTime = (Get-Date).AddDays(-2)
$oldHash = (Get-FileHash $old).Hash

# ---- pure functions ----
Assert 'step: no run recorded yet' ((Format-LoadStep '' @()) -like 'Preparing:*')
Assert 'step: run started, no source finished -> Overture' ((Format-LoadStep 'running' @()) -eq 'Current step: Overture business listings')
Assert 'step: Overture finished -> Sunbiz' ((Format-LoadStep 'running' @('overture')) -eq 'Current step: Sunbiz registrations (finished: Overture business listings)')
Assert 'step: Overture+Sunbiz finished -> DBPR' ((Format-LoadStep 'running' @('overture', 'sunbiz')) -like 'Current step: DBPR licence evidence*')
Assert 'step: all sources finished -> matching and scoring' ((Format-LoadStep 'running' @('dbpr', 'overture', 'sunbiz')) -like 'Current step: Matching duplicates and scoring*')
Assert 'step: run no longer running -> finishing' ((Format-LoadStep 'completed' @('overture')) -like 'Finishing*')
Assert 'outcome codes 0/4/5/1/6/-1' ((@(0, 4, 5, 1, 6, -1) | ForEach-Object { Get-LoadOutcome $_ }) -join ',' -eq 'complete,warnings,busy,failed,failed,failed')
Assert 'files written: earlier files not counted' ((Get-WrittenMB $LI.Data (Get-Date).AddMinutes(-1)) -eq 0)

$s = @{}
Update-LoadState $s 'failed';   Assert 'state: first load failed -> failed' ($s['initial_load'] -eq 'failed')
Update-LoadState $s 'busy';     Assert 'state: busy changes nothing' ($s['initial_load'] -eq 'failed' -and $s['last_load_outcome'] -eq 'failed')
Update-LoadState $s 'warnings'; Assert 'state: partial -> complete_with_warnings (not repeated by Setup)' ($s['initial_load'] -eq 'complete_with_warnings')
Update-LoadState $s 'failed';   Assert 'state: later failure keeps complete_with_warnings' ($s['initial_load'] -eq 'complete_with_warnings')
Update-LoadState $s 'complete'; Assert 'state: success -> complete' ($s['initial_load'] -eq 'complete')
Update-LoadState $s 'failed';   Assert 'state: later failure keeps complete' ($s['initial_load'] -eq 'complete' -and $s['last_load_outcome'] -eq 'failed')

# ---- automatic background refresh rules (launcher) ----
$now = Get-Date
function St([string]$initial, [string]$outcome, $hoursAgo) {
    $h = @{}
    if ($initial) { $h['initial_load'] = $initial }
    if ($outcome) { $h['last_load_outcome'] = $outcome }
    if ($null -ne $hoursAgo) { $h['last_load_at'] = $now.AddHours(-$hoursAgo).ToString('o') }
    return $h
}
Assert 'refresh: never loaded -> load' ($null -ne (Get-DataRefreshReason (St '' '' $null) $now))
Assert 'refresh: first load failed 1h ago -> wait' ($null -eq (Get-DataRefreshReason (St 'failed' 'failed' 1) $now))
Assert 'refresh: first load failed 4h ago -> retry' ((Get-DataRefreshReason (St 'failed' 'failed' 4) $now) -like '*first data load*')
Assert 'refresh: incomplete 1h ago -> wait' ($null -eq (Get-DataRefreshReason (St 'complete_with_warnings' 'warnings' 1) $now))
Assert 'refresh: incomplete 4h ago -> retry' ((Get-DataRefreshReason (St 'complete_with_warnings' 'warnings' 4) $now) -like '*incomplete*')
Assert 'refresh: later failure 5h ago after a full load -> retry' ((Get-DataRefreshReason (St 'complete' 'failed' 5) $now) -like '*incomplete*')
Assert 'refresh: complete 2 days ago -> nothing' ($null -eq (Get-DataRefreshReason (St 'complete' 'complete' 48) $now))
Assert 'refresh: complete 8 days ago -> weekly refresh' ((Get-DataRefreshReason (St 'complete' 'complete' 192) $now) -like '*week old*')
$dt = St 'complete' 'complete' $null; $dt['last_load_at'] = $now.AddDays(-8)
Assert 'refresh: a date already parsed by ConvertFrom-Json also works' ((Get-DataRefreshReason $dt $now) -like '*week old*')

# ---- Sunbiz login: built-in public login unless one was saved ----
$login = Get-SunbizLogin
Assert 'sunbiz: built-in public login is used when none is saved' ($login -and $login.User -eq 'Public' -and $login.Source -eq 'built-in public login')
Save-PlainSecret 'sunbiz_user' 'OtherUser'; Save-PlainSecret 'sunbiz_password' 'OtherPass'
$login = Get-SunbizLogin
Assert 'sunbiz: a saved login overrides the built-in one' ($login.User -eq 'OtherUser' -and $login.Source -eq 'saved')

# ---- app window command line (no window is opened) ----
$ea = Get-EdgeArguments 'http://127.0.0.1:8765/'
Assert 'edge: app mode, own profile, maximized by default' (($ea -contains '--app=http://127.0.0.1:8765/') -and
    (($ea | Where-Object { $_ -like '--user-data-dir=*edge-profile*' }).Count -eq 1) -and ($ea -contains '--start-maximized') -and
    -not ($ea -contains '--start-fullscreen'))
$ef = Get-EdgeArguments 'http://127.0.0.1:8765/' -Fullscreen
Assert 'edge: -Fullscreen asks for full screen instead of maximized' (($ef -contains '--start-fullscreen') -and -not ($ef -contains '--start-maximized'))

# ---- simulated discover runs through the real Invoke-DataLoad / Show-DataLoadResult ----
$hostExe = (Get-Process -Id $PID).Path
$partialJson = '{"run_id": 2, "status": "partial", "counts": {}, "coverage": [' +
    '{"source": "overture", "area": "duval", "state": "complete", "reason": null, "records": 10},' +
    '{"source": "sunbiz", "area": "duval", "state": "not_run", "reason": "SUNBIZ_SFTP_USER / SUNBIZ_SFTP_PASSWORD not set", "records": null}]}'
function New-Fake([int]$code, [string]$stdout, [string]$stderr) {
    $f = Join-Path $testRoot ("fake-discover-$code.ps1")
    $body = @"
Start-Sleep -Seconds 3
`$d = Join-Path '$($LI.Data)' 'sunbiz'; New-Item -ItemType Directory -Path `$d -Force | Out-Null
[IO.File]::WriteAllBytes((Join-Path `$d ('new-' + [guid]::NewGuid().ToString('N') + '.dat')), (New-Object byte[] (2MB)))
Start-Sleep -Seconds 2
[Console]::Out.WriteLine(@'
$stdout
'@)
[Console]::Error.WriteLine('$stderr')
if ($code -eq -1) { [Environment]::Exit(-1) }
exit $code
"@
    [IO.File]::WriteAllText($f, $body)
    return @($hostExe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $f)
}
$cases = @(
    @{ Code = 0;  Out = '{"run_id": 1, "status": "completed", "coverage": [], "counts": {}}'; Err = ''; Want = 'complete' },
    @{ Code = 4;  Out = $partialJson; Err = ''; Want = 'warnings' },
    @{ Code = 5;  Out = ''; Err = 'leadintel: another LeadIntel operation holds the maintenance lock'; Want = 'busy' },
    @{ Code = 1;  Out = ''; Err = 'Traceback (most recent call last): ConnectionError: simulated network failure'; Want = 'failed' },
    @{ Code = -1; Out = ''; Err = 'simulated crash'; Want = 'failed' })
foreach ($c in $cases) {
    Write-Host "`n-- simulated discover exiting with $($c.Code)"
    $res = Invoke-DataLoad @{} (New-Fake $c.Code $c.Out $c.Err) 1
    $outcome = Show-DataLoadResult @{} $res
    Assert "exit $($c.Code) -> $($c.Want)" ($outcome -eq $c.Want) "got $outcome"
    Assert "exit $($c.Code): reported MB counts only this run's 2 MB" ($res.WrittenMB -ge 1.9 -and $res.WrittenMB -le 2.2) "got $($res.WrittenMB)"
    Assert "exit $($c.Code): output saved to the load log" ((Test-Path $res.Log) -and (Test-Path $res.ErrorLog))
    Assert "exit $($c.Code): earlier data untouched" ((Test-Path $old) -and (Get-FileHash $old).Hash -eq $oldHash)
}

Write-Host "`n$($script:passes) passed, $($script:fails) failed. Test folder left for inspection: $testRoot"
exit ([int]($script:fails -gt 0))
