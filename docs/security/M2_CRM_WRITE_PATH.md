# M2 security review: the CRM write path and the B0 maintenance lock

Date: 2026-11-06. Scope: how sales (CRM) changes are written while other LeadIntel operations hold the B0
maintenance lock. Tests: `backend/tests/test_crm_security.py` (on `leadintel_test`, synthetic data only).

## Finding: the M2 exemption relied on SQL text inspection (fixed)

Commit `03080b3f` let CRM writes bypass the B0 commit gate through a separate engine. That engine only checked
the SQL text: it refused `INSERT` / `UPDATE` / `DELETE` statements aimed at tables outside `crm_*`. An adversarial
probe on `leadintel_test` showed **8 ways past that check**:

| Bypass | Example |
|---|---|
| Comment between keyword and table | `UPDATE/**/location SET ...` |
| Quoted table name without a space | `UPDATE"location" SET ...` |
| Line comment and newline | `UPDATE --x` + newline + `location SET ...` |
| Side effect in a function | `SELECT setval('location_id_seq', ...)` |
| Large-object write | `SELECT lo_create(0)` |
| Role switch | `SET LOCAL ROLE postgres` |
| Role switch by function | `SELECT set_config('role', ...)` |
| Table creation | `SELECT ... INTO` |

Nothing in LeadIntel sent such statements; the gap was in what the guard could prove, not in an observed write.
Conclusion: the SQL text check alone was **not** safe, and has been replaced.

## Fix: database-enforced least privilege, otherwise the maintenance lock

| Mode | When | What protects business data |
|---|---|---|
| `shared_lock` (default) | No separate CRM login, or any check fails | Each CRM write takes the B0 maintenance lock and commits through the normal gated engine. B0 is not relaxed. While Load Data runs, sales changes are refused with "paused" (the typed text is kept on screen). |
| `independent` | A separate PostgreSQL **login** `leadintel_crm`, and every self-check passes | PostgreSQL itself: the login may read `location`, and read/write the six CRM tables and their sequences. It has no other privilege. |

**How the `independent` mode is set up and checked:**
- **Where the login comes from.** Setup creates it as a separate login, with no role attributes and `NOINHERIT`. It is not reached through `SET ROLE` from the app account, so no SQL statement can regain the app's rights.
- **Privileges.** `db upgrade` revokes everything from the login, then grants exactly the CRM set. This repeats at every upgrade, so stray grants do not survive.
- **Self-check before use** (`app/crm/access.py`, run again every 5 minutes). The login must:
  - be the session user, with no role attributes and no role memberships;
  - not own the database or any object;
  - have no `CREATE` anywhere;
  - have no `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `TRIGGER` or `REFERENCES` on any non-CRM table or view;
  - have no `TRUNCATE` or `TRIGGER` on the CRM tables;
  - be unable to advance or set any non-CRM sequence.

  In addition, no executable `SECURITY DEFINER` function and no usable foreign server may exist, and there must be no trigger or rule on a CRM table. Rules matter because they run with the table owner's rights. The login must also point at the same database as the app. Any failure, or an error while checking, means `shared_lock`.
- **Pooling.** Every pooled connection is checked on connect: it must be the restricted login.
- **Upgrades.** Each CRM transaction takes advisory key `UPGRADE_KEY` in shared mode (try, never wait). `db upgrade` takes it exclusively, waiting at most 30 s, before migrating. So CRM writes never overlap a schema upgrade.
- **Text check.** It is kept only as a secondary layer: comment- and literal-aware, single statement, an allow-list of write targets.

## Adversarial tests

| Attack | Result |
|---|---|
| 31 attacks run directly as the CRM login, with no application check | All refused by PostgreSQL; business rows, scores, the `location` sequence and the object count are unchanged. |
| The same 31 through the application engine | All refused; the pooled connection is still the restricted login, and normal CRM work continues afterwards. |
| 10 kinds of tampering | Each one switches LeadIntel to `shared_lock`; undoing it restores `independent`. |
| Load Data holding the maintenance lock | `independent` writes continue; `shared_lock` writes are refused as "paused", nothing is saved, and no lock is left behind. |
| Another process's CRM write holding the lock | `shared_lock` waits for it (at most 5 s). |
| An upgrade holding the key | CRM writes are refused. |
| An upgrade starting during a running CRM transaction | The upgrade waits, and gives up safely if the transaction does not finish. |
| The real `db upgrade` command (`main(["db", "upgrade"])`) | Grants exactly the CRM privileges. |

The 31 attacks cover:
- comments, quoting and multiple statements;
- nested CTEs, `MERGE`, `TRUNCATE` and `COPY` (from a program and to a server file);
- table creation, `SELECT INTO`, `ALTER`, `DROP`, indexes, triggers and rules;
- `GRANT`, `SET ROLE`, `SET SESSION AUTHORIZATION` and `set_config`;
- `setval` and `nextval` on business sequences;
- `DO` blocks and temporary `SECURITY DEFINER` functions;
- terminating other sessions, reading server files, creating schemas, and `VACUUM`.

The 10 kinds of tampering:
- a business-table grant;
- `TRUNCATE` on a CRM table;
- a business-sequence grant;
- `CREATE` on the schema;
- membership of `pg_write_all_data`;
- the `CREATEDB` attribute;
- a `SECURITY DEFINER` function;
- a trigger on a CRM table;
- a rule on a CRM table;
- the login owning a table.

## Residual risks (open, low)

- **R1.** The CRM login can create session-local temporary tables and large objects, and take advisory locks. These are PostgreSQL defaults for every login and touch no business data. A test pins this behaviour.
- **R2.** The B0 gap in the lock itself is unchanged: an operation can finish one commit after the lock is lost. This is the pre-existing B0.5 fencing item, now planned as migration 0007.
- **R3.** Setup's sales-login step is new PowerShell, not yet run on Windows. If it fails, Setup only warns and LeadIntel falls back to `shared_lock`.
