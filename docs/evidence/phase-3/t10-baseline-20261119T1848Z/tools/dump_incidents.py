"""Read-only dump of SentinelAI incidents for a scenario run, via the public API (ADR 0003).

    cd testbed && uv run python DUMP.py RUN_ID OUT_DIR

Writes OUT_DIR/sentinelai-incident-<n>.json for every incident in the run window and prints a
readable summary (services, detections, grouping reasons, explanations, readability problems).
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import httpx

from harness.evaluate import SentinelApi, credentials, explanation_problems, ts

TESTBED = Path.cwd()  # run from testbed/ with PYTHONPATH=.
run_id, out = sys.argv[1], Path(sys.argv[2])
record = (
    Path(run_id)
    if run_id.endswith(".json")
    else next((TESTBED / "runs").glob(f"*/{run_id}/run-record.json"))
)
rec = json.loads(record.read_text())
start = ts(rec["run"]["baseline_started_at"])
end = ts(rec["run"]["recovery_ended_at"]) + timedelta(seconds=60)
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
    api = SentinelApi(client)
    api.login(*credentials(TESTBED.parent / ".env"))
    data = api.collect("shop", start, end)
for inc in data.incidents:
    (out / f"sentinelai-incident-{inc['number']}.json").write_text(json.dumps(inc, indent=1) + "\n")
    print(
        f"#{inc['number']} {inc['title']} | {inc['severity']} {inc['status']} | "
        f"started {inc['started_at']} detected {inc['detected_at']} created {inc['created_at']}"
    )
    print(f"services: {[s['name'] for s in inc['services']]}")
    for m in inc["detections"]:
        d = m["detection"]
        name = data.services[d["service_id"]]
        print(
            f"\n- {name} {d['rule']} ({d['severity']}) started {d['started_at'][11:19]} "
            f"fired {d['fired_at'][11:19]} score={m.get('score')}"
        )
        for r in m.get("reasons") or []:
            print(f"    reason {r['type']}: {r['detail']}")
        print(f"    explanation: {d['explanation']}")
        problems = explanation_problems(name, d["explanation"])
        print(f"    readability problems: {', '.join(problems) if problems else 'none'}")
print(f"\nincidents in run window: {len(data.incidents)}; detections in run window: "
      f"{len(data.detections)}")
for d in data.detections:
    print(f"  {data.services[d['service_id']]} {d['rule']} started {d['started_at']} "
          f"fired {d['fired_at']}")
