# Live verification in GitHub Codespaces (and Actions)

The development laptop cannot run the testbed (ADR 0004). Every live check — the 10-minute
baseline, the deploy round-trip and scenarios 1–5 — runs in a **GitHub Codespace** (4-core,
16 GB RAM) or in the manual **`testbed-live`** GitHub Actions workflow.

Replace `OWNER/REPO` below with your repository (for example `Abhi7488-VIT/SentinelAI`).

---

## (a) Create a 4-core Codespace on `phase-2-testbed` and open a terminal

**In the browser**

1. Open `https://github.com/OWNER/REPO`.
2. Click **Code** → **Codespaces** tab → **⋯** (three dots) → **New with options…**
3. Set:
   - **Branch:** `phase-2-testbed`
   - **Dev container configuration:** `SentinelAI (testbed + backend)` (`.devcontainer/devcontainer.json`)
   - **Machine type:** **4-core** (16 GB RAM)
4. Click **Create codespace**. The first build takes several minutes; the creation log ends with
   `[post-create] done. Nothing was started.` Nothing starts automatically.
5. Open a terminal: menu **☰ → Terminal → New Terminal** (or <kbd>Ctrl</kbd>+<kbd>`</kbd>).

**Or from your laptop's terminal (GitHub CLI)**

```bash
gh codespace create --repo OWNER/REPO --branch phase-2-testbed --machine standardLinux32gb
gh codespace list                      # note the NAME of the new codespace
gh codespace ssh -c NAME               # terminal in the codespace
# or open it in the browser:  gh codespace code -c NAME --web
```

(`standardLinux32gb` is the 4-core / 16 GB machine; the `32gb` is its disk.)

Check the tools in the Codespace terminal:

```bash
docker version --format '{{.Server.Version}}' && uv --version && make --version | head -1 && claude --version
PYTHONPATH=testbed python3 -m harness.memguard check   # free-memory check (floor 1024 MiB)
```

## (b) Log in to Claude Code in the Codespace

```bash
claude
```

On first start Claude Code asks how to sign in. Choose your Claude account, open the link it prints
in your browser, approve, and paste the code back into the terminal if it asks. (Alternative: use
an API key by storing `ANTHROPIC_API_KEY` as a **Codespaces secret** — GitHub → Settings → Codespaces
→ Secrets → New secret, allowed for this repository — then rebuild/restart the codespace. Never put
a key in the repository or in `.devcontainer/`.)

## (c) Resume prompt for the Codespace session

Paste this into Claude Code in the Codespace:

```text
Read CLAUDE.md, docs/PROGRESS.md and docs/plans/phase-2.md first.

You are in a GitHub Codespace (4-core / 16 GB) on branch phase-2-testbed. Live verification is
allowed here (ADR 0004); the laptop rules about not starting containers do not apply in this
Codespace. Keep the memory watchdog on (TB_MIN_AVAILABLE_MB=1024).

1. Start the testbed: `make testbed-up` and confirm every container is healthy.
2. Run the 10-minute baseline:
   cd testbed && python3 -m harness.memguard watch --on-breach "make -C .. testbed-down" -- \
     python3 -m harness.live baseline --minutes 10 --out runs
   Then the deploy round-trip:
   python3 -m harness.memguard watch --on-breach "make -C .. testbed-down" -- \
     python3 -m harness.live deploy-check --out runs
3. Quote the summary tables and the key Prometheus, Loki and Jaeger query outputs from
   testbed/runs/<check>/latest.json and its queries/ folder. Fix whatever fails (run ruff,
   mypy --strict and the unit tests before every commit).
4. When both checks pass, update docs/PROGRESS.md (T5: verified live, with the run ids and
   numbers) and commit "T5 verified live".
5. Continue Phase 2 T6–T10 from docs/plans/phase-2.md: controller + ground truth, scenario 1
   (DB pool exhaustion) end to end, scenarios 2–5, reproducibility, CI and docs. Verify every
   scenario live and quote real Prometheus, Loki and Jaeger query output as evidence. Commit
   after each green task. Add each scenario to harness.live and testbed-live.yml as it lands.
6. Never call anything "verified" that did not run live. Run `make testbed-down` when you
   finish a check. At the end of the phase, write the phase report and stop.
```

## (d) Keep costs down

- **Stop the Codespace whenever you are not using it.** A running Codespace uses your compute
  allowance even when idle: `gh codespace stop -c NAME` (or the Codespaces page → ⋯ → Stop).
  Run `make testbed-down` first.
- **Lower the idle timeout:** GitHub → Settings → Codespaces → **Default idle timeout** → 10–15
  minutes.
- **Delete the Codespace when the work is merged:** `gh codespace delete -c NAME`. Stopped
  Codespaces still use storage allowance until deleted.
- **One Codespace at a time,** and use the 4-core machine, not larger.
- **The free allowance is limited** (a monthly quota of core-hours and storage for personal
  accounts). Check what is left at GitHub → Settings → **Billing and licensing** before long
  sessions, and set a spending limit of 0 if you never want to pay.
- **Actions alternative:** the `testbed-live` workflow runs one check per job and always tears
  down. Actions minutes are also quota-limited on private repositories.

## Running a check with GitHub Actions instead

GitHub only allows manual dispatch of a workflow whose file exists on the **default branch**.
`testbed-live.yml` is on `phase-2-testbed` until that branch is merged into `main`. Once it is on
`main`:

```bash
gh workflow run testbed-live.yml --ref phase-2-testbed -f scenario_id=baseline -f duration_minutes=10
gh run watch                     # follow it
gh run download                  # evidence: testbed/runs/ (run record, query outputs, logs)
```

The job summary shows the result table; the artifact holds every query and its raw response.
