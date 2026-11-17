-- Runs once, on first initialization of the Postgres data volume.
-- Separate database for integration tests so they never touch dev data.
CREATE DATABASE sentinel_test;
