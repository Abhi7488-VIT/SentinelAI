"""Export a scenario run's evidence for committing under docs/evidence/.

    python -m harness.evidence RUNS_DIR SCENARIO_ID RUN_ID OUT_DIR

Copies the run record and every Prometheus/Loki query output verbatim. Jaeger responses are
1-2 MB each, so each is replaced by a digest: per matched trace its id, span counts, the status
codes and error-span count of the queried service, plus the SHA-256 and size of the raw response
(the raw file stays in the gitignored runs directory and in CI artifacts).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


def jaeger_digest(raw: bytes) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(raw)
    service = data["query"]["service"]
    traces = []
    for tr in data["response"].get("data") or []:
        procs = tr.get("processes", {})
        spans = [
            s
            for s in tr.get("spans", [])
            if procs.get(s["processID"], {}).get("serviceName") == service
        ]
        tags = [{t["key"]: t["value"] for t in s.get("tags", [])} for s in spans]
        traces.append(
            {
                "traceID": tr["traceID"],
                "spans": len(tr.get("spans", [])),
                "service_spans": len(spans),
                "service_status_codes": sorted(
                    {
                        str(t["http.response.status_code"])
                        for t in tags
                        if "http.response.status_code" in t
                    }
                ),
                "service_error_types": sorted(
                    {str(t["error.type"]) for t in tags if "error.type" in t}
                ),
                "service_error_spans": sum(1 for t in tags if t.get("error") in (True, "true")),
                "start_us": min((s["startTime"] for s in spans), default=None),
            }
        )
    return {
        "backend": "jaeger",
        "query": data["query"],
        "params": data["params"],
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": len(raw),
        "traces": traces,
    }


def export(runs_dir: Path, scenario_id: str, run_id: str, out_dir: Path) -> list[Path]:
    record = runs_dir / scenario_id / f"{run_id}.json"
    queries = runs_dir / scenario_id / run_id / "queries"
    if not record.is_file():
        raise FileNotFoundError(record)
    (out_dir / "queries").mkdir(parents=True, exist_ok=True)
    written = [out_dir / "run-record.json"]
    shutil.copyfile(record, written[0])
    for src in sorted(queries.glob("*.json")):
        if src.name.endswith("-jaeger.json"):
            dst = out_dir / "queries" / src.name.replace(".json", ".digest.json")
            dst.write_text(json.dumps(jaeger_digest(src.read_bytes()), indent=1), encoding="utf-8")
        else:
            dst = out_dir / "queries" / src.name
            shutil.copyfile(src, dst)
        written.append(dst)
    return written


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        sys.stderr.write(str(__doc__) + "\n")
        return 2
    runs_dir, scenario_id, run_id, out_dir = args
    files = export(Path(runs_dir), scenario_id, run_id, Path(out_dir))
    sys.stdout.write(f"{len(files)} files -> {out_dir}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
