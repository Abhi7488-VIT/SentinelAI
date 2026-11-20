#!/usr/bin/env bash
# Sample detector_states every 5 s for DURATION seconds; print every row whose state is not `ok`
# OR whose last condition is not `clear` (so pending, neutral, no_data, firing all show).
# Usage: sample_states.sh DURATION OUT_FILE
set -u
dur=$1 out=$2
psql=(docker compose -f /workspaces/SentinelAI/docker-compose.yml exec -T postgres
      psql -U sentinel -d sentinel -At -F ' | ')
: >"$out"
end=$(( $(date +%s) + dur )) n=0
while [ "$(date +%s)" -lt "$end" ]; do
  "${psql[@]}" -c "SELECT now(), s.name, d.rule, d.state, d.last_cond, d.last_value
    FROM detector_states d JOIN services s ON s.id = d.service_id
    WHERE d.state <> 'ok' OR d.last_cond <> 'clear' ORDER BY 2, 3" >>"$out"
  n=$((n + 1))
  sleep 5
done
echo "samples=$n non_ok_rows=$(wc -l <"$out")"
