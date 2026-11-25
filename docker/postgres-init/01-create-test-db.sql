-- Runs once, when the Postgres data volume is first initialised.
-- Creates the separate database used by the pytest suite.
CREATE DATABASE agentflow_test OWNER agentflow;
