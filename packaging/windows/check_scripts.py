"""Static safety checks for the Friend Edition PowerShell scripts (no PowerShell needed; not shipped in the ZIP).

    python packaging/windows/check_scripts.py

1. Second launch: when LeadIntel is already running, launch.ps1 only opens the browser - it sets the
   second-launch flag before leaving, starts no server, starts no database, and its cleanup (stop server,
   remove run.json, stop database) is skipped; run.json is only ever removed by the window that wrote it
   (or as stale when its server no longer answers).
2. User data: no script deletes, renames or overwrites pgdata, data or secrets; Setup only replaces program
   folders (app, python, venv, pgsql) and the downloads cache, and backs up the database before upgrading.
Exits 1 on any failure.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
fails = []


def check(name, ok):
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        fails.append(name)


def block_after(text, start_pat):
    """Text of the brace block that follows the first match of start_pat."""
    m = re.search(start_pat, text)
    if m is None:
        return ""
    i = text.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
    raise ValueError("unbalanced braces")


launch = (HERE / "launch.ps1").read_text()
alive = block_after(launch, r"if \(\$alive\) ")
check("already-running branch sets the second-launch flag", "$secondLaunch = $true" in alive)
check("already-running branch opens the browser and leaves with return (finally still runs)",
      "Start-Process \"http://127.0.0.1:$($run.port)/\"" in alive and re.search(r"\breturn\b", alive) is not None)
check("already-running branch has no exit, no server start, no database start, no run.json removal",
      not re.search(r"\bexit\b|Start-Pg|uvicorn|Remove-Item", alive))
check("no 'exit' anywhere in launch.ps1 (exit would bypass the second-launch logic)", not re.search(r"(?m)^\s*exit\b|[;{]\s*exit\b", launch))
check("the server start comes after the already-running check",
      "$secondLaunch = $true" in launch and launch.index("$secondLaunch = $true") < launch.index("'-m', 'uvicorn'") and launch.index("$secondLaunch = $true") < launch.index("Start-Pg"))
fin = block_after(launch, r"\}\s*finally ")
guard = block_after(fin, r"if \(-not \$secondLaunch\) ")
outside = fin.replace(guard, "")
check("finally: stopping the server, removing run.json and stopping the database are all inside 'if (-not $secondLaunch)'",
      all(s in guard for s in ("Stop-Process", "Remove-Item -LiteralPath $LI.Run", "Stop-PgIfIdle"))
      and not re.search(r"Stop-Process|Remove-Item|Stop-Pg", outside))
check("finally: run.json is removed only when its pid is this window's server",
      re.search(r"\.pid -eq \$server\.Id", guard) is not None and "if ($mine)" in guard)
stale = launch[:launch.index("Write-Host 'Starting the database...'")]
check("stale run.json is removed only after the health check failed (not when alive)",
      "if ($alive)" in stale and stale.index("if ($alive)") < stale.index("Remove-Item -LiteralPath $LI.Run"))

stop = (HERE / "stop.ps1").read_text()
check("Stop LeadIntel still stops the server(s) and removes run.json",
      "Stop-Process -Id $p.ProcessId" in stop and "Remove-Item -LiteralPath $LI.Run" in stop)

USER = r"(PgData|\.Data\b|Secrets)"
for f in sorted(HERE.glob("*.ps1")):
    t = f.read_text()
    bad = [ln.strip() for ln in t.splitlines()
           if re.search(r"Remove-Item|Rename-Item|Move-Item|Copy-Item|WriteAllText|Set-Content|Out-File", ln)
           and re.search(r"\$LI\." + USER + r"(?!\s*'|\w)|\$LI\[['\"](PgData|Data|Secrets)", ln)]
    # Allowed: writing NEW files inside secrets (encrypted secrets, the deleted-at-once initdb password file),
    # and writing pg_hba.conf/postgresql.conf only when creating a brand-new database.
    allowed = [b for b in bad if re.search(r"Join-Path \$LI\.Secrets|\$pwFile|pg_hba\.conf|postgresql\.conf", b)]
    check(f"{f.name}: never deletes, renames, moves or overwrites pgdata, data or secrets", set(bad) == set(allowed))

setup = (HERE / "setup.ps1").read_text()
init = block_after(setup, r"function Initialize-Database\(\[hashtable\]\$state\) ")
new_db = block_after(init, r"if \(-not \$hasData\) ")
check("setup: pg_hba.conf/postgresql.conf are written only while creating a brand-new database",
      "pg_hba.conf" in new_db and "postgresql.conf" in new_db and init.count("pg_hba.conf") == 1)
check("setup: an existing non-database pgdata folder is left untouched (setup stops)", "was left untouched" in new_db)
check("setup: database passwords are never regenerated for an existing database",
      init.index("New-RandomSecret") < init.index("} elseif (-not (Test-Secret 'db_super')"))
check("setup: database is backed up before upgrading an existing installation", re.search(r"pg_dump.*-Fc", init, re.S) is not None)
check("setup: removals only target program folders and the downloads cache",
      all(re.search(r"\$new|\$prev|\$old|\$stage|\$LI\.Python|\$LI\.Venv|\$part|\$pwFile|\$LI\.Downloads|Remove-Item Env:", ln)
          for ln in setup.splitlines() if "Remove-Item" in ln))

# Under Set-StrictMode, a function that returns an empty list returns $null, and $null.Count throws
# ("The property 'Count' cannot be found"). Function results must be wrapped in @(...) before .Count.
FUNCS = set()
for f in sorted(HERE.glob("*.ps1")):
    FUNCS |= set(re.findall(r"(?m)^function\s+([\w-]+)", f.read_text()))
for f in sorted(HERE.glob("*.ps1")):
    t = f.read_text()
    bad = [m.group(0) for m in re.finditer(r"(?<!@)\(\s*(" + "|".join(map(re.escape, FUNCS)) + r")\b[^()]*\)\.(Count|Length)", t)]
    assigned = re.findall(r"\$(\w+)\s*=\s*(?!@\()(?:" + "|".join(map(re.escape, FUNCS)) + r")\b", t)
    bad += [f"${v}.Count" for v in assigned if re.search(r"\$" + v + r"\.(Count|Length)\b", t)]
    check(f"{f.name}: function results are wrapped in @(...) before .Count", not bad)

# Windows PowerShell 5.1: Measure-Object -Property returns nothing for empty input, so .Sum/.Count on it fails
# under strict mode (seen on Windows). Use @(...).Count or a loop instead.
for f in sorted(HERE.glob("*.ps1")):
    check(f"{f.name}: no Measure-Object (empty input breaks it under strict mode)", not any("Measure-Object" in ln for ln in f.read_text().splitlines() if not ln.lstrip().startswith("#")))

# ---- automatic initial data load (Friend Edition 1.0.0) ----
common = (HERE / "common.ps1").read_text()
load = (HERE / "load-data.ps1").read_text()
dl = block_after(common, r"function Invoke-DataLoad\(")
check("data load runs the existing discover command (no separate score step anywhere)",
      "'discover')" in dl and not re.search(r"Invoke-LeadIntel\s+@\('score'\)", load + setup))
check("data load never stops or kills other processes (a load in progress is left alone)",
      not re.search(r"Stop-Process|\.Kill\(|taskkill", dl + block_after(common, r"function Show-DataLoadResult\(")))
check("exit codes: 0 complete, 4 warnings, 5 busy, everything else failed",
      re.search(r"0 \{ 'complete' \} 4 \{ 'warnings' \} 5 \{ 'busy' \} default \{ 'failed' \}", common) is not None)
uls = block_after(common, r"function Update-LoadState\(")
check("state: 'busy' records nothing", re.search(r"if \(\$outcome -eq 'busy'\) \{ return \}", uls) is not None)
check("state: a failure never downgrades an earlier complete / complete_with_warnings load",
      "elseif ($prev -ne 'complete' -and $prev -ne 'complete_with_warnings') { $state['initial_load'] = 'failed' }" in uls
      and "if ($prev -ne 'complete') { $state['initial_load'] = 'complete_with_warnings' }" in uls)
il = block_after(setup, r"function Invoke-InitialLoad\(")
check("setup: initial load is skipped once complete or complete with warnings (partial loads are not repeated)",
      "if ($done -eq 'complete' -or $done -eq 'complete_with_warnings')" in il)
check("setup: initial load cannot fail Setup (errors are caught inside it)", re.search(r"\btry \{", il) and "} catch {" in il
      and "Stop-WithError" not in il)
main = setup[setup.rindex("Install-AppFiles $state"):]
check("setup: initial load runs only after the database, Sunbiz prompt and shortcuts are done",
      main.index("Initialize-Database $state") < main.index("Request-Sunbiz $state") < main.index("New-Shortcuts")
      < main.index("Invoke-InitialLoad $state"))
check("setup: explains how to retry when the initial load fails or another load is running",
      "run Load Data (Start menu > LeadIntel > Load Data), or run Setup.cmd again" in main)
check("setup and Load Data announce the 10-60+ minute / several hundred MB first load",
      "10-60+ minutes" in setup and "several hundred MB" in setup and "10-60+ minutes" in load)
wm = block_after(common, r"function Get-WrittenMB\(")
check("progress: MB counts only files written since this run started", "LastWriteTime -ge $since" in wm)
fs = block_after(common, r"function Format-LoadStep\(")
check("progress: step text comes only from the run record and finished-source rows",
      "$runStatus" in fs and "$finished" in fs and "Working (step details unavailable)" in dl)
check("progress queries are read-only", "default_transaction_read_only=on" in block_after(common, r"function Invoke-ReadOnlySql\("))

print(f"\n{'ALL CHECKS PASSED' if not fails else f'{len(fails)} CHECK(S) FAILED'}")
sys.exit(1 if fails else 0)
