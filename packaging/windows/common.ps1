# Shared helpers for the LeadIntel Friend Edition scripts. Windows PowerShell 5.1 or later.
#
# Everything lives under %LOCALAPPDATA%\LeadIntel for the current Windows user only:
#   app\       LeadIntel code (replaced by updates)       python\  private Python (replaced if its version changes)
#   venv\      Python packages (rebuilt if the lock changes) pgsql\   PostgreSQL + PostGIS programs
#   pgdata\    this installation's database  -- never replaced or deleted by Setup or updates
#   data\      downloaded source files       -- never replaced or deleted by Setup or updates
#   secrets\   generated passwords, encrypted with Windows DPAPI for this Windows user only
#   backups\   database backups taken before upgrades     logs\    setup and database logs
# No administrator rights, no Windows services, no system settings are used or changed.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version 2

$LIScriptDir = $PSScriptRoot      # folder of these scripts (captured once; used for files shipped next to them)
$LIRoot = Join-Path $env:LOCALAPPDATA 'LeadIntel'
$LI = @{
    Root      = $LIRoot
    App       = Join-Path $LIRoot 'app'
    Python    = Join-Path $LIRoot 'python'
    Venv      = Join-Path $LIRoot 'venv'
    Pg        = Join-Path $LIRoot 'pgsql'
    PgData    = Join-Path $LIRoot 'pgdata'
    Data      = Join-Path $LIRoot 'data'
    Secrets   = Join-Path $LIRoot 'secrets'
    Backups   = Join-Path $LIRoot 'backups'
    Logs      = Join-Path $LIRoot 'logs'
    Downloads = Join-Path $LIRoot 'downloads'
    State     = Join-Path $LIRoot 'leadintel.json'
    Run       = Join-Path $LIRoot 'run.json'
}
$PgSuperUser = 'li_super'
$PgAppUser = 'leadintel'
$PgDatabase = 'leadintel'

function Write-Step([string]$msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Write-Note([string]$msg) { Write-Host "   $msg" }
function Stop-WithError([string]$msg) {
    Write-Host "`nLeadIntel: $msg" -ForegroundColor Red
    throw "LeadIntel: $msg"
}

# ---------- state file (installation id, versions, ports) ----------
function Read-State {
    $h = @{}
    if (Test-Path $LI.State) {
        $o = Get-Content -Raw -LiteralPath $LI.State | ConvertFrom-Json
        foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = $p.Value }
    }
    return $h
}
function Save-State([hashtable]$state) {
    $state['updated_at'] = (Get-Date).ToString('o')
    $json = New-Object PSObject -Property $state | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($LI.State, $json, (New-Object Text.UTF8Encoding $false))
}
function Get-StateValue([hashtable]$state, [string]$key, $default = $null) {
    if ($state.ContainsKey($key) -and $null -ne $state[$key] -and "$($state[$key])" -ne '') { return $state[$key] }
    return $default
}

# ---------- secrets: Windows DPAPI, current user only ----------
function New-RandomSecret {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}
function Save-Secret([string]$name, [Security.SecureString]$value) {
    New-Item -ItemType Directory -Path $LI.Secrets -Force | Out-Null
    # ConvertFrom-SecureString without a key = DPAPI, readable only by this Windows user on this computer.
    $enc = ConvertFrom-SecureString -SecureString $value
    [IO.File]::WriteAllText((Join-Path $LI.Secrets "$name.dpapi"), $enc, (New-Object Text.UTF8Encoding $false))
}
function Save-PlainSecret([string]$name, [string]$plain) {
    $s = New-Object Security.SecureString
    foreach ($c in $plain.ToCharArray()) { $s.AppendChar($c) }
    $s.MakeReadOnly()
    Save-Secret $name $s
}
function Test-Secret([string]$name) { return Test-Path (Join-Path $LI.Secrets "$name.dpapi") }
function Get-Secret([string]$name) {
    $f = Join-Path $LI.Secrets "$name.dpapi"
    if (-not (Test-Path $f)) { return $null }
    $sec = ConvertTo-SecureString -String ([IO.File]::ReadAllText($f).Trim())
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

# ---------- native programs ----------
function Invoke-Native([string]$exe, [string[]]$argList, [string]$stdin = $null) {
    # Windows PowerShell treats native stderr as errors under 'Stop'; run with 'Continue' and use the exit code.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($null -ne $stdin) { $stdin | & $exe @argList 2>&1 | ForEach-Object { Write-Host "   $_" } }
        else { & $exe @argList 2>&1 | ForEach-Object { Write-Host "   $_" } }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}
function Get-PgExe([string]$name) { return Join-Path $LI.Pg "bin\$name.exe" }
function Get-PyExe { return Join-Path $LI.Venv 'Scripts\python.exe' }

# ---------- downloads ----------
function Get-VerifiedDownload($item, [string]$label) {
    if (-not $item.url -or -not $item.sha256) { Stop-WithError "$label is not pinned in downloads.json; this package is incomplete." }
    New-Item -ItemType Directory -Path $LI.Downloads -Force | Out-Null
    $file = Join-Path $LI.Downloads ([IO.Path]::GetFileName(([Uri]$item.url).AbsolutePath))
    if ((Test-Path $file) -and (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $item.sha256.ToUpper()) {
        Write-Note "$label already downloaded and verified."
        return $file
    }
    $mb = [math]::Round($item.size / 1MB)
    Write-Note "Downloading $label (about $mb MB) from $(([Uri]$item.url).Host) ..."
    $part = "$file.part"
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    if (Test-Path $curl) {
        $code = Invoke-Native $curl @('-L', '--fail', '--retry', '3', '--connect-timeout', '30', '-sS', '-o', $part, $item.url)
        if ($code -ne 0) { Stop-WithError "download of $label failed (curl exit code $code). Check the internet connection and run Setup again." }
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $item.url -OutFile $part -UseBasicParsing
    }
    $hash = (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash
    if ($hash -ne $item.sha256.ToUpper()) {
        Remove-Item -LiteralPath $part -Force
        Stop-WithError "$label failed its integrity check (SHA-256 $hash). Nothing was installed from it."
    }
    Move-Item -LiteralPath $part -Destination $file -Force
    Write-Note "$label verified (SHA-256 $($hash.Substring(0, 16))...)."
    return $file
}

function Expand-ZipSubfolders([string]$zip, [string]$dest, [string[]]$folders, [string[]]$skip = @()) {
    # Extracts <top>/<folder>/... entries (one top-level folder in the archive) into $dest\<folder>\...
    # $skip: path prefixes relative to <top> (e.g. 'bin/postgisgui/') that are left out. Existing files are
    # overwritten, as the PostGIS bundle's own instructions require.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destFull = [IO.Path]::GetFullPath($dest).TrimEnd('\') + '\'
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    $n = 0
    try {
        foreach ($e in $z.Entries) {
            $parts = $e.FullName -split '[\\/]'
            if ($parts.Count -lt 3 -or $folders -notcontains $parts[1]) { continue }
            $relSlash = ($parts[1..($parts.Count - 1)] -join '/')
            $skipped = $false
            foreach ($s in $skip) { if ($relSlash.StartsWith($s, [StringComparison]::OrdinalIgnoreCase)) { $skipped = $true; break } }
            if ($skipped) { continue }
            $rel = ($parts[1..($parts.Count - 1)] -join '\')
            $full = [IO.Path]::GetFullPath((Join-Path $dest $rel))
            if (-not $full.StartsWith($destFull, [StringComparison]::OrdinalIgnoreCase)) { Stop-WithError "unsafe path in archive: $($e.FullName)" }
            if ($e.FullName.EndsWith('/') -or $e.FullName.EndsWith('\')) { New-Item -ItemType Directory -Path $full -Force | Out-Null; continue }
            New-Item -ItemType Directory -Path (Split-Path $full) -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $full, $true)
            $n++
        }
    } finally { $z.Dispose() }
    if ($n -eq 0) { Stop-WithError "nothing was extracted from $zip (unexpected archive layout)." }
    return $n
}

# ---------- PostgreSQL (private instance, started as a normal user process) ----------
function Test-PgRunning {
    if (-not (Test-Path (Join-Path $LI.PgData 'PG_VERSION'))) { return $false }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & (Get-PgExe 'pg_ctl') status -D $LI.PgData *> $null; return ($LASTEXITCODE -eq 0) }
    finally { $ErrorActionPreference = $old }
}
function Start-Pg {
    if (Test-PgRunning) { return $false }
    New-Item -ItemType Directory -Path $LI.Logs -Force | Out-Null
    # Started in its own hidden console, so closing any LeadIntel window does not stop the database other
    # windows use, and no output pipe is held open by the server. WaitForExit() waits for pg_ctl only
    # (Start-Process -Wait would also wait for the server itself). Stop LeadIntel or the last window stops it.
    $log = Join-Path $LI.Logs 'postgres.log'
    $p = Start-Process -FilePath (Get-PgExe 'pg_ctl') -WindowStyle Hidden -PassThru `
         -ArgumentList @('start', '-D', "`"$($LI.PgData)`"", '-l', "`"$log`"", '-w', '-t', '90')
    $null = $p.Handle
    $p.WaitForExit()
    if ($p.ExitCode -ne 0 -or -not (Test-PgRunning)) { Stop-WithError "the LeadIntel database did not start. See $log" }
    return $true
}
function Stop-Pg {
    if (-not (Test-PgRunning)) { return }
    $code = Invoke-Native (Get-PgExe 'pg_ctl') @('stop', '-D', $LI.PgData, '-m', 'fast', '-w', '-t', '60')
    if ($code -ne 0) { Write-Host '   The database did not stop cleanly; it will recover on the next start.' -ForegroundColor Yellow }
}
function Get-LeadIntelProcesses {
    # Python processes running from this installation's private environment (server, Load Data, Export).
    $venv = [IO.Path]::GetFullPath($LI.Venv)
    return @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
             Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) })
}
function Stop-PgIfIdle {
    if (@(Get-LeadIntelProcesses).Count -eq 0) { Stop-Pg }
}
function Test-PortFree([int]$port) {
    $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $port)
    try { $l.Start(); return $true } catch { return $false } finally { try { $l.Stop() } catch { } }
}
function Find-FreePort([int]$from, [int]$to) {
    for ($p = $from; $p -le $to; $p++) { if (Test-PortFree $p) { return $p } }
    Stop-WithError "no free local port between $from and $to."
}

# ---------- environment for LeadIntel commands (this process and its children only) ----------
function Set-AppEnvironment([hashtable]$state, [switch]$ReadOnly) {
    $pw = Get-Secret 'db_app'
    if (-not $pw) { Stop-WithError 'the database credentials are missing. Run Setup again.' }
    $env:PGPASSWORD = $pw
    $env:LEADINTEL_DATABASE_URL = "postgresql+psycopg://$PgAppUser@127.0.0.1:$($state['pg_port'])/$PgDatabase"
    $env:LEADINTEL_DATA_DIR = $LI.Data
    $env:LEADINTEL_CONFIG_DIR = Join-Path $LI.App 'config'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUTF8 = '1'
    if ($ReadOnly) { $env:PGOPTIONS = '-c default_transaction_read_only=on' } else { Remove-Item Env:PGOPTIONS -ErrorAction SilentlyContinue }
    $login = Get-SunbizLogin
    if ($login) { $env:SUNBIZ_SFTP_USER = $login.User; $env:SUNBIZ_SFTP_PASSWORD = $login.Password }
}
function Get-SunbizLogin {
    # A login saved with 'setup.ps1 -SunbizLogin' (DPAPI) wins; otherwise the public login that Florida publishes on
    # its Data Downloads page, shipped in sunbiz-public-login.json. Returns $null only if neither is available.
    $u = Get-Secret 'sunbiz_user'
    $p = Get-Secret 'sunbiz_password'
    if ($u -and $p) { return @{ User = $u; Password = $p; Source = 'saved' } }
    $f = Join-Path $LIScriptDir 'sunbiz-public-login.json'
    if (Test-Path $f) {
        $j = Get-Content -Raw -LiteralPath $f | ConvertFrom-Json
        if ($j.user -and $j.password) { return @{ User = "$($j.user)"; Password = "$($j.password)"; Source = 'built-in public login' } }
    }
    return $null
}
function Clear-AppEnvironment {
    foreach ($n in 'PGPASSWORD', 'PGOPTIONS', 'SUNBIZ_SFTP_USER', 'SUNBIZ_SFTP_PASSWORD', 'LEADINTEL_DATABASE_URL') {
        Remove-Item "Env:$n" -ErrorAction SilentlyContinue
    }
}
function Invoke-LeadIntel([string[]]$cliArgs) {
    $runner = Join-Path $LI.App 'packaging\windows\run_cli.py'
    return Invoke-Native (Get-PyExe) (@($runner) + $cliArgs)
}
function Assert-Installed {
    $state = Read-State
    if (-not (Get-StateValue $state 'db_ready' $false)) {
        Stop-WithError 'LeadIntel is not set up yet (or setup did not finish). Run Setup.cmd first.'
    }
    return $state
}

# ---------- data loading (existing `discover` command: downloads, imports, matching and scoring) ----------
# Safety comes from discover itself: it holds the maintenance lock (a second load exits with code 5 and changes
# nothing), commits each source separately (a failed source is rolled back alone), never deletes prospects, and
# replaces scores in one transaction. These functions only start it, report progress and record the outcome.
$LoadSteps = [ordered]@{ overture = 'Overture business listings'; sunbiz = 'Sunbiz registrations'; dbpr = 'DBPR licence evidence' }

function Format-LoadStep([string]$runStatus, [string[]]$finished) {
    # Pure function (tested). discover records a run only after settings and county boundaries are ready, then
    # runs Overture, Sunbiz and DBPR in that fixed order, writing each source's coverage row when that source
    # finishes, then matches duplicates and scores. So the step in progress is the first source without a row.
    if (-not $runStatus) { return 'Preparing: settings and county boundaries (no run recorded yet)' }
    if ($runStatus -ne 'running') { return "Finishing (run recorded as $runStatus)" }
    $done = @(); $current = 'Matching duplicates and scoring prospects'
    foreach ($k in $LoadSteps.Keys) {
        if (@($finished) -contains $k) { $done += $LoadSteps[$k] } else { $current = $LoadSteps[$k]; break }
    }
    $text = "Current step: $current"
    if ($done.Count -gt 0) { $text += " (finished: $($done -join ', '))" }
    return $text
}

function Get-WrittenMB([string]$folder, [datetime]$since) {
    # Size of files created or changed in $folder since $since: what this run downloaded or wrote, including
    # downloads still in progress. Files cached by earlier runs are not counted.
    if (-not (Test-Path $folder)) { return 0 }
    # Plain loop: in Windows PowerShell 5.1, Measure-Object returns nothing for an empty input, and .Sum on
    # nothing fails under strict mode.
    [long]$bytes = 0
    foreach ($f in @(Get-ChildItem -LiteralPath $folder -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        if ($f.LastWriteTime -ge $since) { $bytes += $f.Length }
    }
    return [math]::Round($bytes / 1MB, 1)
}

function Invoke-ReadOnlySql([hashtable]$state, [string]$sql) {
    # One read-only query as the app account; returns its output lines, or $null if the database cannot answer.
    $psql = Get-PgExe 'psql'
    if (-not (Test-Path $psql) -or -not (Get-StateValue $state 'pg_port')) { return $null }
    $saved = @{ PGOPTIONS = $env:PGOPTIONS; PGPASSWORD = $env:PGPASSWORD }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $env:PGOPTIONS = '-c default_transaction_read_only=on'
        $env:PGPASSWORD = Get-Secret 'db_app'
        $lines = @(& $psql -X -q -At -h 127.0.0.1 -p "$($state['pg_port'])" -U $PgAppUser -d $PgDatabase -c $sql 2>$null)
        if ($LASTEXITCODE -ne 0) { return $null }
        return ,$lines
    } catch { return $null }
    finally {
        $ErrorActionPreference = $old
        foreach ($k in $saved.Keys) { if ($null -eq $saved[$k]) { Remove-Item "Env:$k" -ErrorAction SilentlyContinue } else { Set-Item "Env:$k" $saved[$k] } }
    }
}

function Get-LoadStatus([hashtable]$state, [long]$baseRunId) {
    # Run status and finished sources of the discovery run started after $baseRunId (read-only).
    $sql = "SELECT coalesce((SELECT status FROM discovery_run WHERE id > $baseRunId ORDER BY id DESC LIMIT 1), '') || '#' || " +
           "coalesce((SELECT string_agg(DISTINCT source_key, ',') FROM coverage WHERE run_id = " +
           "(SELECT max(id) FROM discovery_run WHERE id > $baseRunId)), '')"
    $lines = Invoke-ReadOnlySql $state $sql
    if ($null -eq $lines) { return $null }
    $line = @($lines | Where-Object { $_ -match '#' }) | Select-Object -First 1
    if (-not $line) { return $null }
    $parts = $line -split '#', 2
    return @{ Status = $parts[0]; Finished = @($parts[1] -split ',' | Where-Object { $_ }) }
}

function Format-ProcessArgs([string[]]$argList) {
    return @($argList | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } })
}

function Invoke-DataLoad([hashtable]$state, [string[]]$command = $null, [int]$pollSeconds = 10) {
    # Runs discover with live progress. Output goes to logs\load-data-<time>.log (+ .errors.log).
    # $command replaces the real discover for simulated tests. Returns @{ Code; Log; ErrorLog; Elapsed }.
    New-Item -ItemType Directory -Path $LI.Logs -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $log = Join-Path $LI.Logs "load-data-$stamp.log"
    $errLog = Join-Path $LI.Logs "load-data-$stamp.errors.log"
    if (-not $command) { $command = @((Get-PyExe), (Join-Path $LI.App 'packaging\windows\run_cli.py'), 'discover') }
    $base = 0
    $b = Invoke-ReadOnlySql $state 'SELECT coalesce(max(id), 0) FROM discovery_run'
    if ($null -ne $b) { $first = @($b | Where-Object { $_ -match '^\d+$' }) | Select-Object -First 1; if ($first) { $base = [long]$first } }
    $start = Get-Date
    $p = Start-Process -FilePath $command[0] -ArgumentList (Format-ProcessArgs @($command | Select-Object -Skip 1)) -NoNewWindow -PassThru `
         -RedirectStandardOutput $log -RedirectStandardError $errLog
    $null = $p.Handle
    $lastStep = $null; $lastPrint = [datetime]::MinValue
    while (-not $p.WaitForExit($pollSeconds * 1000)) {
        $st = Get-LoadStatus $state $base
        $step = if ($null -eq $st) { 'Working (step details unavailable)' } else { Format-LoadStep $st.Status $st.Finished }
        $now = Get-Date
        if ($step -ne $lastStep -or ($now - $lastPrint).TotalSeconds -ge 60) {
            Write-Host ("   [{0:hh\:mm\:ss}] {1} - files downloaded/written this run: {2:N1} MB" -f ($now - $start), $step, (Get-WrittenMB $LI.Data $start))
            $lastStep = $step; $lastPrint = $now
        }
    }
    $p.WaitForExit()
    return @{ Code = $p.ExitCode; Log = $log; ErrorLog = $errLog; Elapsed = (Get-Date) - $start; WrittenMB = (Get-WrittenMB $LI.Data $start) }
}

function Get-LoadOutcome([int]$code) {
    # Pure function (tested): discover exit codes -> outcome.
    switch ($code) { 0 { 'complete' } 4 { 'warnings' } 5 { 'busy' } default { 'failed' } }
}

function Show-DataLoadResult([hashtable]$state, [hashtable]$res) {
    $outcome = Get-LoadOutcome $res.Code
    Write-Host ("   Finished after {0:hh\:mm\:ss}; {1:N1} MB downloaded/written." -f $res.Elapsed, $res.WrittenMB)
    $report = $null
    try { $report = Get-Content -Raw -LiteralPath $res.Log -ErrorAction Stop | ConvertFrom-Json } catch { }
    if ($outcome -eq 'warnings' -and $report) {
        Write-Host '   Loaded, but some sources were incomplete:' -ForegroundColor Yellow
        $rows = if ($report.PSObject.Properties['coverage']) { @($report.coverage) } else { @() }
        foreach ($c in $rows) {
            if ($c.PSObject.Properties['state'] -and $c.state -ne 'complete') {
                $reason = if ($c.PSObject.Properties['reason'] -and $c.reason) { "$($c.reason)" } else { '' }
                $why = if ($reason) { " - $($reason.Substring(0, [math]::Min(140, $reason.Length)))" } else { '' }
                Write-Host "     $($c.source) / $($c.area): $($c.state)$why" -ForegroundColor Yellow
            }
        }
        Write-Host '   Everything that did load is saved. Run Load Data later to try the missing parts again.'
    } elseif ($outcome -eq 'busy') {
        Write-Host '   Another LeadIntel data load is already running. It was left alone; this one changed nothing.' -ForegroundColor Yellow
        Write-Host '   Wait for it to finish (or run Load Data later).'
    } elseif ($outcome -eq 'failed') {
        Write-Host "   The data load did not finish (exit code $($res.Code))." -ForegroundColor Red
        if (Test-Path $res.ErrorLog) { Get-Content -LiteralPath $res.ErrorLog -Tail 12 | ForEach-Object { Write-Host "     $_" } }
        Write-Host '   Prospects and files from earlier loads are kept; only the unfinished step was rolled back.'
        Write-Host '   Check the internet connection, then run Load Data (Start menu > LeadIntel) to try again.'
    }
    if ($outcome -ne 'busy') {
        $n = Invoke-ReadOnlySql $state ("SELECT count(*) || '|' || count(*) FILTER (WHERE q.status IN ('provisionally_qualified','needs_research')) " +
             "FROM location l LEFT JOIN qualification q ON q.location_id = l.id AND q.superseded_at IS NULL WHERE l.merged_into_id IS NULL")
        $line = if ($null -ne $n) { @($n | Where-Object { $_ -match '^\d+\|\d+$' }) | Select-Object -First 1 } else { $null }
        if ($line) { $c = $line -split '\|'; Write-Host "   Businesses in your database: $($c[0]); prospect candidates: $($c[1])." -ForegroundColor Green }
    }
    Write-Host "   Details: $($res.Log)"
    return $outcome
}

function Update-LoadState([hashtable]$state, [string]$outcome) {
    # Pure state transition (tested). 'busy' records nothing; a failed retry never downgrades an earlier success.
    if ($outcome -eq 'busy') { return }
    $state['last_load_outcome'] = $outcome
    $state['last_load_at'] = (Get-Date).ToString('o')
    $prev = Get-StateValue $state 'initial_load'
    if ($outcome -eq 'complete') { $state['initial_load'] = 'complete' }
    elseif ($outcome -eq 'warnings') { if ($prev -ne 'complete') { $state['initial_load'] = 'complete_with_warnings' } }
    elseif ($prev -ne 'complete' -and $prev -ne 'complete_with_warnings') { $state['initial_load'] = 'failed' }
}

function Get-DataRefreshReason([hashtable]$state, [datetime]$now) {
    # Pure function (tested): why the launcher should start Load Data in the background, or $null.
    #   - the first load never succeeded (failed or interrupted)       -> retry if the last attempt is >= 3 hours old
    #   - the last load was incomplete or failed                        -> retry if it is >= 3 hours old
    #   - the data is a week old                                        -> refresh
    $initial = Get-StateValue $state 'initial_load'
    $outcome = Get-StateValue $state 'last_load_outcome'
    $at = Get-StateValue $state 'last_load_at'
    $last = $null
    if ($at -is [datetime]) { $last = $at }
    elseif ($at) { try { $last = [datetime]::Parse("$at", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) } catch { } }
    $age = if ($last) { $now - $last } else { $null }
    if (-not $initial -or $initial -eq 'failed') {
        if (-not $age -or $age.TotalHours -ge 3) { return 'the first data load has not finished yet' }
        return $null
    }
    if ($outcome -and $outcome -ne 'complete' -and $age -and $age.TotalHours -ge 3) { return 'the last update was incomplete' }
    if (-not $age -or $age.TotalDays -ge 7) { return 'the data is more than a week old' }
    return $null
}

# ---------- app window (Microsoft Edge app mode, with the default browser as fallback) ----------
function Find-Edge {
    # Edge ships with Windows 10/11; look it up the documented way (App Paths), then the usual install folders.
    foreach ($key in 'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
                     'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe') {
        try { $p = (Get-ItemProperty -LiteralPath $key -ErrorAction Stop).'(default)'; if ($p -and (Test-Path $p)) { return $p } } catch { }
    }
    $candidates = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if ($base) { $candidates += (Join-Path $base 'Microsoft\Edge\Application\msedge.exe') }
    }
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}
function Get-EdgeArguments([string]$url, [switch]$Fullscreen) {
    # Pure function (tested): the Edge command line for LeadIntel's app window.
    # Own profile folder: keeps LeadIntel's browser storage apart from the user's browsing, and makes Edge open a
    # separate app window instead of handing the request to an already-running Edge.
    $profileDir = Join-Path $LI.Root 'edge-profile'
    $a = @("--app=$url", "--user-data-dir=`"$profileDir`"", '--no-first-run', '--no-default-browser-check')
    if ($Fullscreen) { $a += '--start-fullscreen' } else { $a += '--start-maximized' }
    return ,$a
}
function Open-LeadIntelWindow([string]$url, [switch]$Fullscreen, [switch]$Browser) {
    # Opens LeadIntel as an app window. Returns 'edge-app' or 'browser'. Never sends keystrokes.
    if (-not $Browser) {
        $edge = Find-Edge
        if ($edge) {
            Start-Process -FilePath $edge -ArgumentList (Get-EdgeArguments $url -Fullscreen:$Fullscreen) | Out-Null
            for ($i = 0; $i -lt 30; $i++) {          # up to 15 seconds for the app window to appear
                Start-Sleep -Milliseconds 500
                $win = @(Get-Process msedge -ErrorAction SilentlyContinue |
                         Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like '*LeadIntel*' })
                if ($win.Count -gt 0) { return 'edge-app' }
            }
            Write-Host 'The LeadIntel app window did not appear; opening your default browser instead.' -ForegroundColor Yellow
        } else {
            Write-Host 'Microsoft Edge was not found; opening LeadIntel in your default browser.' -ForegroundColor Yellow
        }
    }
    Start-Process $url
    return 'browser'
}
