# LeadIntel M1 → M3 — Windows test procedure (disposable installation)

One session tests the M1 interface, the upgrade to the M3 TEST build (migrations 0006 and 0007) and the M2 sales and
M3 features (Search Area, website audits, saved searches). It does this
**without touching your real Friend Edition**: everything runs in a separate local Windows test account. LeadIntel
installs per Windows user (`%LOCALAPPDATA%\LeadIntel`), so the test account gets its own:

- program folder;
- PostgreSQL database cluster;
- encrypted passwords;
- data;
- shortcuts.

Your real installation, its database and its data are never opened.

Packages (TEST builds only — not for distribution):

| Package | SHA-256 | Built from |
|---|---|---|
| `LeadIntel-1.1.0-m1-TEST.zip` | `89EE97CC3D428B2641FFCBCDB8ED8A6C7A3C5083193433C2BB99807635D26208` | `aa4eb2de` |
| `LeadIntel-1.3.0-m3-TEST.zip` | *(see the build note sent with the ZIP)* | *(commit in the build note)* |

The M3 TEST build contains everything from M2, so no separate M2 package is needed.

Record PASS / FAIL and a short note for every line; take a screenshot of anything that looks wrong. Time needed:
about 2–2.5 hours, most of it waiting for Load Data.

## 0. Prepare (in your normal account)

- [ ] Stop your real LeadIntel (Start menu › LeadIntel › Stop LeadIntel). **Do not run both installations at the same
      time**: they may use the same local database port.
- [ ] Create a local test account: Settings › Accounts › Other users › Add account › "I don't have this person's
      sign-in information" › "Add a user without a Microsoft account". Name: `LeadIntelTest`.
- [ ] Copy both ZIPs (M1 and M3) to `C:\Users\Public\Downloads` so the test account can read them.
- [ ] Switch to the test account: Start › your name › `LeadIntelTest`. Everything below happens there.

## 1. Fresh M1 installation (test account)

- [ ] Check the M1 fingerprint. The command must print exactly the M1 value in the table above:
      `(Get-FileHash "C:\Users\Public\Downloads\LeadIntel-1.1.0-m1-TEST.zip").Hash`
- [ ] Extract the M1 ZIP to the test account's Downloads folder and run `Setup.cmd`. It installs from scratch (no
      "updating" message). At the end, "Press Enter to start LeadIntel now" opens LeadIntel.
- [ ] Start menu › LeadIntel › Load Data. It runs, shows progress steps, and ends with "Done" and the business count.
      **Write the count down.**

## 2. M1 checks

Follow sections 2–8 of the M1 checklist (`M1-Windows-acceptance-checklist.md`, sent with the M1 ZIP):
- the app window, full screen and `-Fullscreen`;
- the map under the security policy;
- filtering, paging and export;
- business details;
- themes;
- display scaling 100 / 125 / 150 %;
- data loading;
- `test-dataload.ps1`, which should report **47 passed, 0 failed**.

Results are recorded the same way as in the M1 checklist.

## 3. Upgrade to the M3 TEST build (migrations 0006 and 0007, test account only)

- [ ] Stop LeadIntel (Start menu › LeadIntel › Stop LeadIntel).
- [ ] Check the M3 fingerprint as in section 1, extract the M3 ZIP and run its `Setup.cmd`. Expected output:
  - [ ] "Backing up the database before updating: …\backups\leadintel-before-1.3.0-m3-….dump";
  - [ ] the Python packages install without errors (M3 adds `playwright`, `greenlet` and `pyee`; no browser is
        downloaded: audits use the Microsoft Edge that Windows already has);
  - [ ] no warning about "the sales account";
  - [ ] if shown: "CRM login leadintel_crm: privileges set". This line comes from the `db upgrade` step that Setup
        runs, not from Setup itself, and Setup may not display it. If it does not appear, the profile dialog check in
        section 4 confirms the same thing;
  - [ ] Setup finishes and offers to start LeadIntel.
- [ ] The business count and the data banner match section 1, so **no data was lost**.

## 4. M2 sales features

- [ ] A **Sales** button and your initials button appear at the top right. The filter panel has a **Sales** group:
      Claimed, Follow-up, List and status chips.
- [ ] Open a business. The Sales section asks you to set up your profile. Enter a name and initials, then save.
- [ ] The profile dialog (your initials button) says: "Sales changes: also saved while Load Data runs (separate,
      restricted database account)". If it says "paused …", note it: that means the safe fallback is active.
- [ ] **Claim**: the section says "Claimed by you". The list entry shows "Mine".
- [ ] Set Status = Contacted, a next action and a follow-up date of yesterday, then **Save**. The list shows
      "Contacted" and "Follow-up overdue". The Sales button shows "Sales · 1 due".
- [ ] Log a call with an outcome and a note. It appears at the top of the timeline.
- [ ] Add the business to a new list ("Add to list…" › "New list…"). The list chip appears.
- [ ] Open the **Sales** overview. It shows:
  - [ ] the claimed / overdue counts;
  - [ ] a pipeline with "Contacted 1";
  - [ ] the follow-up row;
  - [ ] the recent activity, described in words (no blank "→" entries);
  - [ ] the list.
- [ ] Click "Contacted" in the pipeline. The list shows only that business. Ctrl+R keeps the filter.
- [ ] Filter Claimed = "Not claimed", then Follow-up = "Overdue": the counts change accordingly.
- [ ] **Export CSV**: the file has the columns `sales_status, sales_owner, follow_up, next_action`, filled in for the
      claimed business. It opens in Excel.
- [ ] Release the claim: the section says "Not claimed". The history keeps every entry.
- [ ] Close LeadIntel and start it again. Profile, claim history, list and notes are all still there.

## 5. Sales changes while Load Data runs

- [ ] Start Load Data (Start menu › LeadIntel › Load Data). While it is still running, in the app:
  - [ ] add a note and claim another business. Both should be **saved**, if the profile dialog said "also saved
        while Load Data runs".
  - [ ] if instead a message says "sales changes are paused …", note it. The text you typed must still be in the
        box after the message.
- [ ] Let Load Data finish. The business count is the same as or higher than before, and your notes are unchanged.

## 6. M3 Search Area

- [ ] Open the **Area** panel and switch it on: a 10-mile circle appears on the map around the default center. The
      list, the counts and the map show only businesses inside it.
- [ ] Drag the center marker: the circle and the results follow. Panning or zooming the map does **not** move it.
- [ ] "Pick on map", then click somewhere: the center moves there.
- [ ] Type a ZIP code (for example 32202), then a city ("Orange Park"), a business name and a street address
      ("117 W Duval St Jacksonville"), choosing a result each time: the center moves. Places outside the target
      counties are marked.
- [ ] Move the radius slider and type a radius; switch miles ↔ km: the circle size does not change when switching units.
- [ ] Back and Forward in the browser (Alt+← / Alt+→) restore the previous areas. Ctrl+R keeps the area.
- [ ] Export CSV with the area on: the number of rows equals the count shown.

## 7. M3 website audits

Use a business whose website you would be comfortable visiting once (a single, polite visit of up to 15 pages; do not
audit many real businesses in a row - this is a functional check, not a bulk crawl).

- [ ] Open the business. The **Website quality (WQS)** card says "Not analysed yet … Unknown is not the same as poor".
- [ ] The **Website audit** section shows the website and a **Run website audit** button. Click it:
  - [ ] "Audit running…" with a timer appears and the button is disabled;
  - [ ] within about a minute the section shows **Completed** (or "Partly completed", or a clear reason such as
        "Not allowed by the website (robots.txt)"), and the WQS card shows a number with a **Provisional** badge and
        "% assessed".
- [ ] If instead it says "the browser for website audits could not be started (LeadIntel uses Microsoft Edge …)",
      note the full message: Edge is missing or blocked on this PC.
- [ ] **Open the audit report**:
  - [ ] seven areas with bars; "Visual design" says "Not assessed";
  - [ ] "What to suggest first": up to 5 items, each with Found / Why it matters / Suggestion, no promises about
        revenue;
  - [ ] "Compared with similar businesses": most likely "Not enough comparable data … (at least 8 are needed)";
  - [ ] Findings in three groups: Concerning, Good, "Not assessed - these never lower the score";
  - [ ] desktop and phone screenshots show the website's pages; clicking one opens the picture;
  - [ ] "Open the live website in your browser" opens the site in a normal browser window (not inside LeadIntel).
- [ ] Click **Run the audit again**. When it finishes, "Earlier audits (1)" lists the first audit, and the report
      mentions the previous audit's score.
- [ ] While an audit runs, start **Load Data**: Load Data waits or says another task is running; start an audit while
      Load Data runs: the section says another LeadIntel task (such as Load Data) is running. Nothing breaks.
- [ ] Close LeadIntel during an audit, start it again and run another audit: the earlier one is listed as
      "Interrupted".
- [ ] The screenshots are stored under `%LOCALAPPDATA%\LeadIntel\data\crawl\`.

## 8. M3 saved searches

- [ ] Set some filters, an area and List-only layout. Click **Saved searches**, type a name, **Save**. The list shows
      a readable summary ("within 10 mi of …").
- [ ] Change everything (clear filters, switch layout), then **Saved searches** › **Open**: the filters, area (same
      radius and unit), sort and layout come back exactly.
- [ ] Save again under the same name with different filters: it is replaced, not duplicated. Delete it.

## 9. Report back

Send:
- PASS / FAIL with notes for sections 1–8;
- the business counts before and after the upgrade;
- the `test-dataload.ps1` output;
- screenshots;
- the full Setup output from section 3. Select it in the window and copy it; it contains no passwords.

## 10. Clean up (afterwards, your decision)

- [ ] Stop LeadIntel in the test account, sign out, and switch back to your normal account.
- [ ] To remove everything:
  1. Settings › Accounts › Other users › `LeadIntelTest` › Remove › "Delete account and data".
  2. This deletes only the test account's files, including its LeadIntel database.
  3. Your real installation is not affected.
- [ ] Start your real LeadIntel again. It is still M1, and its data is untouched.

WQS stays "Provisional" in this build: checking it against human ratings of 60-100 real websites is a separate,
later task with its own instructions.
