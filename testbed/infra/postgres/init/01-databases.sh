#!/bin/sh
# Runs once, on first initialization of the testbed Postgres volume.
# Each service owns its database and connects with its own least-privilege role.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE ROLE users_svc LOGIN PASSWORD '${USERS_DB_PASSWORD}';
CREATE ROLE orders_svc LOGIN PASSWORD '${ORDERS_DB_PASSWORD}';
CREATE ROLE payments_svc LOGIN PASSWORD '${PAYMENTS_DB_PASSWORD}';
CREATE DATABASE users OWNER users_svc;
CREATE DATABASE orders OWNER orders_svc;
CREATE DATABASE payments OWNER payments_svc;
EOSQL
