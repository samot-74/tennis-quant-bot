"""One run: OddsPapi (Betano.pt + Pinnacle) -> edges on the same market/line -> Telegram alerts.

python -X utf8 -m src.pipeline T1   # also refreshes TML results (Markov fallback)
python -X utf8 -m src.pipeline T2   # only new alerts / edges up >= 2 pp are sent
python -X utf8 -m src.pipeline T2 --t=6707,9179   # only these tournaments
"""
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from src import betano_scraper, historical_loader, oddspapi
from src.db import ROOT, connect
from src.edge_detector import CFG, detect
from src.telegram_notifier import notify


def main(run_tag: str = "T1", tournaments: list[int] | None = None):
    load_dotenv(ROOT / ".env")
    if run_tag == "T1":
        historical_loader.main(refresh=True)
    con = connect()
    betano, pinnacle, info = oddspapi.fetch(con, CFG["window_hours"], tournaments)
    used = con.execute("SELECT count(*) FROM api_usage WHERE provider='oddspapi' AND status < 400 AND ts >= date_trunc('month', now())").fetchone()[0]
    print(f"Betano.pt: {len(betano)} odds · Pinnacle: {len(pinnacle)} odds · {info} · "
          f"OddsPapi {used}/{CFG['oddspapi']['monthly_budget']} requests this month")
    if not betano:
        sys.exit("no Betano.pt odds for target matches")
    betano_scraper.save(betano, run_tag)
    sharp, ref = oddspapi.sharp_probs(pinnacle)
    ts = datetime.now(timezone.utc)
    if ref:
        con.executemany("INSERT INTO reference_odds VALUES (?,?,?,?,?,?)",
                        [(ts, run_tag, eid, r["p1"], r["p2"], r["source"]) for eid, r in ref.items()])
    alerts, stats = detect(con, betano, ref, sharp=sharp)
    print(f"stats: {stats} -> {len(alerts)} alerts")
    print(f"sent {notify(con, alerts, summary=True)} alerts")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--t=")]
    tids = next((a[4:] for a in sys.argv[1:] if a.startswith("--t=")), "")
    main(args[0] if args else "T1", [int(t) for t in tids.split(",") if t] or None)
