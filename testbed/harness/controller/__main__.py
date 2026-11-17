"""Controller CLI (inside the controller container: `tb.sh scenario ...` runs this).

    python -m harness.controller list
    python -m harness.controller validate
    python -m harness.controller run ID [--seed N] [--baseline S] [--observe S] [--recovery S]
                                        [--dry-run]
    python -m harness.controller start ID [--seed N]     # inject and leave in place (manual)
    python -m harness.controller stop ID                 # end an active run / remove the fault
    python -m harness.controller status
    python -m harness.controller compare A.json B.json   # fingerprint comparison
    python -m harness.controller serve [--port 8090]

Exit codes: 0 ok / verified, 1 run failed or aborted, 2 usage error, 4 busy (another run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from harness.controller import groundtruth, service
from harness.controller.checks import compare_fingerprints
from harness.controller.lock import RunLock
from harness.controller.plugin import discover
from harness.controller.primitives import ControllerSettings
from harness.controller.runner import RunOptions


def _out(obj: Any) -> None:
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj, indent=2)) + "\n")
    sys.stdout.flush()


def summary(record: dict[str, Any]) -> str:
    run = record["run"]
    lines = [
        f"scenario {record['id']}  run {run['run_id']}  seed {run['seed']}"
        f"  -> {run['status'].upper()}",
        f"  fault: {record['timeline']['started_at']} -> {record['timeline']['ended_at']}",
    ]
    for title, key in (
        ("baseline", "baseline_checks"),
        ("symptoms", "observed"),
        ("recovery", "recovery"),
    ):
        for o in run[key]:
            peak = "-" if o["peak"] is None else f"{o['peak']:.4g}"
            onset = "-" if o["onset_s"] is None else f"{o['onset_s']:.0f}s"
            mark = "ok " if o["observed"] else "NO "
            lines.append(
                f"  {title:9} {mark} {o['id']:32} peak={peak:>9} onset={onset:>6}  {o['detail']}"
            )
    lines += [f"  reason: {r}" for r in run["reasons"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="harness.controller")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("validate")
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("id")
    r.add_argument("--seed", type=int)
    r.add_argument("--baseline", type=int, default=120)
    r.add_argument("--observe", type=int, default=300)
    r.add_argument("--recovery", type=int, default=180)
    r.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("start")
    s.add_argument("id")
    s.add_argument("--seed", type=int)
    t = sub.add_parser("stop")
    t.add_argument("id")
    c = sub.add_parser("compare")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8090)
    args = p.parse_args(argv)
    settings = ControllerSettings.from_env()

    if args.cmd == "list":
        _out(service.list_scenarios(settings))
        return 0
    if args.cmd == "validate":
        ids = groundtruth.definition_ids(settings.scenarios_dir)
        plugins = discover()
        for sid in ids:
            groundtruth.load_definition(settings.scenarios_dir, sid)
        missing = sorted(set(ids) ^ set(plugins))
        _out({"definitions": ids, "plugins": sorted(plugins), "unmatched": missing})
        return 0 if not missing else 1
    if args.cmd == "status":
        _out({"active": RunLock(settings.runs_dir).current()})
        return 0
    if args.cmd == "compare":
        a, b = (json.loads(x.read_text(encoding="utf-8")) for x in (args.a, args.b))
        diffs = compare_fingerprints(a["run"]["fingerprint"], b["run"]["fingerprint"])
        _out({"a": a["run"]["fingerprint"], "b": b["run"]["fingerprint"], "differences": diffs})
        return 0 if not diffs else 1
    if args.cmd == "serve":
        import uvicorn

        from harness.controller.api import build_app

        uvicorn.run(build_app(settings), host="0.0.0.0", port=args.port, log_level="warning")  # noqa: S104
        return 0

    # `stop` has no --seed option (it removes whatever the active run injected).
    arg_seed = getattr(args, "seed", None)
    seed = settings.default_seed if arg_seed is None else arg_seed
    try:
        if args.cmd == "run":
            options = RunOptions(
                seed=seed,
                baseline_s=args.baseline,
                observe_s=args.observe,
                recovery_s=args.recovery,
                mode="dry_run" if args.dry_run else "full",
            )
            run, lock = service.prepare_run(settings, args.id, options)
            _out(
                f"[controller] run {run.run_id}: {args.id} seed={seed} "
                f"baseline={args.baseline}s observe={args.observe}s recovery={args.recovery}s"
            )
            record = asyncio.run(
                service.execute(
                    run, lock, on_step=lambda st: _out(f"[{st['at']}] {st['step']}: {st['detail']}")
                )
            )
            _out(summary(record))
            ok = record["run"]["status"] in ("verified", "dry_run")
            return 0 if ok else 1
        if args.cmd == "start":
            _out(asyncio.run(service.manual_start(settings, args.id, seed))["run"])
            return 0
        if args.cmd == "stop":
            _out(asyncio.run(service.manual_stop(settings, args.id)))
            return 0
    except service.BusyError as busy:
        _out(f"[controller] busy: {busy.holder}")
        return 4
    except (KeyError, ValueError) as exc:
        _out(f"[controller] error: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
