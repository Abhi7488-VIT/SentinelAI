"""Summarise detection ticks from worker JSON logs on stdin whose eval_end is in [start, end).

    docker compose logs worker --no-log-prefix --since X | python ticks.py START END
"""

import json
import statistics
import sys
from datetime import datetime

start, end = (datetime.fromisoformat(a.replace("Z", "+00:00")) for a in sys.argv[1:3])
ticks, problems = [], []
for line in sys.stdin:
    try:
        rec = json.loads(line)
    except ValueError:
        continue
    lvl = rec.get("level")
    if rec.get("event") == "detection_tick_done":
        if start <= datetime.fromisoformat(rec["eval_end"]) < end:
            ticks.append(rec)
    elif lvl in ("warning", "error", "critical") or rec.get("event") == "detection_tick_skipped":
        ts = rec.get("timestamp", "")
        if start.isoformat() <= ts.replace("Z", "+00:00") < end.isoformat():
            problems.append(rec)
d = sorted(t["duration_ms"] for t in ticks)
ends = [t["eval_end"] for t in ticks]
print(
    f"ticks={len(ticks)} evaluated_total={sum(t['evaluated'] for t in ticks)} "
    f"fired={sum(t['fired'] for t in ticks)} "
    f"incidents_created={sum(t['incidents_created'] for t in ticks)}"
)
if d:
    p95 = d[max(0, round(0.95 * len(d)) - 1)]
    print(f"duration_ms p50={statistics.median(d):.0f} p95={p95:.0f} max={d[-1]:.0f}")
    print(f"eval_end first/last: {min(ends)} {max(ends)} distinct: {len(set(ends))}")
print(f"warnings/errors/skips in window: {len(problems)}")
for p in problems[:10]:
    print("  ", json.dumps(p)[:300])
