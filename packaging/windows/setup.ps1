# LeadIntel Friend Edition - Setup (first install, repair, or update). Run through Setup.cmd.
#
# Installs everything for the current Windows user under %LOCALAPPDATA%\LeadIntel without administrator rights:
# private Python, Python packages, private PostgreSQL + PostGIS, a new database with its own generated passwords
# (encrypted with Windows DPAPI), the database schema and default settings, and Start-menu/Desktop shortcuts.
# Re-running Setup from a newer package updates the program; the database (pgdata) and data folder are never
# replaced or deleted, and the database is backed up before any schema upgrade.
param([switch]$SunbizLogin, [switch]$NoShortcuts, [switch]$NoStart)
. (Join-Path $PSScriptRoot 'common.ps1')

$Source = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))     # the package's app folder
$Manifest = Get-Content -Raw -LiteralPath (Join-Path $Source 'manifest.json') | ConvertFrom-Json
$Downloads = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'downloads.json') | ConvertFrom-Json
$Lock = Join-Path $PSScriptRoot 'requirements-win-py313.lock'

function Install-AppFiles([hashtable]$state) {
    Write-Step "Installing LeadIntel $($Manifest.version)"
    if ([IO.Path]::GetFullPath($LI.App).TrimEnd('\') -ieq $Source.TrimEnd('\')) {
        Write-Note 'Running from the installed copy; program files are left as they are.'
        return
    }
    if (@(Get-LeadIntelProcesses).Count -gt 0) {
        Stop-WithError 'LeadIntel is running. Close it (Stop LeadIntel) and run Setup again.'
    }
    $new = "$($LI.App).new"; $prev = "$($LI.App).previous"
    if (Test-Path $new) { Remove-Item -LiteralPath $new -Recurse -Force }
    Copy-Item -LiteralPath $Source -Destination $new -Recurse
    Get-ChildItem -LiteralPath $new -Recurse -File | Unblock-File
    if (Test-Path $LI.App) {
        if (Test-Path $prev) { Remove-Item -LiteralPath $prev -Recurse -Force }
        Rename-Item -LiteralPath $LI.App -NewName (Split-Path $prev -Leaf)
    }
    Rename-Item -LiteralPath $new -NewName (Split-Path $LI.App -Leaf)
    Write-Note "Program files: $($LI.App)"
}

function Install-Python([hashtable]$state) {
    Write-Step "Python $($Downloads.python.version)"
    $exe = Join-Path $LI.Python 'python.exe'
    if ((Test-Path $exe) -and (Get-StateValue $state 'python_version') -eq $Downloads.python.version) { Write-Note 'Already installed.'; return }
    $zip = Get-VerifiedDownload $Downloads.python 'Python'
    if (Test-Path $LI.Python) { Remove-Item -LiteralPath $LI.Python -Recurse -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $LI.Python)
    if (-not (Test-Path $exe)) { Stop-WithError 'Python was not extracted correctly.' }
    $state['python_version'] = $Downloads.python.version
    $state.Remove('lock_sha256')          # a new Python needs a new package environment
    Write-Note "Installed to $($LI.Python)"
}

function Install-Packages([hashtable]$state) {
    Write-Step 'Python packages'
    $lockHash = (Get-FileHash -LiteralPath $Lock -Algorithm SHA256).Hash
    if ((Test-Path (Get-PyExe)) -and (Get-StateValue $state 'lock_sha256') -eq $lockHash) { Write-Note 'Already installed.'; return }
    if (Test-Path $LI.Venv) { Remove-Item -LiteralPath $LI.Venv -Recurse -Force }
    $code = Invoke-Native (Join-Path $LI.Python 'python.exe') @('-m', 'venv', $LI.Venv)
    if ($code -ne 0) { Stop-WithError 'could not create the Python environment.' }
    Write-Note 'Downloading and installing about 60 MB of packages from PyPI (exact versions, each checked by SHA-256)...'
    $code = Invoke-Native (Get-PyExe) @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--quiet',
                                       '--only-binary=:all:', '--no-deps', '--require-hashes', '-r', $Lock)
    if ($code -ne 0) { Stop-WithError 'installing the Python packages failed (see above). Check the internet connection and run Setup again.' }
    $code = Invoke-Native (Get-PyExe) @('-m', 'pip', 'check', '--disable-pip-version-check')
    if ($code -ne 0) { Stop-WithError 'the installed Python packages are incomplete (see above).' }
    $state['lock_sha256'] = $lockHash
}

function Install-VcRuntime([string]$binDir) {
    # App-local Microsoft Visual C++ runtime for PostgreSQL (no administrator, no system change): copies of
    # Microsoft-signed DLLs that Setup already downloaded and verified (Python ZIP, hash-locked pyarrow package).
    Write-Note 'Adding the Microsoft Visual C++ runtime files next to PostgreSQL (no system-wide install)...'
    foreach ($f in $Downloads.vc_runtime.files) {
        $src = Join-Path $LI.Root ($f.from -replace '/', '\')
        if (-not (Test-Path $src)) { Stop-WithError "runtime file not found: $src" }
        if ($f.PSObject.Properties['sha256'] -and (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash -ne $f.sha256.ToUpper()) {
            Stop-WithError "runtime file $($f.to) failed its integrity check."
        }
        $sig = Get-AuthenticodeSignature -LiteralPath $src
        if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            Stop-WithError "runtime file $($f.to) is not validly signed by Microsoft ($($sig.Status))."
        }
        Copy-Item -LiteralPath $src -Destination (Join-Path $binDir $f.to) -Force
    }
}

function Install-Postgres([hashtable]$state) {
    $want = "$($Downloads.postgresql.version)+postgis-$($Downloads.postgis.version)"
    Write-Step "PostgreSQL $($Downloads.postgresql.version) with PostGIS $($Downloads.postgis.version)"
    if ((Test-Path (Get-PgExe 'postgres')) -and (Get-StateValue $state 'pg_build') -eq $want) { Write-Note 'Already installed.'; return }
    $pgZip = Get-VerifiedDownload $Downloads.postgresql 'PostgreSQL'
    $gisZip = Get-VerifiedDownload $Downloads.postgis 'PostGIS'
    $wasRunning = Test-PgRunning
    if ($wasRunning) { Stop-Pg }
    $stage = "$($LI.Pg).new"
    if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    Write-Note 'Extracting PostgreSQL (programs only; no pgAdmin, no Windows service)...'
    $n = Expand-ZipSubfolders $pgZip $stage @($Downloads.postgresql.extract) @($Downloads.postgresql.skip)
    Write-Note "  $n files"
    Write-Note 'Adding PostGIS...'
    $n = Expand-ZipSubfolders $gisZip $stage @($Downloads.postgis.extract) @($Downloads.postgis.skip)
    Write-Note "  $n files"
    Install-VcRuntime (Join-Path $stage 'bin')
    foreach ($f in 'postgres', 'pg_ctl', 'initdb', 'psql', 'pg_dump') {
        if (-not (Test-Path (Join-Path $stage "bin\$f.exe"))) { Stop-WithError "PostgreSQL program $f.exe is missing after extraction." }
    }
    if (-not (Test-Path (Join-Path $stage 'share\extension\postgis.control'))) { Stop-WithError 'PostGIS is missing after extraction.' }
    $old = "$($LI.Pg).previous"
    if (Test-Path $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    if (Test-Path $LI.Pg) { Rename-Item -LiteralPath $LI.Pg -NewName (Split-Path $old -Leaf) }
    Rename-Item -LiteralPath $stage -NewName (Split-Path $LI.Pg -Leaf)
    # The programs must run for this user without any system-wide runtime; prove it before going further.
    $code = Invoke-Native (Get-PgExe 'postgres') @('--version')
    if ($code -ne 0) {
        Stop-WithError ('PostgreSQL cannot run on this computer (exit code ' + $code + '). It may need the Microsoft Visual C++ ' +
                        'runtime, which requires an administrator to install. Nothing else was changed.')
    }
    $state['pg_build'] = $want
    if (Test-Path $old) { Remove-Item -LiteralPath $old -Recurse -Force }
}

function Invoke-Sql([string]$db, [string]$sql) {
    $env:PGPASSWORD = Get-Secret 'db_super'
    try {
        return Invoke-Native (Get-PgExe 'psql') @('-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1', '-p', "$($script:state['pg_port'])",
                                                 '-U', $PgSuperUser, '-d', $db, '-f', '-') $sql
    } finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
}

function Initialize-Database([hashtable]$state) {
    Write-Step 'Database'
    $hasData = Test-Path (Join-Path $LI.PgData 'PG_VERSION')
    if (-not $hasData) {
        if (Test-Path $LI.PgData) {
            if (@(Get-ChildItem -LiteralPath $LI.PgData -Force).Count -gt 0) {
                Stop-WithError "$($LI.PgData) exists but is not a database. It was left untouched; move it away and run Setup again."
            }
        }
        $port = Find-FreePort 54329 54399
        $state['pg_port'] = $port
        Write-Note 'Generating this installation''s own database passwords (stored encrypted for your Windows account)...'
        $super = New-RandomSecret
        Save-PlainSecret 'db_super' $super
        Save-PlainSecret 'db_app' (New-RandomSecret)
        $pwFile = Join-Path $LI.Secrets 'initdb.tmp'
        [IO.File]::WriteAllText($pwFile, $super, (New-Object Text.UTF8Encoding $false))
        try {
            $code = Invoke-Native (Get-PgExe 'initdb') @('-D', $LI.PgData, '-U', $PgSuperUser, "--pwfile=$pwFile",
                                                       '--auth=scram-sha-256', '--encoding=UTF8', '--no-locale')
        } finally { Remove-Item -LiteralPath $pwFile -Force -ErrorAction SilentlyContinue }
        if ($code -ne 0) { Stop-WithError 'creating the database failed (see above).' }
        # Local connections only, password (SCRAM) authentication only. No 'trust' anywhere.
        $hba = "# LeadIntel: local TCP connections with SCRAM passwords only.`r`n" +
               "host    all    all    127.0.0.1/32    scram-sha-256`r`n" +
               "host    all    all    ::1/128         scram-sha-256`r`n"
        [IO.File]::WriteAllText((Join-Path $LI.PgData 'pg_hba.conf'), $hba, (New-Object Text.UTF8Encoding $false))
        $conf = "`r`n# LeadIntel`r`nlisten_addresses = '127.0.0.1'`r`nport = $port`r`npassword_encryption = 'scram-sha-256'`r`n" +
                "logging_collector = off`r`nmax_connections = 40`r`n"
        [IO.File]::AppendAllText((Join-Path $LI.PgData 'postgresql.conf'), $conf, (New-Object Text.UTF8Encoding $false))
        $state['db_ready'] = $false
        Save-State $state
    } elseif (-not (Test-Secret 'db_super') -or -not (Test-Secret 'db_app')) {
        Stop-WithError ('a database exists but its encrypted passwords are missing (were they created by another Windows user?). ' +
                        'Nothing was changed.')
    }
    $script:state = $state
    Start-Pg | Out-Null

    if (-not (Get-StateValue $state 'db_ready' $false)) {
        Write-Note 'Creating the LeadIntel database and its application account...'
        $app = Get-Secret 'db_app'
        $sql = @"
DO `$`$BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$PgAppUser') THEN
    CREATE ROLE $PgAppUser LOGIN PASSWORD '$app';
  ELSE
    ALTER ROLE $PgAppUser LOGIN PASSWORD '$app';
  END IF;
END`$`$;
SELECT 'CREATE DATABASE $PgDatabase OWNER $PgAppUser ENCODING ''UTF8'' TEMPLATE template0'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$PgDatabase')\gexec
"@
        if ((Invoke-Sql 'postgres' $sql) -ne 0) { Stop-WithError 'creating the database account failed (see above).' }
        if ((Invoke-Sql $PgDatabase "CREATE EXTENSION IF NOT EXISTS postgis;`nCREATE EXTENSION IF NOT EXISTS pg_trgm;") -ne 0) {
            Stop-WithError 'enabling PostGIS failed (see above).'
        }
    } else {
        Write-Note 'Existing database kept.'
        $pending = (Get-StateValue $state 'app_version') -ne $Manifest.version
        if ($pending) {
            New-Item -ItemType Directory -Path $LI.Backups -Force | Out-Null
            $bak = Join-Path $LI.Backups ("leadintel-before-$($Manifest.version)-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.dump')
            Write-Note "Backing up the database before updating: $bak"
            $env:PGPASSWORD = Get-Secret 'db_app'
            try {
                $code = Invoke-Native (Get-PgExe 'pg_dump') @('-h', '127.0.0.1', '-p', "$($state['pg_port'])", '-U', $PgAppUser,
                                                            '-d', $PgDatabase, '-Fc', '-f', $bak)
            } finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
            if ($code -ne 0) { Stop-WithError 'the backup failed, so the database was not upgraded (see above).' }
        }
    }

    # M2: a separate login for sales (CRM) changes. 'db upgrade' gives it the CRM tables only, and LeadIntel checks the
    # restriction on every start before relying on it. Without it, sales changes simply pause while Load Data runs.
    if (-not (Test-Secret 'db_crm')) { Save-PlainSecret 'db_crm' (New-RandomSecret) }
    $crmPw = Get-Secret 'db_crm'
    $crmSql = @"
DO `$`$BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$PgCrmUser') THEN
    CREATE ROLE $PgCrmUser LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD '$crmPw';
  ELSE
    ALTER ROLE $PgCrmUser LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD '$crmPw';
  END IF;
END`$`$;
"@
    if ((Invoke-Sql 'postgres' $crmSql) -ne 0) {
        Write-Note 'Warning: the sales account could not be prepared. LeadIntel still works; sales changes pause while Load Data runs.'
    }
    $crmPw = $null

    Write-Note 'Bringing the database schema up to date...'
    Set-AppEnvironment $state
    try {
        if ((Invoke-LeadIntel @('db', 'upgrade')) -ne 0) { Stop-WithError 'the database schema upgrade failed (see above).' }
        if ((Invoke-LeadIntel @('config', 'seed')) -ne 0) { Stop-WithError 'loading the default settings failed (see above).' }
    } finally { Clear-AppEnvironment }
    $state['db_ready'] = $true
    $state['app_version'] = $Manifest.version
    Save-State $state
}

function Request-Sunbiz([hashtable]$state) {
    # LeadIntel uses Florida's publicly published Sunbiz login automatically (sunbiz-public-login.json), so Setup
    # asks nothing. Only 'setup.ps1 -SunbizLogin' asks, to save a different login (e.g. if Florida changes it).
    if (-not $SunbizLogin) { return }
    Write-Step 'Sunbiz login (override)'
    Write-Note 'LeadIntel normally uses the public login Florida publishes on its Data Downloads page.'
    Write-Note 'Enter a different login only if that one stopped working. Press Enter to keep the built-in one.'
    $u = Read-Host '   Sunbiz login name (press Enter to keep the built-in login)' -AsSecureString
    if ($u.Length -eq 0) {
        Remove-Item -LiteralPath (Join-Path $LI.Secrets 'sunbiz_user.dpapi'), (Join-Path $LI.Secrets 'sunbiz_password.dpapi') -ErrorAction SilentlyContinue
        Write-Note 'Using the built-in public login.'
    } else {
        $p = Read-Host '   Sunbiz password' -AsSecureString
        if ($p.Length -eq 0) { Write-Note 'No password entered; nothing changed.' }
        else { Save-Secret 'sunbiz_user' $u; Save-Secret 'sunbiz_password' $p; Write-Note 'Saved, encrypted for your Windows account.' }
    }
}

function Invoke-InitialLoad([hashtable]$state) {
    # First data load. Runs until one load has completed (fully, or with warnings - a partial load is not repeated
    # automatically; Load Data retries it). Never fails Setup: LeadIntel is already installed when this runs.
    $done = Get-StateValue $state 'initial_load'
    if ($done -eq 'complete' -or $done -eq 'complete_with_warnings') {
        Write-Step 'Prospect data'
        Write-Note 'Already loaded. Use Load Data (Start menu > LeadIntel) to update it.'
        return $done
    }
    Write-Step 'Loading public business data for the target counties (first time)'
    Write-Note 'This usually takes 10-60+ minutes and downloads several hundred MB. Progress is shown below.'
    Write-Note 'If it is interrupted, nothing already loaded is lost: run Load Data or Setup again to continue.'
    $outcome = 'failed'
    try {
        Start-Pg | Out-Null
        Set-AppEnvironment $state
        try { $res = Invoke-DataLoad $state } finally { Clear-AppEnvironment }
        $outcome = Show-DataLoadResult $state $res
    } catch {
        Write-Host "   The data load could not run: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '   LeadIntel is installed. Run Load Data (Start menu > LeadIntel) to load the data.'
    }
    Update-LoadState $state $outcome
    Save-State $state
    return $outcome
}

function New-Shortcuts {
    Write-Step 'Shortcuts'
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'LeadIntel'
    New-Item -ItemType Directory -Path $menu -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $items = @(
        @('LeadIntel', 'launch.ps1', 'Start LeadIntel and open it in the browser'),
        @('Stop LeadIntel', 'stop.ps1', 'Stop LeadIntel and its database'),
        @('Load Data', 'load-data.ps1', 'Download public business data and score prospects'),
        @('Export Prospects', 'export.ps1', 'Save prospects to a CSV file in Documents\LeadIntel Exports'))
    foreach ($i in $items) {
        $lnk = $shell.CreateShortcut((Join-Path $menu "$($i[0]).lnk"))
        $lnk.TargetPath = $ps
        $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $LI.App "packaging\windows\$($i[1])")`""
        $lnk.WorkingDirectory = $LI.Root
        $lnk.Description = $i[2]
        $lnk.Save()
    }
    $readme = $shell.CreateShortcut((Join-Path $menu 'README.lnk'))
    $readme.TargetPath = Join-Path $LI.App 'README.txt'
    $readme.Save()
    $desk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'LeadIntel.lnk'))
    $desk.TargetPath = $ps
    $desk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $LI.App 'packaging\windows\launch.ps1')`""
    $desk.WorkingDirectory = $LI.Root
    $desk.Description = 'Start LeadIntel'
    $desk.Save()
    Write-Note "Start menu: LeadIntel folder. Desktop: LeadIntel."
}

# ------------------------------------------------------------------------------------------------
try {
    Write-Host "LeadIntel Friend Edition $($Manifest.version) - Setup" -ForegroundColor Green
    Write-Host "Installs for Windows user '$env:USERNAME' only, into $($LI.Root). No administrator rights are used."
    if (-not [Environment]::Is64BitOperatingSystem) { Stop-WithError 'a 64-bit Windows is required.' }
    foreach ($k in 'python', 'postgresql', 'postgis') {
        if (-not $Downloads.$k.sha256) { Stop-WithError "this package is incomplete: the $k download is not pinned." }
    }
    $drive = Get-PSDrive -Name ($LI.Root.Substring(0, 1))
    if ($drive.Free -lt 4GB -and -not (Test-Path $LI.PgData)) { Stop-WithError "at least 4 GB of free disk space is needed on $($drive.Name):." }
    if (-not (Test-Path $LI.PgData)) {
        Write-Host ''
        Write-Host 'First installation: this takes about 10-60+ minutes and downloads roughly 1 GB in total' -ForegroundColor Yellow
        Write-Host '(about 600 MB of programs, then several hundred MB of public business data). Keep this window open.' -ForegroundColor Yellow
    }
    foreach ($d in 'Root', 'Data', 'Secrets', 'Backups', 'Logs', 'Downloads') { New-Item -ItemType Directory -Path $LI[$d] -Force | Out-Null }
    Start-Transcript -Path (Join-Path $LI.Logs ("setup-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')) | Out-Null

    $state = Read-State
    if (-not (Get-StateValue $state 'installation_id')) {
        $state['installation_id'] = [guid]::NewGuid().ToString()
        $state['created_at'] = (Get-Date).ToString('o')
    }
    $state['edition'] = 'friend'
    Save-State $state

    Install-AppFiles $state
    Install-Python $state;   Save-State $state
    Install-Packages $state; Save-State $state
    Install-Postgres $state; Save-State $state
    Initialize-Database $state
    Request-Sunbiz $state;   Save-State $state
    if (-not $NoShortcuts) { New-Shortcuts }
    # The verified archives are no longer needed (about 0.5 GB); a later Setup downloads again only if a version changes.
    Get-ChildItem -LiteralPath $LI.Downloads -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    # LeadIntel is fully installed and usable from here on; the data load below cannot undo that.
    $load = Invoke-InitialLoad $state
    Stop-PgIfIdle

    Write-Host "`nSetup finished. Start LeadIntel from the Desktop or Start menu, or double-click LeadIntel.cmd." -ForegroundColor Green
    if ($load -eq 'failed' -or $load -eq 'busy') {
        Write-Host 'The first data load did not finish, so LeadIntel may have no prospects yet. It is installed and works;' -ForegroundColor Yellow
        Write-Host 'to load the data, run Load Data (Start menu > LeadIntel > Load Data), or run Setup.cmd again.' -ForegroundColor Yellow
    } elseif ($load -eq 'warnings') {
        Write-Host 'Prospects are loaded (some sources were incomplete; see above). Run Load Data any time to update or retry.'
    } else {
        Write-Host 'Prospects are loaded. Run Load Data (Start menu > LeadIntel) any time to update them.'
    }
    if (-not $NoStart) {
        Write-Host ''
        [void](Read-Host 'Press Enter to start LeadIntel now (or close this window to start it later from the Desktop)')
        # Its own window: closing this Setup window then does not stop LeadIntel.
        $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        Start-Process -FilePath $ps -WorkingDirectory $LI.Root -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $LI.App 'packaging\windows\launch.ps1')`"")
    }
    exit 0
} catch {
    if ($_.Exception.Message -notlike 'LeadIntel:*') { Write-Host "`nSetup stopped: $($_.Exception.Message)" -ForegroundColor Red }
    Write-Host "Nothing you already had was deleted. Fix the problem above and run Setup again; it continues where it stopped."
    try { Stop-PgIfIdle } catch { }
    exit 1
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
