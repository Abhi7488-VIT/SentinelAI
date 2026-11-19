"""Fault-injection controller: scenario plugins, symptom checks, ground-truth run records.

CLI: python -m harness.controller list | validate | run | start | stop | status | serve
HTTP (serve, :8090): POST /scenarios/{id}/start, POST /scenarios/{id}/stop, GET /runs/{run_id}
"""
