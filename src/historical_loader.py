"""Download TennisMyLife (Sackmann-schema) CSVs and load them into DuckDB table `matches`.

Usage: python -m src.historical_loader            # first_year..current year from settings.yaml
       python -m src.historical_loader --refresh  # re-download current year only (daily run)
"""
import sys
from datetime import date

import requests
import yaml

from src.db import ROOT, connect

BASE = "https://stats.tennismylife.org/data/"
RAW = ROOT / "data" / "raw" / "tml"
SUFFIX = {"atp": "{y}.csv", "challenger": "{y}_challenger.csv",
          "wta": "{y}_wta.csv", "atp_quali": "atp_quali/{y}_atp_quali.csv"}


def download(rel: str, force: bool) -> bool:
    dest = RAW / rel.replace("/", "_")
    if dest.exists() and not force:
        return True
    r = requests.get(BASE + rel, timeout=60)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content.replace(bytes([13]), b""))  # some files mix CRLF/LF, which breaks the sniffer
    return True


def main(refresh: bool = False):
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())["history"]
    this_year = date.today().year
    all_years = range(cfg["first_year"], this_year + 1)
    files = {}
    for tour in cfg["tours"]:
        for y in ([this_year] if refresh else all_years):
            if not download(SUFFIX[tour].format(y=y), force=(y == this_year)):
                print(f"missing: {tour} {y}")
        files[tour] = [p.as_posix() for y in all_years
                       if (p := RAW / SUFFIX[tour].format(y=y).replace("/", "_")).exists()]
    download("ATP_Database.csv", force=refresh)

    con = connect()
    selects = [f"SELECT '{tour}' AS tour, * FROM read_csv({fs}, union_by_name=true, all_varchar=true)"
               for tour, fs in files.items() if fs]
    con.execute(f"""
        CREATE OR REPLACE TABLE matches AS
        SELECT * REPLACE (strptime(tourney_date, '%Y%m%d')::DATE AS tourney_date)
        FROM ({' UNION ALL BY NAME '.join(selects)})
        WHERE score IS NOT NULL
    """)
    con.execute(f"""CREATE OR REPLACE TABLE players AS
        SELECT * FROM read_csv('{(RAW / 'ATP_Database.csv').as_posix()}', all_varchar=true)""")
    print(con.sql("SELECT tour, count(*) n, min(tourney_date), max(tourney_date) FROM matches GROUP BY 1 ORDER BY 1"))


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
