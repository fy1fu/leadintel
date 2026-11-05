LeadIntel - Friend Edition
==========================

LeadIntel finds local businesses in the target Florida counties and ranks them as website-development
prospects. This copy is completely your own: its own database, its own passwords, its own data. It does
not connect to anyone else's computer or account.

Requirements: Windows 10 or 11 (64-bit), an internet connection during setup and data loading,
about 4 GB of free disk space. No administrator rights are needed.


1. INSTALL
----------
1. Extract (unzip) this folder anywhere, for example to your Downloads folder.
   (Right-click the ZIP > Extract All. Do not run it from inside the ZIP.)
2. Double-click  Setup.cmd
   - If Windows shows "Windows protected your PC", click "More info" > "Run anyway".
     This appears because the package is not from a store; it only installs into your own user folder.
   - Setup downloads Python, its packages, PostgreSQL and PostGIS from their official websites (about 600 MB),
     checks each file's fingerprint, and installs everything into:
         %LOCALAPPDATA%\LeadIntel
   - It creates a private database with passwords generated just for this computer. They are encrypted
     for your Windows account; you never need to know or type them.
   - Finally it loads public business data for the target counties into your database, so LeadIntel
     opens with prospects. Progress (current step, elapsed time, MB downloaded) is shown in the window.
   - In total it takes about 10-60+ minutes and downloads roughly 1 GB. Keep the window open.
   - If the data load is interrupted or fails, LeadIntel is still installed; nothing already loaded is lost.
     Run Load Data (section 3) or Setup.cmd again to finish it.
3. When it says "Setup finished", press Enter to start LeadIntel (or close the window and start it later
   from the Desktop). You can then delete the extracted folder if you like (keep the ZIP for reinstalls).


2. START
--------
Double-click "LeadIntel" on the Desktop (or Start menu > LeadIntel > LeadIntel).
A window opens, then your browser opens LeadIntel at http://127.0.0.1:8765/ (only this computer can reach it).
Keep that window open while you use LeadIntel.


3. UPDATE DATA (Load Data)
--------------------------
Setup already loaded the data once. To refresh it with the latest public data, run:
    Start menu > LeadIntel > Load Data
It downloads new public data for the target counties, then matches and scores the prospects. Updates reuse
files that have not changed and are usually much faster than the first load. LeadIntel can stay open;
refresh the browser page when it finishes.
- Safe to run any time: if a load is already running, a second one stops without changing anything.
- If a load is interrupted or a source fails, everything loaded before is kept; just run Load Data again.
- If some sources were incomplete, the window lists them; the rest is saved.
- You normally do not need to run it yourself: when you start LeadIntel, it updates the data in the
  background if the last update was incomplete (after 3 hours) or the data is a week old.


4. USE
------
- Filters at the top: county, niche, qualification ("Candidates" = provisionally qualified + needs research),
  and a name search.
- The list (left) is sorted by prospect score (LPS). Click a business to see its record (right): scores,
  qualification reasons, contacts, online presence, addresses, registry filings and the source records.
- The map needs an internet connection; the list and records work without it.
- Scores are provisional: website research is not part of this version.


5. EXPORT TO CSV
----------------
Start menu > LeadIntel > Export Prospects
Saves the candidates to a new CSV file in  Documents\LeadIntel Exports  and opens that folder.
Open the file in Excel. Each export creates a new file; nothing is overwritten.


6. STOP / SHUT DOWN
-------------------
- Press Ctrl+C in the LeadIntel window: this stops LeadIntel and its database.
- Closing the window with X stops LeadIntel, but its database may keep running quietly in the background.
  To stop everything: Start menu > LeadIntel > Stop LeadIntel.
- Signing out or restarting Windows also stops everything. Your data is always kept.


7. UPDATES
----------
When you receive a newer LeadIntel ZIP:
1. Stop LeadIntel.  2. Extract the new ZIP.  3. Double-click its Setup.cmd.
Setup replaces only the program. Your database, downloaded data and settings are kept, and the database
is backed up to %LOCALAPPDATA%\LeadIntel\backups before any change to its structure.


8. SUNBIZ LOGIN
---------------
LeadIntel downloads newly registered Florida businesses from Sunbiz using the public login that Florida's
Division of Corporations publishes on its Data Downloads page. It is built in; you never need to enter it.
Only if Florida changes that login, enter the new one (from the Data Downloads page) by opening PowerShell and
running:
    powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\LeadIntel\app\packaging\windows\setup.ps1" -SunbizLogin


9. WHERE THINGS ARE
-------------------
%LOCALAPPDATA%\LeadIntel\
    app\  python\  venv\  pgsql\   program files (replaced by updates)
    pgdata\                        your database (never replaced by updates)
    data\                          downloaded public data (never replaced by updates)
    secrets\                       encrypted passwords (only your Windows account can read them)
    backups\                       database backups taken before updates
    logs\                          setup and database logs - useful if something goes wrong

To remove LeadIntel completely: Stop LeadIntel, then delete the %LOCALAPPDATA%\LeadIntel folder and the
LeadIntel shortcuts. This deletes your LeadIntel database too.


10. IF SOMETHING GOES WRONG
---------------------------
- Run Setup.cmd again: it continues where it stopped and never deletes your data.
- Send the newest file from %LOCALAPPDATA%\LeadIntel\logs to the person who gave you LeadIntel.
  (The logs never contain passwords.)
