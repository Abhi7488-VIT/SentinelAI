#!/usr/bin/env bash
# psql against the SentinelAI Postgres; SQL on stdin or -c
cd /workspaces/SentinelAI && exec docker compose exec -T postgres psql -U sentinel -d sentinel -P pager=off "$@"
