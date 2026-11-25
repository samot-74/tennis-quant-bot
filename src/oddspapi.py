"""Betano.pt + Pinnacle odds from OddsPapi (https://oddspapi.io), one request per 5 tournaments per book.

Budget (free tier 250 requests/month): every call is logged in `api_usage`; the run stops before
going over `oddspapi.monthly_budget`. A run costs 1 (fixtures) + 2 * ceil(tournaments / 5).
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

from src.db import ROOT
from src.edge_detector import CFG, best_of

BASE = "https://api.oddspapi.io/v4"
CATALOGUE = ROOT / "config" / "oddspapi_markets.json"  # versioned: saves 1 request per cloud run

# (marketType, period) -> our market key. Handicap lines are player1's line on outcome "1".
MARKETS = {("moneyline", "result"): "match_winner", ("totals-games", "result"): "total_games",
           ("spreads-games", "result"): "game_handicap", ("spreads", "result"): "set_handicap",
           ("teamtotals-games-team1", "result"): "player1_games",
           ("teamtotals-games-team2", "result"): "player2_games",
           ("totals-games", "p1"): "set1_total_games", ("moneyline", "p1"): "set1_winner",
           # Pinnacle doesn't list these; their fair price is derived from its set handicap (see sharp_probs)
           ("correctscore", "result"): "correct_score", ("bothwinset", "result"): "both_win_set",
           ("winaset-team1", "result"): "p1_win_set", ("winaset-team2", "result"): "p2_win_set"}
DERIVED = ("correct_score", "both_win_set", "p1_win_set", "p2_win_set")
SELECTION = {"1": "p1", "2": "p2", "Over": "over", "Under": "under", "Yes": "yes", "No": "no",
             **{s: s for s in ("2:0", "2:1", "1:2", "0:2")}}


class BudgetExceeded(RuntimeError):
    pass


def _get(con, path: str, **params):
    used = con.execute("SELECT count(*) FROM api_usage WHERE provider = 'oddspapi' AND status < 400 AND ts >= date_trunc('month', now())"
                       ).fetchone()[0]
    if used >= CFG["oddspapi"]["monthly_budget"]:
        raise BudgetExceeded(f"OddsPapi budget used: {used}/{CFG['oddspapi']['monthly_budget']} this month")
    for attempt in range(4):
        time.sleep(1.5)  # free tier rejects back-to-back calls
        r = requests.get(BASE + path, params={**params, "apiKey": os.environ["ODDSPAPI_KEY"]}, timeout=60)
        con.execute("INSERT INTO api_usage VALUES (now(), 'oddspapi', ?, ?)", [path, r.status_code])
        if r.status_code != 429 or attempt == 3:
            break
        try:  # the API says how long to wait
            wait_ms = r.json().get("error", {}).get("retryMs") or 2000
        except ValueError:
            wait_ms = 2000
        print(f"OddsPapi rate limit, waiting {wait_ms / 1000:.1f}s", file=sys.stderr)
        time.sleep(min(wait_ms / 1000, 60) + 0.5)
    if not r.ok:  # never echo the URL: it contains the key
        raise RuntimeError(f"OddsPapi {path} -> HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def catalogue(con) -> dict[str, dict]:
    """marketId -> market definition for the markets we map (fetched once if the file is missing)."""
    if not CATALOGUE.exists():
        CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
        CATALOGUE.write_text(json.dumps([m for m in _get(con, "/markets", sportId=12)
                                         if (m["marketType"], m["period"]) in MARKETS]), encoding="utf-8")
    return {str(m["marketId"]): m for m in json.loads(CATALOGUE.read_text(encoding="utf-8"))}


def wanted(f: dict, now: datetime, hours: float) -> bool:
    name = f["tournamentName"]
    start = datetime.fromisoformat(f["startTime"].replace("Z", "+00:00"))
    return (f["statusId"] == 0 and f["hasOdds"] and 0 < (start - now).total_seconds() < hours * 3600
            and name.startswith(tuple(CFG["oddspapi"]["tour_prefixes"])) and "Doubles" not in name
            and not any(re.fullmatch(r"R\d+P\d+", f[k]) for k in ("participant1Name", "participant2Name")))


def player(name: str) -> str:
    """'La Serna, Juan Manuel' -> 'Juan Manuel La Serna'."""
    last, _, first = name.partition(", ")
    return f"{first} {last}".strip()


def normalise(odds: list[dict], fixtures: dict[str, dict], book: str, cat: dict[str, dict]) -> list[dict]:
    """odds-by-tournaments payload (no names) + /fixtures info by fixtureId -> edge_detector rows."""
    rows = []
    for o in odds:
        f = fixtures.get(o["fixtureId"])
        if not f:
            continue
        markets = o.get("bookmakerOdds", {}).get(book, {}).get("markets", {})
        ev = {"event_id": f["fixtureId"], "region": f.get("tournamentName", ""), "tournament": "",
              "player1": player(f["participant1Name"]), "player2": player(f["participant2Name"]),
              "kickoff": datetime.fromisoformat(f["startTime"].replace("Z", "+00:00"))}
        for mid, m in markets.items():
            d = cat.get(mid)
            key = d and MARKETS.get((d["marketType"], d["period"]))
            if not key or not m.get("marketActive", True):
                continue
            names = {str(o["outcomeId"]): o["outcomeName"] for o in d["outcomes"]}
            for oid, o in m["outcomes"].items():
                pr = o.get("players", {}).get("0", {})
                sel = SELECTION.get(names.get(oid))
                if not sel or not pr.get("active", True) or not pr.get("price"):
                    continue
                line = d["handicap"] if sel != "p2" or not key.endswith("handicap") else -d["handicap"]
                rows.append({**ev, "market": key, "selection": sel, "line": float(line), "odd": float(pr["price"]),
                             "market_id": key if key in DERIVED else mid})
    return rows


FIXTURES_CACHE = ROOT / "data" / "processed" / "fixtures_today.json"


def fixtures(con, max_age_h: float = 20) -> list[dict]:
    """Today+tomorrow tennis fixtures; reuses the planner's copy if it is fresh (saves a request)."""
    if FIXTURES_CACHE.exists() and (datetime.now().timestamp() - FIXTURES_CACHE.stat().st_mtime) < max_age_h * 3600:
        return json.loads(FIXTURES_CACHE.read_text(encoding="utf-8"))
    fx = _get(con, "/fixtures", sportId=12, **{"from": str(date.today()), "to": str(date.today() + timedelta(days=1))})
    FIXTURES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURES_CACHE.write_text(json.dumps(fx), encoding="utf-8")
    return fx


def fetch(con, hours: float, tournaments: list[int] | None = None) -> tuple[list[dict], list[dict], dict]:
    """(betano rows, pinnacle rows, info) for target singles matches in the next `hours`,
    optionally only for the given tournament ids (one cluster of the day)."""
    now = datetime.now(timezone.utc)
    cat = catalogue(con)
    fx = fixtures(con)
    target = [f for f in fx if wanted(f, now, hours) and (not tournaments or f["tournamentId"] in tournaments)]
    tids = sorted({f["tournamentId"] for f in target})
    keep = {f["fixtureId"]: f for f in target}
    print(f"OddsPapi: {len(fx)} fixtures, {len(target)} target singles in {len(tids)} tournaments", file=sys.stderr)
    out = {}
    for book in ("betano.pt", "pinnacle"):
        data = []
        for i in range(0, len(tids), 5):
            data += _get(con, "/odds-by-tournaments", bookmaker=book, tournamentIds=",".join(map(str, tids[i:i + 5])))
        out[book] = normalise(data, keep, book, cat)
    return out["betano.pt"], out["pinnacle"], {"fixtures": len(target), "tournaments": len(tids)}


def sharp_probs(pin_rows: list[dict]) -> tuple[dict, dict]:
    """Pinnacle rows -> ({(event, market_id, selection): fair p}, {event: winner ref}) via Shin per market.

    Best of 3 only: set handicap -1.5 is exactly a 2-0, so correct score / win a set / both win a set
    follow from it and the winner price (keyed by the DERIVED market names normalise() uses as ids).
    """
    from src.market import shin_probs

    groups = {}
    for r in pin_rows:
        groups.setdefault((r["event_id"], r["market_id"]), []).append(r)
    sharp, ref, two_nil, bo = {}, {}, {}, {}
    for (eid, mid), rs in groups.items():
        if len(rs) != 2:
            continue
        for r, p in zip(rs, shin_probs([r["odd"] for r in rs])):
            sharp[(eid, mid, r["selection"])] = p
            bo[eid] = best_of(r["region"], r["tournament"])
            if r["market"] == "match_winner":
                ref.setdefault(eid, {"source": "Pinnacle"})[r["selection"]] = p
            if r["market"] == "set_handicap" and r["line"] == -1.5:
                two_nil.setdefault(eid, {})[r["selection"]] = p
    ref = {e: v for e, v in ref.items() if "p1" in v and "p2" in v}
    for eid, t in two_nil.items():
        if eid not in ref or bo[eid] != 3 or len(t) != 2:
            continue
        w, p20, p02 = ref[eid]["p1"], t["p1"], t["p2"]
        derived = {("correct_score", "2:0"): p20, ("correct_score", "0:2"): p02,
                   ("correct_score", "2:1"): w - p20, ("correct_score", "1:2"): 1 - w - p02,
                   ("both_win_set", "yes"): 1 - p20 - p02, ("both_win_set", "no"): p20 + p02,
                   ("p1_win_set", "yes"): 1 - p02, ("p1_win_set", "no"): p02,
                   ("p2_win_set", "yes"): 1 - p20, ("p2_win_set", "no"): p20}
        sharp.update({(eid, m, s): p for (m, s), p in derived.items() if p > 0.01})
    return sharp, ref
