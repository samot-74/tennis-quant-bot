"""DuckDB connection + schema. Historical match tables are created by historical_loader."""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "tennis_quant.duckdb"

SCHEMA = """
CREATE TABLE IF NOT EXISTS betano_odds (
    snapshot_ts TIMESTAMP, run_tag VARCHAR,          -- run_tag: T1 / T2
    event_id VARCHAR, region VARCHAR, tournament VARCHAR, player1 VARCHAR, player2 VARCHAR,
    kickoff TIMESTAMP, market VARCHAR, selection VARCHAR, line DOUBLE, odd DOUBLE
);
CREATE TABLE IF NOT EXISTS reference_odds (        -- de-vigged match-winner price, per Betano event
    snapshot_ts TIMESTAMP, run_tag VARCHAR, event_id VARCHAR,
    p1 DOUBLE, p2 DOUBLE, source VARCHAR           -- source: Pinnacle / Betfair / consensus(n)
);
CREATE TABLE IF NOT EXISTS player_map (
    source VARCHAR, source_name VARCHAR, player_id VARCHAR, score DOUBLE,
    PRIMARY KEY (source, source_name)
);
CREATE TABLE IF NOT EXISTS api_usage (ts TIMESTAMP, provider VARCHAR, path VARCHAR, status INT);
CREATE TABLE IF NOT EXISTS sent_alerts (
    match_id VARCHAR, market VARCHAR, selection VARCHAR,
    odd_sent DOUBLE, edge_sent DOUBLE, stake DOUBLE, ts TIMESTAMP DEFAULT current_timestamp
);
"""


def connect(path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con
