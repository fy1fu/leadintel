# LeadIntel on Windows: setup, backup, restore, recovery

Approved decision F2: native installs, no Docker. Tested commands are marked ✅; steps not yet run on a real Windows machine are covered by verification item V12.

## 1. Install (one time)

| Component | Version | How |
|---|---|---|
| PostgreSQL | 16.x | EDB installer from postgresql.org → keep port **5432** → note the `postgres` password → tick **Stack Builder** at the end |
| PostGIS | 3.4+ | In Stack Builder: *Spatial Extensions* → **PostGIS 3.x Bundle for PostgreSQL 16** |
| Python | 3.13 | python.org installer → tick **Add python.exe to PATH** |
| Git | latest | git-scm.com |
| Node.js | 22 LTS | nodejs.org (needed for the map UI and Lighthouse in later milestones) |

Create the databases in **SQL Shell (psql)**, which the PostgreSQL installer adds to the Start menu:

```sql
CREATE ROLE leadintel LOGIN PASSWORD 'choose-a-password';
CREATE DATABASE leadintel OWNER leadintel;
CREATE DATABASE leadintel_test OWNER leadintel;
\c leadintel
CREATE EXTENSION postgis; CREATE EXTENSION pg_trgm;
\c leadintel_test
CREATE EXTENSION postgis; CREATE EXTENSION pg_trgm;
```

The extensions need a superuser, so create them here once; LeadIntel's migrations then find them already installed.

## 2. Get the code and configure (PowerShell)

```powershell
git clone https://github.com/fy1fu/leadintel
cd leadintel\backend
py -3.13 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

Set environment variables for your user. Persistent `setx` values take effect in **new** PowerShell windows:

```powershell
setx LEADINTEL_DATABASE_URL "postgresql+psycopg://leadintel:choose-a-password@localhost:5432/leadintel"
setx LEADINTEL_TEST_DATABASE_URL "postgresql+psycopg://leadintel:choose-a-password@localhost:5432/leadintel_test"
setx SUNBIZ_SFTP_USER "<username from the official Sunbiz Data Downloads page>"
```

Set the Sunbiz password for the current window only, so it never lands in shell history:

```powershell
$env:SUNBIZ_SFTP_PASSWORD = Read-Host "Sunbiz public password"
```

## 3. First run

```powershell
.venv\Scripts\leadintel net-check --no-store    # network diagnostics; needs no database at all
.venv\Scripts\leadintel db upgrade              # create / upgrade the business schema
.venv\Scripts\leadintel config seed             # seed versioned settings from config\defaults
.venv\Scripts\leadintel discover --since 2026-10-01
.venv\Scripts\leadintel report > niche-report.json
.venv\Scripts\leadintel export prospects.csv --qualification provisionally_qualified,needs_research
```

Run the tests:

```powershell
.venv\Scripts\python -m pytest
```

## 4. Backup

Back up both the database and the data folder. The data folder holds downloaded source files and Overture snapshots, which can be re-downloaded but take time.

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmm
& "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -h localhost -U leadintel -Fc -f "D:\backups\leadintel-$stamp.dump" leadintel
Compress-Archive -Path ..\data -DestinationPath "D:\backups\leadintel-data-$stamp.zip"
```

Notes:
- Recommended: a weekly Task Scheduler job running the two commands above, keeping the last 8 dumps.
- The **network diagnostics store** (`data\netdiag.db`) is included in the data folder.
- Network diagnostics never need the business database.
- The custom-format dump (`-Fc`) is compressed and restorable table by table.

## 5. Restore

The restore target is a new database, so the current one stays untouched until you switch:

```powershell
& "C:\Program Files\PostgreSQL\16\bin\createdb.exe" -h localhost -U postgres -O leadintel leadintel_restore
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -U postgres -d leadintel_restore -c "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
& "C:\Program Files\PostgreSQL\16\bin\pg_restore.exe" -h localhost -U leadintel -d leadintel_restore --no-owner "D:\backups\leadintel-YYYYMMDD-HHMM.dump"
```

Check the restore, then point `LEADINTEL_DATABASE_URL` at `leadintel_restore` (or rename the databases):

```powershell
.venv\Scripts\leadintel db status
.venv\Scripts\leadintel report
```

## 6. Recovery

| Situation | What happens | What to do |
|---|---|---|
| PostgreSQL stopped or unreachable | `leadintel net-check` still works (own store). Settings fall back to packaged defaults with a warning; business commands stop with "business database unavailable". | Start the service: `services.msc` → **postgresql-x64-16** → Start. Re-run the command. |
| A discovery run was interrupted | The run stays `running` / `partial`. Imported files and snapshots are recorded. | Run `leadintel discover` again. Imports are idempotent and completed files are reused. |
| Overture snapshot corrupt or partial | The SHA-256 check fails, so the snapshot is rebuilt automatically on the next run | Nothing; or delete `data\overture\<release>` to force a rebuild |
| Sunbiz file download interrupted | The `.part` file is resumed; size is checked against the server listing; the ledger marks the day `failed` until it succeeds | Re-run `leadintel discover`; the ledger turns `verified` |
| A day is `absent_unexplained` | Coverage shows **partial** for the date range | Re-run later (files can appear late); reconcile with the quarterly file (Phase 1 M3 / V3) |
| Migration failed | Alembic runs each migration in a transaction; nothing half-applied | Fix the cause; `leadintel db upgrade` again. Restore from backup only if data was damaged. |
| Wrong merge | `merge_event` records every moved row | Unmerge (API/CLI arrives with M4; the function exists now: `app.resolve.merge.unmerge`) |
| Bad scoring settings | Scoring changes need approval; every version is kept | `leadintel config history --kind scoring`; restore an earlier version (CLI restore arrives with M4) |
