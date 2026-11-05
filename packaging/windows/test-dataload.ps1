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
