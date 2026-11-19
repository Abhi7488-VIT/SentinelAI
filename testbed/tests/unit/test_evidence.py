import json
from pathlib import Path

from harness.evidence import export, jaeger_digest


def _jaeger_file() -> bytes:
    trace = {
        "traceID": "abc",
        "processes": {"p1": {"serviceName": "order-service"}, "p2": {"serviceName": "frontend"}},
        "spans": [
            {
                "processID": "p1",
                "startTime": 10,
                "tags": [
                    {"key": "http.response.status_code", "value": 503},
                    {"key": "error", "value": True},
                    {"key": "error.type", "value": "503"},
                ],
            },
            {"processID": "p2", "startTime": 5, "tags": [{"key": "error", "value": True}]},
        ],
    }
    return json.dumps(
        {
            "backend": "jaeger",
            "query": {"service": "order-service"},
            "params": {},
            "response": {"data": [trace]},
        }
    ).encode()


def test_jaeger_digest_keeps_only_the_queried_service() -> None:
    d = jaeger_digest(_jaeger_file())
    assert d["raw_bytes"] > 0
    assert len(d["raw_sha256"]) == 64
    assert d["traces"] == [
        {
            "traceID": "abc",
            "spans": 2,
            "service_spans": 1,
            "service_status_codes": ["503"],
            "service_error_types": ["503"],
            "service_error_spans": 1,
            "start_us": 10,
        }
    ]


def test_export_copies_metrics_and_digests_traces(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    q = runs / "s" / "r1" / "queries"
    q.mkdir(parents=True)
    (runs / "s" / "r1.json").write_text("{}", encoding="utf-8")
    (q / "001-prometheus.json").write_text('{"x": 1}', encoding="utf-8")
    (q / "002-jaeger.json").write_bytes(_jaeger_file())
    out = tmp_path / "out"
    files = export(runs, "s", "r1", out)
    assert sorted(f.name for f in files) == [
        "001-prometheus.json",
        "002-jaeger.digest.json",
        "run-record.json",
    ]
    assert (out / "queries" / "001-prometheus.json").read_text(encoding="utf-8") == '{"x": 1}'
