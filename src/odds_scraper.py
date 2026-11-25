"""Reference (sharp) match-winner odds.

Primary: Pinnacle via The Odds API (THE_ODDS_API_KEY). Only ATP/WTA 500+/Slams are listed there,
  1 credit per active tournament per run (free tier: 500/month); the sports list call is free.
Fallback for everything else (Challengers, 250s, missing key): OddsPortal via OddsHarvester:

1. `upcoming --links-only` for today + tomorrow (cheap: one listing page per day).
2. Pair each link with a Betano event (both player names in the URL slug + kickoff within 12 h;
   tennis start times are estimates and differ by hours between sites).
3. Scrape match_winner only for the paired links.

From a Portuguese IP OddsPortal shows only PT-licensed books (no Pinnacle). So the fair price is:
  Pinnacle (or Betfair) if present, else Shin over the median odds of all listed books ("consensus").
Set ODDSHARVESTER_PROXY in .env to route OddsHarvester through a proxy of your choice.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import requests

from src.entity_resolver import same_player
from src.market import shin_probs

SHARP = ("pinnacle", "betfair")


def _run(args: list[str], out: Path) -> list[dict]:
    cmd = ["oddsharvester", "upcoming", "-s", "tennis", "--headless", "-f", "json", "-o", str(out), *args]
    if os.getenv("ODDSHARVESTER_PROXY"):
        cmd += ["--proxy-url", os.environ["ODDSHARVESTER_PROXY"]]
    subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=1800)
    return json.loads(out.read_text(encoding="utf-8")) if out.exists() else []


def slug_names(link: str) -> tuple[str, str] | None:
    m = re.search(r"/h2h/([a-z0-9-]+)-[A-Za-z0-9]{8}/([a-z0-9-]+)-[A-Za-z0-9]{8}/", link)
    return (m.group(1).replace("-", " "), m.group(2).replace("-", " ")) if m else None


def pair(links: list[dict], events: list[dict]) -> dict[str, str]:
    """{event_id: link}. Slug order is alphabetical, NOT home/away: orientation comes from the record."""
    out = {}
    for ev in events:
        best = None
        for l in links:
            names = slug_names(l["match_link"])
            ko = datetime.strptime(l["kickoff_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
            gap = abs((ko - ev["kickoff"]).total_seconds())
            if not names or gap > 12 * 3600:
                continue
            for swapped, (h, a) in ((False, names), (True, names[::-1])):
                s = min(same_player(ev["player1"], h), same_player(ev["player2"], a))
                if s and (best is None or (s, -gap) > (best[0], -best[3])):
                    best = (s, l["match_link"], swapped, gap)
        if best and best[0] >= 70:
            out[ev["event_id"]] = best[1]
    return out


def orient(ev: dict, record: dict) -> bool | None:
    """True if OddsPortal home is Betano player2; None if sides can't be told apart (skip, never mirror)."""
    straight = min(same_player(ev["player1"], record["home_team"]), same_player(ev["player2"], record["away_team"]))
    crossed = min(same_player(ev["player1"], record["away_team"]), same_player(ev["player2"], record["home_team"]))
    return None if max(straight, crossed) < 70 or straight == crossed else crossed > straight


def fair_probs(record: dict) -> tuple[float, float, str] | None:
    """(p_home, p_away, source) from one OddsHarvester record."""
    books = [b for b in record.get("match_winner_market", []) if b.get("period") == "FullTime"]
    if not books:
        return None
    sharp = [b for b in books if b["bookmaker_name"].lower().startswith(SHARP)]
    if sharp:
        b = sharp[0]
        h, a = shin_probs([float(b["player_1"]), float(b["player_2"])])
        return h, a, b["bookmaker_name"]
    h, a = shin_probs([median(float(b["player_1"]) for b in books),
                       median(float(b["player_2"]) for b in books)])
    return h, a, f"consensus({len(books)})"


ODDS_API = "https://api.the-odds-api.com/v4/sports"


def oddsapi_pair(api_events: list[dict], events: list[dict]) -> dict[str, dict]:
    """Betano events -> Pinnacle fair probs from The Odds API event list (pure, tested)."""
    out = {}
    for ev in events:
        for ae in api_events:
            ko = datetime.fromisoformat(ae["commence_time"].replace("Z", "+00:00"))
            if abs((ko - ev["kickoff"]).total_seconds()) > 12 * 3600:
                continue
            swapped = orient(ev, ae)
            book = next((b for b in ae.get("bookmakers", []) if b["key"] == "pinnacle"), None)
            mkt = book and next((m for m in book["markets"] if m["key"] == "h2h"), None)
            if swapped is None or not mkt:
                continue
            price = {o["name"]: o["price"] for o in mkt["outcomes"]}
            h, a = shin_probs([price[ae["home_team"]], price[ae["away_team"]]])
            out[ev["event_id"]] = {"p1": a if swapped else h, "p2": h if swapped else a, "source": "Pinnacle"}
            break
    return out


def oddsapi_reference(events: list[dict]) -> dict[str, dict]:
    key = os.getenv("THE_ODDS_API_KEY")
    if not key:
        return {}
    try:
        sports = requests.get(ODDS_API, params={"apiKey": key}, timeout=20).json()  # free call
        api_events = []
        for sp in (x["key"] for x in sports if x.get("group") == "Tennis" and x.get("active")):
            r = requests.get(f"{ODDS_API}/{sp}/odds", timeout=20, params={
                "apiKey": key, "bookmakers": "pinnacle", "markets": "h2h", "oddsFormat": "decimal"})
            if r.ok:
                api_events += r.json()
            print(f"The Odds API {sp}: {r.status_code}, credits left {r.headers.get('x-requests-remaining')}",
                  file=sys.stderr)
    except (requests.RequestException, ValueError) as e:
        print(f"The Odds API unavailable: {e}", file=sys.stderr)
        return {}
    return oddsapi_pair(api_events, events)


def reference_odds(events: list[dict]) -> dict[str, dict]:
    """{betano event_id: {"p1": fair p, "p2": fair p, "source": str}}. Pinnacle first, OddsPortal for the rest."""
    ref = oddsapi_reference(events)
    rest = [e for e in events if e["event_id"] not in ref]
    print(f"Reference: {len(ref)} from Pinnacle (The Odds API), {len(rest)} left for OddsPortal", file=sys.stderr)
    return {**ref, **(oddsportal_reference(rest) if rest else {})}


def oddsportal_reference(events: list[dict]) -> dict[str, dict]:
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    links = []
    for d in (now, now + timedelta(days=1)):
        links += _run(["-d", d.strftime("%Y%m%d"), "--links-only"], tmp / f"links_{d:%Y%m%d}.json")
    pairs = pair(links, events)
    print(f"OddsPortal: {len(links)} links, {len(pairs)} paired with Betano", file=sys.stderr)
    if not pairs:
        return {}
    lf = tmp / "links.txt"
    lf.write_text("\n".join(pairs.values()))
    records = {r["match_link"]: r for r in _run(["--match-links-file", str(lf), "-m", "match_winner"],
                                                 tmp / "odds.json")}
    out = {}
    evs = {e["event_id"]: e for e in events}
    for eid, link in pairs.items():
        r = records.get(link)
        fp = fair_probs(r) if r else None
        swapped = orient(evs[eid], r) if fp else None
        if swapped is not None:
            h, a, src = fp
            out[eid] = {"p1": a if swapped else h, "p2": h if swapped else a, "source": src}
    return out
