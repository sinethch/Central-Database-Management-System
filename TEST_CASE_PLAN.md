# DBOps End-to-End Test Case Plan

## 1. Purpose and scope

This plan covers manual end-to-end and regression testing of the current DBOps Control Plane UI and its connected API workflows:

- Authentication, optional MFA, and access roles
- Database targets and dashboard monitoring
- Remote server registration, health checks, and isolated PostgreSQL, MongoDB, MySQL/MariaDB, and Redis container provisioning
- Manual PostgreSQL snapshots and remote copies
- Backup schedule configuration and execution
- Backups list, download, verification, and deletion
- Restore Center, new-database and existing-database restores
- Restored Databases registration and connection URL access
- Storage metrics and mounted snapshot repositories
- Telegram notifications, migration history, and activity

This is a **test plan**, not evidence that the cases have already been executed. Record actual results in the run sheet in section 5.

## 2. Safety and execution rules

1. Run destructive or operational cases only against a dedicated QA deployment, disposable PostgreSQL databases, and a remote QA server. Never use production databases, production hosts, or production backup paths.
2. Take and verify a known-good snapshot of the QA fixture before testing restore, retention, or deletion.
3. Existing-database restore writes to the selected database. Run it only against a disposable target created for this test run. Never point it at the source fixture or any shared database.
4. Container provisioning creates a new remote database container, data directory, and registered database target. Confirm the selected engine, server, and an unused host port before submitting. Do not use an existing container name, port, or data directory.
5. Backup deletion removes the app's local archive and list entry. Test only on a disposable test snapshot after confirming remote/repository copies are not being relied on.
6. Use test-only Telegram bot/chat credentials if testing live delivery. Never include credentials, full connection URLs, MFA seeds, or unredacted logs in evidence.
7. Do not bypass SSH host-key checks. Confirm the QA server host key out-of-band and use the configured trusted `known_hosts` file.
8. Label all test-created resources with a run identifier, for example `QA-20261020-01`. Use a fresh identifier for each run.
9. Stop if the UI identifies a non-QA database, server, or destination. Record the actual target and stop; do not continue on an ambiguous selection.

## 3. Environment, roles, and test data

### Required environment

- A working QA DBOps deployment with the current UI and API.
- An administrator QA account and a non-admin QA account.
- A disposable PostgreSQL source database with known data. Include a table such as `qa_restore_probe` with a unique run ID row, and record the expected schema and row count before snapshotting.
- A second disposable PostgreSQL destination for existing-database restore tests. It must be safe to overwrite/change and must not contain valuable data.
- Optional non-PostgreSQL QA target(s), if verifying that monitoring is supported while snapshot/restore remains PostgreSQL-only.
- A QA Linux server with Docker, SSH enabled, verified host key, configured known-hosts access, and pre-created parent directories for the configured DBOps database and snapshot roots.
- An unused QA server host port for container provisioning.
- A mounted QA snapshot repository if repository replication is in scope.
- Test Telegram bot/chat configuration if live delivery is in scope; otherwise use the application's safe test/dry-run setup, if available.
- An API key and the configured encryption keys required by the environment. Keep them in the approved secret store and out of the test report.

### Suggested fixture and naming

- Run ID: `QA-YYYYMMDD-NN`
- Source database label: `QA Source <run-id>`
- Existing restore target: `QA Existing Target <run-id>`
- Remote provisioned database/container: `qa-pg-<run-id>`
- Snapshot description: `QA snapshot <run-id>`
- Schedule: daily at a near-future QA-only time, or a supported custom schedule that can be observed without waiting overnight.
- Fixture data: record table names, row counts, and a unique probe row before snapshot. Compare them after restore.

## 4. Execution conventions

- Start each test from a clean, signed-in browser session unless a case says otherwise.
- Use the dashboard labels as displayed. API requests may be used as supplementary diagnostics, but UI workflows and visible results are the primary acceptance criteria.
- Capture evidence with a test ID, timestamp, run ID, redacted screenshot, and relevant non-secret status/log text.
- Treat an expected blocked/validation result as a pass for negative cases.
- For asynchronous snapshot, schedule, and restore jobs, wait for a terminal status and use the UI refresh controls where needed. Do not infer success merely because a dialog closed.

## 5. Run sheet

Copy this table into the execution record and fill in the result and evidence for each test.

| Test ID | Result (Pass/Fail/Blocked/Not run) | Actual result / defect reference | Evidence (redacted) | Tester / date |
|---|---|---|---|---|
| TAIL-01 to TAIL-10 | | | | |
| AUTH-01 to AUTH-04 | | | | |
| DB-01 to DB-03 | | | | |
| DASH-01 to DASH-04 | | | | |
| SERVER-01 to SERVER-05 | | | | |
| SNAP-01 to SNAP-05 | | | | |
| SCHED-01 to SCHED-04 | | | | |
| BACKUP-01 to BACKUP-05 | | | | |
| RESTORE-01 to RESTORE-06 | | | | |
| RDB-01 to RDB-03 | | | | |
| STORAGE-01 to STORAGE-04 | | | | |
| NOTIFY-01 to NOTIFY-04 | | | | |
| ACT-01 to ACT-02 | | | | |

## 6. Test cases

### A. Sign-in and user access

#### AUTH-01 — Sign in without MFA

**Priority:** P1 · **Type:** Positive

**Preconditions:** QA user exists with MFA disabled; user is not already signed in.

**Steps**
1. Open the DBOps login page.
2. Enter the QA username and password and submit.
3. Observe the next screen and the signed-in user indicator.

**Expected**
- Sign-in succeeds without requesting a Microsoft Authenticator code.
- The dashboard loads and identifies the expected user.

#### AUTH-02 — Sign in with MFA enabled

**Priority:** P1 · **Type:** Positive

**Preconditions:** A separate QA user has MFA enabled and is enrolled in an authenticator app.

**Steps**
1. Submit the MFA-enabled user's username and password.
2. Enter a current six-digit authenticator code.
3. Submit the code.

**Expected**
- The UI requests MFA only for this enrolled account.
- A valid code completes sign-in; an invalid or expired code is rejected without granting a session.

#### AUTH-03 — Create users with MFA off and on

**Priority:** P1 · **Type:** Positive / regression

**Preconditions:** Signed in as administrator.

**Steps**
1. Open **Users**.
2. Create one QA user with **Enable Microsoft Authenticator (MFA)** unchecked.
3. Confirm the user is created and no setup QR/manual key is presented for that user.
4. Create a second QA user with the MFA checkbox selected.
5. Confirm a QR code/manual entry key is displayed and securely enroll the test account.
6. Sign out and test each account using AUTH-01 and AUTH-02.

**Expected**
- The checkbox state is respected: only the checked account requires MFA.
- The MFA setup key is displayed only for the enrolled account and is not copied into evidence.

#### AUTH-04 — Non-admin access to administrator actions

**Priority:** P1 · **Type:** Negative / authorization

**Preconditions:** Signed in as a non-admin QA user.

**Steps**
1. Inspect the header for the **Users** control.
2. Attempt administrator-only actions such as viewing/copying a credential-bearing restored database URL, where applicable.
3. Where API authorization is part of the test setup, attempt the corresponding operation without administrator authorization.

**Expected**
- Administrator-only controls are hidden or denied.
- No user management or full credential-bearing URL is exposed to the non-admin.

### B. Database targets and dashboard

#### DB-01 — Register and select a PostgreSQL target

**Priority:** P1 · **Type:** Positive

**Preconditions:** A reachable disposable PostgreSQL QA database and least-privileged QA credentials.

**Steps**
1. Click **Add database instance**.
2. Enter a QA label and PostgreSQL connection URL.
3. Use **Test Connection**.
4. If the test succeeds, click **Add Database**.
5. Select the new target pill.

**Expected**
- A successful connectivity result is shown and the target is added.
- The selected target is reflected in the dashboard and active-target toolbar.
- The displayed connection URL is masked; clicking its copy control, if used, is done only in the QA environment.

#### DB-02 — Reject a bad URL or unreachable target

**Priority:** P1 · **Type:** Negative

**Steps**
1. Open **Add database instance**.
2. Enter an invalid URL, then try an unreachable QA host or invalid QA credentials.
3. Use **Test Connection** and attempt submission if the form permits.

**Expected**
- Validation/connectivity failure is explicit.
- The failed target is not silently reported as healthy or added as a successful connection.
- No credentials are exposed in the error message.

#### DB-03 — Unregister a QA database target

**Priority:** P1 · **Type:** Destructive action / isolated QA

**Preconditions:** A disposable registered QA target created for this run.

**Steps**
1. Select the QA target and click **Unregister**.
2. Review and accept the confirmation.
3. Confirm it disappears from the app's Targets list.
4. Independently connect to the QA database using a permitted QA client.

**Expected**
- The target is removed from DBOps monitoring.
- The database and its data still exist; unregistering does not drop or alter the database.

#### DASH-01 — Dashboard metrics and engine-specific availability

**Priority:** P1 · **Type:** Positive

**Steps**
1. Select the QA PostgreSQL source target.
2. Review connection status, latency, engine version, uptime, database size, buffer cache, query/deadlock visualization, active queries, table sizes, and lock diagnostics as available.
3. Use **Refresh** and confirm values/status refresh.
4. If a non-PostgreSQL QA target is available, select it and review which metrics are supported.

**Expected**
- PostgreSQL values are populated when the target and permissions support them.
- Unsupported metrics are identified as unsupported/unavailable rather than shown as fabricated success data.
- Switching targets refreshes target-specific data.

#### DASH-02 — Live refresh pause and resume

**Priority:** P2 · **Type:** UI behavior

**Steps**
1. Observe the last-updated indicator and a changing metric/status.
2. Pause automatic refresh from the header.
3. Wait longer than the normal refresh interval.
4. Resume refresh and wait for an update.

**Expected**
- The UI indicates the paused state and automatic values stop refreshing.
- Refresh resumes when re-enabled; manual **Refresh** remains available.

#### DASH-03 — Active query and slow-query display

**Priority:** P2 · **Type:** Positive / controlled database activity

**Preconditions:** Only if an approved QA-only query workload can be generated safely.

**Steps**
1. Run a harmless, bounded query against the disposable QA source that can be observed during the test.
2. Review Active Queries and slow-query diagnostics.
3. If verifying termination, use a known QA query only and confirm the prompt/action before selecting **Kill**.

**Expected**
- Query details appear only when returned by the target and available permissions.
- A termination action affects only the selected QA query and reports its result.

#### DASH-04 — Dashboard navigation and command palette

**Priority:** P2 · **Type:** UI regression

**Steps**
1. Navigate between Console, Overview, Backups, Restore, Restored DBs, Storage, and Activity.
2. Open the command palette with **Ctrl+K** (or **⌘+K** on macOS), search for a QA database/action, and select a result.
3. Switch light/dark theme.

**Expected**
- Each section is reachable and displays content without a broken/blank transition.
- Command search returns relevant actions/targets and the selected command works.
- Theme choice changes appearance without losing the current session.

### C. Remote server and database containers

#### SERVER-01 — Register a verified QA remote server

**Priority:** P1 · **Type:** Positive / SSH integration

**Preconditions:** QA host key independently verified and present in the configured trusted `known_hosts`; Docker available; configured path parents exist; stable server credential encryption key configured.

**Steps**
1. Open **Database Servers** and choose **Add Server**.
2. Enter the QA server label/IP, SSH port, account, password, and absolute QA database/snapshot paths.
3. Register the server.
4. Review the new server card.

**Expected**
- Registration succeeds only after host identity, SSH access, paths, and Docker preflight checks pass.
- The app reports the registered server; registration does not create files, containers, or databases on the remote host.

#### SERVER-02 — Reject untrusted host identity and invalid paths

**Priority:** P1 · **Type:** Negative / security boundary

**Steps**
1. In an isolated QA configuration, attempt registration with an unknown/changed host key.
2. Separately attempt relative paths, path traversal, or a path containing a symlinked component, if safely available in the QA host.

**Expected**
- Registration is rejected with an actionable error.
- Host-key verification is not bypassed and no remote files/containers are created.

#### SERVER-03 — Refresh remote server health

**Priority:** P2 · **Type:** Positive

**Preconditions:** SERVER-01 passed.

**Steps**
1. Review the QA server card's Docker, uptime, load, memory, configured-path disk, and DBOps container information.
2. Trigger **Refresh Health**.
3. Stop Docker or make the QA host temporarily unavailable only if the environment owner approves; then refresh and restore availability.

**Expected**
- Health fields update when the host is available.
- An unreachable host is reported as unavailable/error, not healthy.
- Restoring connectivity permits a later successful health check.

#### SERVER-04 — Provision a new isolated database container

**Priority:** P1 · **Type:** Provisioning / isolated QA only

**Preconditions:** SERVER-01 passed; QA server selected; unique container/database labels and data path; unused port; strong test-only database password; the user has authorization to create a container.

**Steps**
1. Select **Create Database Container** on the QA server and choose an engine.
2. Enter the QA display label, database name, database user, test-only password, and confirmed unused host port.
3. Review the server and resource details, then create the container.
4. Wait for completion and inspect the server card and Targets list.
5. Verify the new target's connection using DBOps and, if permitted, a QA client.
6. Confirm pre-existing QA containers and databases were not changed.

**Expected**
- A new isolated container and new data directory are created under the configured QA root for the selected engine.
- The new PostgreSQL database is registered/selectable in Targets.
- The connection succeeds with the supplied test credentials.
- Existing server containers/databases are untouched.

#### SERVER-05 — Remove server registration

**Priority:** P2 · **Type:** Cleanup / isolated QA

**Preconditions:** Server was registered for this run; provisioned test resources have been independently recorded and cleaned up according to QA policy.

**Steps**
1. Remove the QA server registration from DBOps.
2. Confirm it disappears from the server list.
3. Independently verify server-side data/containers remain unless separately cleaned up by the test owner.

**Expected**
- Only the DBOps server registration is removed.
- Remote containers, databases, files, and snapshots are not implicitly deleted.

### D. Manual snapshot and remote copies

#### SNAP-01 — Create a standard PostgreSQL snapshot

**Priority:** P1 · **Type:** End-to-end

**Preconditions:** DB-01 passed; QA PostgreSQL fixture is populated and its schema/row counts recorded.

**Steps**
1. Select the QA source PostgreSQL target.
2. Click **Take Snapshot**.
3. Confirm the dialog shows the intended target.
4. Choose Custom Archive, compression enabled, encryption disabled for the baseline case, and a QA run-ID description.
5. Leave remote-server copy options unselected for the local-only case.
6. Click **Start Snapshot** and wait for a terminal result.
7. Open **Backups** and locate the snapshot by ID/description.

**Expected**
- Snapshot completes successfully and is listed with the expected source, format, size, timestamp, and status.
- The source fixture schema and data remain unchanged.
- The local archive exists and can be verified.

#### SNAP-02 — Create an encrypted snapshot

**Priority:** P1 · **Type:** Positive / configuration

**Preconditions:** A valid stable `BACKUP_ENCRYPTION_KEY` is configured in QA and protected.

**Steps**
1. Start another QA snapshot.
2. Enable archive encryption and use a unique description.
3. Wait for completion and verify the snapshot.
4. Confirm the archive can be decrypted/used for the supported restore workflow while the correct QA key is configured.

**Expected**
- Encryption-enabled snapshot is clearly recorded and verifies successfully.
- The configured key is not displayed or included in evidence.
- Do not change/remove the key to simulate failure on any shared environment.

#### SNAP-03 — Copy a snapshot to a remote QA server

**Priority:** P1 · **Type:** Replication

**Preconditions:** SERVER-01 passed; completed local QA snapshot exists.

**Steps**
1. Start a manual snapshot and select the QA server as a remote destination.
2. Wait for the local archive and remote copy operation to finish.
3. Review the completion result and server copy status.
4. Verify the remote copy/checksum using the available UI status.
5. Attempt a restore using that verified remote copy into a new disposable QA database (covered further in RESTORE-04).

**Expected**
- Local snapshot remains available.
- Successful copy is recorded only after checksum verification.
- A copy error is visible and does not incorrectly claim remote success or remove the local snapshot.

#### SNAP-04 — Snapshot validation and unsupported target

**Priority:** P2 · **Type:** Negative

**Steps**
1. Attempt to open/start snapshot without selecting a PostgreSQL target, where the UI permits.
2. Select a MongoDB, MySQL/MariaDB, or Redis QA target and inspect snapshot availability.

**Expected**
- Snapshot creation requires a supported PostgreSQL source and gives a clear message otherwise.
- No non-PostgreSQL backup is falsely reported as completed by the PostgreSQL snapshot workflow.

#### SNAP-05 — Snapshot operation failure

**Priority:** P2 · **Type:** Resilience

**Preconditions:** Use a separate QA-only test target that can be made unavailable safely.

**Steps**
1. Begin a snapshot on the isolated QA target.
2. Cause a controlled connection/permission failure using QA-only methods.
3. Observe the result in the dialog, toast, Backups list, and activity feed.

**Expected**
- Failure is reported explicitly with a failed/non-success status.
- No success-shaped snapshot entry or misleading checksum result is presented.
- Restore the QA target to a working state after the test.

### E. Backup schedule

**Current deployment guard:** Run scheduler-changing cases only under the approval and restoration steps in TAIL-06. The current enabled daily schedule resolves to `Primary PostgreSQL (Production)`; do not disable, replace, or leave it pointing at a test target without an approved maintenance window and owner-managed rollback.

#### SCHED-01 — Configure and persist a daily schedule

**Priority:** P1 · **Type:** End-to-end

**Preconditions:** At least one PostgreSQL QA target exists.

**Steps**
1. Open **Backup Schedule**.
2. Select the QA PostgreSQL database.
3. Choose **Every day**, set a QA-approved time in UTC, and verify the displayed Sri Lanka time.
4. Set status to Active and leave destinations local-only for the baseline.
5. Save the schedule, close/reopen the dialog, and inspect the next run.

**Expected**
- The selected source, frequency, time, enabled status, preview, and next-run value are correct and persist after reopening.
- The schedule is independent of the active dashboard target.

#### SCHED-02 — Configure weekly and monthly schedules

**Priority:** P2 · **Type:** Boundary / timezone

**Steps**
1. Configure a weekly schedule and choose a weekday.
2. Confirm UTC and Sri Lanka time/day preview, including a boundary where local date differs if practical.
3. Configure a monthly schedule for day 28 and verify preview.
4. Try to use an unsupported day beyond 28 if exposed through API/configuration.

**Expected**
- Weekly day/time preview is consistent with the configured UTC schedule and timezone conversion.
- Monthly scheduling offers days 1–28.
- Invalid schedules are rejected with an explicit validation result.

#### SCHED-03 — Schedule destinations and execution

**Priority:** P1 · **Type:** End-to-end

**Preconditions:** QA repository is mounted if testing repository copies; SERVER-01 passed if testing server copies; scheduled run time can be observed.

**Steps**
1. Open the schedule and choose the QA PostgreSQL database.
2. Select one available QA mounted repository and/or QA server.
3. Save an Active schedule at a near-future QA-only time.
4. Wait for the schedule to fire; inspect Backups and destination copy status.
5. Compare the scheduled backup source with the selected schedule database, not the currently selected dashboard target.

**Expected**
- A scheduled snapshot is created for the configured database.
- The local copy exists, selected destination copies are attempted, and verified copies show successful checksums.
- Destination failures are surfaced/logged without falsely marking failed copies as available.

#### SCHED-04 — Disable schedule and preserve remote data

**Priority:** P1 · **Type:** Negative / cleanup

**Steps**
1. Set schedule status to Disabled and save.
2. Reopen the dialog and confirm it remains disabled with no next run.
3. Wait beyond the previously configured near-future execution time.

**Expected**
- No scheduled snapshot is created while disabled.
- Existing local/remote snapshots remain; disabling a schedule does not delete them.

### F. Backups section

#### BACKUP-01 — List, filter by target, and inspect snapshot details

**Priority:** P1 · **Type:** Positive

**Preconditions:** At least two QA snapshots exist, preferably from separate QA PostgreSQL targets.

**Steps**
1. Open **Backups** and review rows for backup ID, filename, format, size, status, age, and creation time.
2. Change the selected database target and review the list.
3. Refresh the list.

**Expected**
- Rows show the correct metadata and target association.
- Target filtering/selection behaves consistently with the UI.
- Refresh does not duplicate or alter snapshot records.

#### BACKUP-02 — Verify archive integrity

**Priority:** P1 · **Type:** Positive

**Steps**
1. Select a completed QA snapshot and click **Verify**.
2. Wait for verification to finish.
3. Review checksum and archive/table-of-contents validation results.

**Expected**
- Valid archive reports successful integrity checks without restoring or changing a database.
- Verification result is associated with the correct backup ID.

#### BACKUP-03 — Download a snapshot

**Priority:** P2 · **Type:** Positive

**Steps**
1. Download a completed QA snapshot.
2. Confirm the downloaded file name and non-zero size.
3. If allowed, compare its checksum with the app's recorded checksum.

**Expected**
- The correct archive is downloaded and its bytes/checksum are consistent with the selected snapshot.
- No connection URL, password, or encryption key is included in the filename or response.

#### BACKUP-04 — Handle missing or invalid snapshot

**Priority:** P2 · **Type:** Negative

**Steps**
1. Use the UI to inspect a stale/missing QA archive only if it can be safely isolated, or request a clearly invalid test backup ID via the QA API.
2. Attempt verification/download.

**Expected**
- The app returns a clear not-found/verification failure.
- It does not report a missing archive as valid or offer a corrupt/empty file as a successful download.

#### BACKUP-05 — Delete a disposable snapshot

**Priority:** P1 · **Type:** Destructive action / isolated QA

**Preconditions:** Create a dedicated disposable local-only QA snapshot; ensure no test depends on it and no remote copy is being treated as the only recovery copy.

**Steps**
1. Identify the disposable snapshot by run ID and backup ID.
2. Use its delete action and confirm the prompt.
3. Refresh the Backups list and attempt to access the deleted local archive.
4. If a remote copy was separately made, verify its behavior against the product's documented deletion semantics before deciding whether cleanup is required.

**Expected**
- The app clearly confirms the local deletion and removes the row/local archive as specified.
- Deletion does not drop, alter, or restore a database.
- Any remote-copy retention behavior is recorded; never assume remote files were deleted by local deletion.

### G. Restore Center

#### RESTORE-01 — Restore to a new database (recommended path)

**Priority:** P1 · **Type:** End-to-end / isolated QA

**Preconditions:** SNAP-01 passed; destination server is a disposable QA PostgreSQL server; destination name is confirmed unused; source fixture baseline recorded.

**Steps**
1. In Backups, choose **Restore** for the test snapshot, or open **Restore Center** and select the snapshot.
2. Select the source database and snapshot.
3. Select **Local or mounted repository** as the source copy.
4. Select the QA destination PostgreSQL server/target.
5. Choose **Create a separate new database** and enter a unique unused QA database name.
6. Review the restore plan: source snapshot, destination server, new database name, and effect.
7. Type exactly `RESTORE` and click **Restore Now**.
8. Wait for the job to complete; inspect status, duration, logs, and the new database.
9. Compare the probe row, schema, and row counts against the source baseline.

**Expected**
- Confirmation is required and the plan matches the selected source/destination.
- A new database is created; existing source and destination databases remain unchanged.
- The restored schema/data match the snapshot contents, subject to documented owner/privilege exclusions.
- Restore history records a completed clone restore.

#### RESTORE-02 — Existing-database restore requires explicit confirmation

**Priority:** P1 · **Type:** Destructive action / isolated QA only

**Preconditions:** A snapshot exists; the selected existing target is disposable, pre-populated with known marker data, and approved for modification.

**Steps**
1. Open Restore Center and select the QA snapshot and existing QA target.
2. Choose **Write into an existing database**.
3. Review the warning and restore plan.
4. Try to submit with no phrase and with `RESTORE`; confirm both are rejected.
5. Enter exactly `RESTORE EXISTING` and submit.
6. Wait for terminal status and inspect the target data and restore log.

**Expected**
- Existing-database restore requires the exact `RESTORE EXISTING` phrase.
- The UI clearly warns that the selected database will be changed.
- Successful restore writes snapshot objects/data to the selected QA database; document observed behavior for objects not present in the snapshot.
- No production/shared database is used.

#### RESTORE-03 — Reject invalid restore selections and confirmation

**Priority:** P1 · **Type:** Negative

**Steps**
1. Open Restore Center with no completed snapshot selected and attempt to continue.
2. Try a non-PostgreSQL destination, if one appears.
3. Try an existing-database restore from a remote server copy.
4. Try a new-database name that already exists in the QA server.
5. Enter an incorrect confirmation phrase.

**Expected**
- Each unsupported/invalid combination is blocked with a clear error before an unsafe operation starts.
- Remote server copies are limited to new-database restores.
- Existing database names are not silently overwritten during clone restore.

#### RESTORE-04 — Restore using a verified remote copy

**Priority:** P1 · **Type:** End-to-end

**Preconditions:** SNAP-03 passed and remote copy shows checksum-verified/available.

**Steps**
1. In Restore Center, select the snapshot and choose its verified QA remote-server copy.
2. Select a new, unused destination database name on a QA PostgreSQL destination.
3. Review plan, type `RESTORE`, and start.
4. Wait for completion and validate the restored fixture data.

**Expected**
- Only a verified available remote copy is selectable.
- The new database matches the fixture snapshot.
- Existing-database mode is unavailable/rejected for remote copies.

#### RESTORE-05 — Restore job status, history, and logs

**Priority:** P2 · **Type:** Positive / observability

**Steps**
1. Open the Restore section and inspect a completed and (if safely obtainable) failed QA restore.
2. Refresh restore history.
3. Open the job details/logs.

**Expected**
- Restore ID, backup, mode, destination, status, duration, and start time correspond to the job.
- Logs/status explain success or failure without exposing credentials.
- Refresh does not create another restore.

#### RESTORE-06 — Restore failure does not appear successful

**Priority:** P2 · **Type:** Resilience

**Preconditions:** Use a separate QA-only failure setup (for example a destination deliberately made unavailable); do not corrupt a retained test archive.

**Steps**
1. Initiate a new-database restore to the isolated failing QA destination.
2. Observe UI result, restore history, and logs.
3. Restore service availability and verify the source snapshot is still available.

**Expected**
- Failure is explicitly recorded and is not shown as completed.
- Existing source data and the valid source snapshot remain intact.
- Any partially created test resources are identified for manual QA cleanup.

### H. Restored Databases

#### RDB-01 — Add a clone restore to Targets

**Priority:** P1 · **Type:** Positive

**Preconditions:** RESTORE-01 completed.

**Steps**
1. Open **Restored DBs**.
2. Find the completed clone restore and click **Add to Targets** if it is not already registered.
3. Confirm its monitoring state changes to **In Targets** and it appears in target selection.
4. Select it and verify dashboard connectivity.

**Expected**
- The restored database is added as a monitored target and is reachable.
- The clone restore itself is not repeated by registering the database.

#### RDB-02 — Role-based full connection URL access

**Priority:** P1 · **Type:** Authorization

**Steps**
1. As administrator, open Restored DBs and use **Copy full URL** for a QA restored target.
2. Confirm the copied URL works only against the QA database; redact it immediately from clipboard/evidence.
3. Sign in as non-admin and inspect the same row/action.

**Expected**
- Administrator can copy the URL.
- Non-admin cannot retrieve/copy the credential-bearing URL and is directed to ask an administrator.
- Evidence and logs do not contain the full URL.

#### RDB-03 — Remove restored target from monitoring

**Priority:** P1 · **Type:** Isolated QA cleanup

**Steps**
1. Use **Remove from Targets** on the disposable restored QA database.
2. Confirm it is no longer monitored.
3. Independently verify the database and its data still exist on the QA server.

**Expected**
- Only its app target registration is removed; the database/data remain.

### I. Storage section and snapshot repositories

#### STORAGE-01 — Database storage metrics

**Priority:** P1 · **Type:** Positive

**Steps**
1. Select the QA PostgreSQL target.
2. Navigate to **Storage**.
3. Review the Top Tables by Size section (name, total, data, and index sizes).
4. Refresh metrics and compare with the known QA fixture where practical.

**Expected**
- Storage rows belong to the selected database and use understandable size units.
- Empty databases show an empty state; query/permission failures are not presented as zero-size success.

#### STORAGE-02 — Lock diagnostics

**Priority:** P2 · **Type:** Positive / safe QA workload

**Preconditions:** Only if a controlled, bounded lock test is approved for the disposable database.

**Steps**
1. Review Lock Contention Diagnostics with no lock workload.
2. If approved, create a brief QA-only waiting-lock scenario using two QA sessions and release it promptly.
3. Refresh diagnostics and confirm the waiting state clears after release.

**Expected**
- Clear lock state is shown when no waits exist.
- A controlled waiting lock is identified and disappears after release.
- Do not leave an open transaction or lock after the case.

#### STORAGE-03 — Mounted repository appears as a schedule destination

**Priority:** P1 · **Type:** Integration

**Preconditions:** A QA repository is mounted/configured and available.

**Steps**
1. Open Backup Schedule and inspect **Mounted repositories**.
2. Select the QA repository and save a schedule or execute the approved replication workflow.
3. Verify the copy's checksum/status and destination path using approved QA tools.

**Expected**
- Available repository is selectable; unavailable repositories are clearly marked/disabled.
- A successful copy is recorded only after verification.

#### STORAGE-04 — Unavailable repository/copy failure

**Priority:** P2 · **Type:** Negative

**Preconditions:** Separate QA repository that can be safely made unavailable.

**Steps**
1. Make the test repository unavailable using an approved QA procedure.
2. Refresh repository destinations and/or initiate a QA copy.
3. Restore the mount and refresh again.

**Expected**
- Unavailable destination is not falsely shown as mounted/verified.
- Copy failure is explicit; the local source snapshot remains available.

### J. Notifications and activity

**Current deployment guard:** The current Telegram configuration is already enabled. Run notification mutation or delivery cases only with a confirmed QA-only recipient. Do not overwrite the saved configuration or send a test event to an unconfirmed destination.

#### NOTIFY-01 — Save Telegram settings and verify masking

**Priority:** P1 · **Type:** Positive / secret handling

**Preconditions:** Run on a separate QA deployment with test-only bot credentials, or with explicit owner approval to change and restore the current configuration. The current token is masked and must not be overwritten unless its replacement/rollback is planned.

**Steps**
1. On the isolated QA deployment, open **Alerts** or **Notifications**.
2. Set delivery enabled, sound preference, test bot token/chat ID, and selected event subscriptions.
3. Save Settings.
4. Reopen the dialog and inspect the token field/hint.

**Expected**
- Configuration persists, event subscription controls retain their state, and the saved token is masked.
- No token is exposed in UI evidence, network logs, or test report.

#### NOTIFY-02 — Send test notification

**Priority:** P1 · **Type:** External integration

**Steps**
1. With approved test-only Telegram configuration, use **Send Test**.
2. Confirm a message arrives at the QA chat.
3. Repeat with delivery disabled, or an invalid test-only configuration, if safe and approved.

**Expected**
- Enabled, valid configuration delivers one test message and reports success.
- Disabled/invalid delivery is reported explicitly; the UI does not claim successful delivery.

#### NOTIFY-03 — Event subscriptions and silent mode

**Priority:** P2 · **Type:** Integration

**Preconditions:** Isolated QA Telegram destination and a specific event that can be safely generated in QA. Do not alter the current deployment's saved token/subscriptions without owner approval.

**Steps**
1. Enable one test event subscription, save, and generate that event in QA.
2. Disable the subscription, save, and generate the event again.
3. If relevant to the environment, repeat with silent mode selected.

**Expected**
- Enabled subscription sends the configured event; disabled subscription does not.
- Silent setting is persisted and respected by the integration when supported by the chat/client.

#### NOTIFY-04 — Migration history alert baseline

**Priority:** P2 · **Type:** Integration / read-only behavior

**Preconditions:** Recognized migration ledger on disposable PostgreSQL QA database; test-only alert destination.

**Steps**
1. Enable **New migration detected** and save.
2. Allow the initial scan to establish a baseline.
3. Add a valid new migration ledger row using the QA application's approved migration mechanism.
4. Wait for the documented polling interval and inspect Telegram, Migration History, and activity.

**Expected**
- Existing baseline rows do not trigger a false initial alert.
- A new recognized ledger record appears in read-only history and triggers an alert when subscribed.
- DBOps does not run or alter the migration itself.

#### ACT-01 — Activity feed records QA operations

**Priority:** P2 · **Type:** Positive

**Steps**
1. Perform safe QA actions such as target switch, ping, snapshot, verification, and completed restore.
2. Open Activity and review operator grouping, recent events, and counts.
3. Expand older events if the history control is available.

**Expected**
- Relevant actions appear with sensible labels, timestamps, and operator grouping.
- Activity does not expose passwords, full connection URLs, tokens, or MFA setup keys.

#### ACT-02 — Clear activity

**Priority:** P2 · **Type:** Destructive UI action / QA only

**Steps**
1. Confirm the feed contains only QA test events.
2. Click **Clear Log** and confirm if prompted.
3. Refresh/reopen the section.

**Expected**
- The QA activity feed is cleared as specified and empty-state/counts update consistently.
- Clearing activity does not delete snapshots, databases, restore jobs, or remote resources.

## 7. Suggested execution order

Run in this order to reuse artifacts and reduce risk:

1. TAIL-01 through TAIL-05
2. TAIL-06 manual remote copy; run scheduled execution only under its explicit approval gate
3. TAIL-07 through TAIL-08
4. TAIL-09 and TAIL-10, preserving the existing schedule and Telegram configuration
5. AUTH-01 through AUTH-04
6. DB-01 through DB-03
7. DASH-01 through DASH-04
8. SERVER-01 through SERVER-04
9. SNAP-01, SNAP-02, and SNAP-03
10. SCHED-01 through SCHED-04 only on an isolated QA deployment or under TAIL-06 approval/rollback
11. BACKUP-01 through BACKUP-05
12. RESTORE-01, RESTORE-04, and RESTORE-05; run RESTORE-02 only on its dedicated disposable target
13. RDB-01 through RDB-03
14. STORAGE-01 through STORAGE-04
15. NOTIFY-01 through NOTIFY-04 only with an approved QA Telegram recipient; then ACT-01 through ACT-02
16. Clean up QA resources and run final smoke checks

## 8. Cleanup checklist

- [ ] Restore the approved pre-test schedule state; do not leave the production-impacting baseline disabled or pointing at the temporary QA database.
- [ ] Restore pre-test Telegram settings if they were changed under approval; revoke test credentials if required. Do not delete or replace the existing configuration as routine cleanup.
- [ ] Preserve required test evidence without secrets.
- [ ] Retain the baseline snapshot until all restore cases and data comparisons are complete.
- [ ] Remove disposable snapshots only after confirming no test depends on them.
- [ ] Remove restored targets from DBOps if no longer needed; confirm databases remain before separately deleting them under the QA owner's cleanup procedure.
- [ ] Remove provisioned QA containers/databases and their directories only through the approved QA host cleanup procedure; DBOps server unregistration does not perform this cleanup.
- [ ] Remove disposable database registrations and remote server registrations from DBOps after recording results.
- [ ] Confirm production resources were not selected or changed.
- [ ] Record blocked/not-run cases and defects with run ID, test ID, redacted evidence, and reproduction steps.

## 9. Exit criteria

- All P1 cases pass, or each failure has a defect with owner and disposition.
- No unresolved data-integrity, authorization, credential-exposure, or misleading-success defect remains.
- Restore comparisons match the source snapshot for the recorded fixture, with documented exclusions such as owner/privilege settings.
- Cleanup is complete or remaining QA resources have an owner and explicit expiry.
- The run sheet and evidence are stored in the approved QA location.
