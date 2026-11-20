-- T10 quiet-baseline queries (same measures as docs/evidence/phase-3/t10-quiet-baseline.md).
-- psql -v ws='<window start>' -v we='<window end>' -f baseline.sql
\pset footer on
\echo '== detections created in the window (+60 s for the 30 s evaluation lag)'
SELECT s.name, d.rule, d.severity, d.started_at, d.fired_at, d.explanation
FROM detections d JOIN services s ON s.id = d.service_id
WHERE d.created_at >= :'ws' AND d.created_at < CAST(:'we' AS timestamptz) + interval '60 s'
ORDER BY d.started_at;

\echo '== detections with started_at in the window (any created_at)'
SELECT count(*) FROM detections
WHERE started_at >= :'ws' AND started_at < :'we';

\echo '== detections total (all time; 7 are from S1 run 20261119T171535Z-e076ed)'
SELECT count(*) FROM detections;

\echo '== incidents created in the window (+60 s)'
SELECT number, title, severity, status, created_at FROM incidents
WHERE created_at >= :'ws' AND created_at < CAST(:'we' AS timestamptz) + interval '60 s';

\echo '== incidents total (all time; #1 is from S1 run 20261119T171535Z-e076ed)'
SELECT count(*) FROM incidents;

\echo '== per service over the window''s evaluation buckets: traffic and headroom to thresholds'
SELECT s.name, count(*) AS buckets, sum(w.requests) AS requests, sum(w.errors) AS errors,
       min(w.requests_60s) AS min_req_60s,
       round(max(w.errors_60s::numeric / nullif(w.requests_60s, 0)), 4) AS max_err_ratio_60s,
       round(max(w.p95_60s_ms)::numeric, 1) AS max_p95_60s_ms,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY w.p95_60s_ms))::numeric, 1)
         AS median_p95_60s_ms
FROM service_window_stats w JOIN services s ON s.id = w.service_id
WHERE w.bucket_start >= :'ws' AND w.bucket_start < :'we'
GROUP BY s.name ORDER BY s.name;

\echo '== exception signatures seen in the window'
SELECT s.name, e ->> 'type' AS type, e ->> 'message' AS message, sum((e ->> 'count')::int) AS spans
FROM service_window_stats w JOIN services s ON s.id = w.service_id,
     jsonb_array_elements(w.exceptions) e
WHERE w.bucket_start >= :'ws' AND w.bucket_start < :'we'
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC;

\echo '== detector states now'
SELECT s.name, d.rule, d.state, d.last_cond, round(d.last_value::numeric, 4) AS last_value
FROM detector_states d JOIN services s ON s.id = d.service_id ORDER BY 1, 2;

\echo '== latency headroom at window end: baseline = median per-minute p95 over the 30 min before'
\echo '   the last 60-s window (minutes with >= 20 requests); fire threshold = max(3x, +100 ms)'
WITH base AS (
  SELECT w.service_id, count(*) AS minutes,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY w.p95_60s_ms) AS ms
  FROM service_window_stats w
  WHERE w.bucket_start > CAST(:'we' AS timestamptz) - interval '31 min'
    AND w.bucket_start <= CAST(:'we' AS timestamptz) - interval '1 min'
    AND extract(second FROM w.bucket_start + interval '15 s') = 0
    AND w.requests_60s >= 20
  GROUP BY w.service_id
), win AS (
  SELECT service_id, max(p95_60s_ms) AS mx FROM service_window_stats
  WHERE bucket_start >= :'ws' AND bucket_start < :'we' GROUP BY service_id
)
SELECT s.name, b.minutes AS baseline_minutes, round(b.ms::numeric, 1) AS baseline_ms,
       round(greatest(3 * b.ms, b.ms + 100)::numeric, 1) AS fire_ms,
       round(x.mx::numeric, 1) AS window_max_p95_ms,
       round((x.mx / greatest(3 * b.ms, b.ms + 100))::numeric, 2) AS fraction_of_threshold
FROM base b JOIN win x USING (service_id) JOIN services s ON s.id = b.service_id ORDER BY 1;

\echo '== DB pool headroom in the window (max used/max, max pending; fire needs >= 0.9 AND pending > 0)'
SELECT s.name,
       max(p.value) FILTER (WHERE m.name = 'db.client.connection.count'
                              AND m.attributes ->> 'db.client.connection.state' = 'used') AS max_used,
       max(p.value) FILTER (WHERE m.name = 'db.client.connection.max') AS pool_max,
       max(p.value) FILTER (WHERE m.name = 'db.client.connection.pending_requests') AS max_pending
FROM metric_points p JOIN metric_series m ON m.org_id = p.org_id AND m.id = p.series_id
JOIN services s ON s.id = m.service_id
WHERE p.ts >= :'ws' AND p.ts < :'we' AND m.name LIKE 'db.client.connection.%'
GROUP BY s.name ORDER BY s.name;

\echo '== instances first seen in the window (crash loop needs >= 3 in 5 min)'
SELECT count(*) FROM service_instances WHERE first_seen_at >= :'ws' AND first_seen_at < :'we';
