# T0 S2 attempt 1 — aborted before the fault (run `20261120T101021Z-970f78`)

Not a result. The harness controller failed at 10:10:21Z, before injecting anything:
`InvalidPasswordError('password authentication failed for user "testbed"')`
(`harness-run.txt`). The testbed stayed at baseline, so SentinelAI saw nothing to detect, and
`evaluation.txt` holds only my runner's own bug (it passed `<run_id>.json` as the run id; fixed in
`scripts/t0-run-scenario.sh`).

Cause: the stored password of the testbed's Postgres superuser no longer matched
`POSTGRES_PASSWORD` in `testbed/.env`. Postgres applies that variable only when its data volume is
first created (`sentinelai-testbed_pgdata`, 2026-11-17); `testbed/.env` was last written on
2026-11-19 11:40. S1 (a traffic fault) never logs in as the superuser, so the mismatch first showed
up in S2, the first scenario since then that applies an allow-listed SQL change.

Fix (10:13Z): `ALTER ROLE testbed PASSWORD …` with the value from the container's own
environment, over the container's local socket (value never printed). Verified: the controller
logs in. No SentinelAI code or data changed; the shop services use their own database users and
were unaffected.
