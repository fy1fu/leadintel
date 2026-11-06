# LeadIntel M1 + M2 — Windows test procedure (disposable installation)

One session tests the M1 interface, the upgrade to M2 (migration 0006) and the M2 sales features. It does this
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
| `LeadIntel-1.2.0-m2-TEST.zip` | *(filled in when the M2 TEST ZIP is approved and built)* | *(commit)* |

Record PASS / FAIL and a short note for every line; take a screenshot of anything that looks wrong. Time needed:
about 1.5–2 hours, most of it waiting for Load Data.

## 0. Prepare (in your normal account)

- [ ] Stop your real LeadIntel (Start menu › LeadIntel › Stop LeadIntel). **Do not run both installations at the same
      time**: they may use the same local database port.
- [ ] Create a local test account: Settings › Accounts › Other users › Add account › "I don't have this person's
      sign-in information" › "Add a user without a Microsoft account". Name: `LeadIntelTest`.
- [ ] Copy both ZIPs to `C:\Users\Public\Downloads` so the test account can read them.
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

## 3. Upgrade to M2 (migration 0006, test account only)

- [ ] Stop LeadIntel (Start menu › LeadIntel › Stop LeadIntel).
- [ ] Check the M2 fingerprint as in section 1, extract the M2 ZIP and run its `Setup.cmd`. Expected output:
  - [ ] "Backing up the database before updating: …\backups\leadintel-before-1.2.0-m2-….dump";
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

## 6. Report back

Send:
- PASS / FAIL with notes for sections 1–5;
- the business counts before and after the upgrade;
- the `test-dataload.ps1` output;
- screenshots;
- the full Setup output from section 3. Select it in the window and copy it; it contains no passwords.

## 7. Clean up (afterwards, your decision)

- [ ] Stop LeadIntel in the test account, sign out, and switch back to your normal account.
- [ ] To remove everything:
  1. Settings › Accounts › Other users › `LeadIntelTest` › Remove › "Delete account and data".
  2. This deletes only the test account's files, including its LeadIntel database.
  3. Your real installation is not affected.
- [ ] Start your real LeadIntel again. It is still M1, and its data is untouched.
