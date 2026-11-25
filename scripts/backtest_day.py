"""Replay one day's 07:15 send with no lookahead, then settle it with the real results.

Known at 07:15 only: the fixture list and, per outcome, the last Betano/Pinnacle price published
before 07:15 (OddsPapi odds history). Same detector, thresholds, stakes and daily cap as the live bot.
Results (TennisMyLife ongoing-tournament files) are read only after the bets are fixed.

python -X utf8 scripts/backtest_day.py 2026-11-24 [07:15]
Costs 1 OddsPapi request per match (cached in data/raw/oddspapi/hist/).
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.db import ROOT, connect  # noqa: E402
from src.edge_detector import detect  # noqa: E402
from src.entity_resolver import same_player  # noqa: E402
from src.oddspapi import _get, catalogue, normalise, player, sharp_probs  # noqa: E402

HIST = ROOT / "data" / "raw" / "oddspapi" / "hist"
TML = "https://stats.tennismylife.org/data/"


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def as_of(hist: dict, t: datetime) -> dict:
    """History -> odds-by-tournaments-shaped payload with the last price at or before t per outcome."""
    books = {}
    for book, b in hist.get("bookmakers", {}).items():
        mk = {}
        for mid, m in b.get("markets", {}).items():
            outs = {}
            for oid, o in m.get("outcomes", {}).items():
                past = [p for p in o.get("players", {}).get("0", []) if ts(p["createdAt"]) <= t]
                if past and past[-1]["active"]:
                    outs[oid] = {"players": {"0": {"price": past[-1]["price"], "active": True}}}
            if outs:
                mk[mid] = {"marketActive": True, "outcomes": outs}
        books[book] = {"markets": mk}
    return {"fixtureId": hist["fixtureId"], "bookmakerOdds": books}


def sets_from_score(score: str) -> list[tuple[int, int, bool]] | None:
    """'6-4 6-7(5) 7-6(3)' (winner first) -> [(w, l, tiebreak)]; None for RET/W-O/unfinished."""
    if not score or re.search(r"RET|W/O|DEF|ABD|unfinished", score, re.I):
        return None
    out = []
    for s in score.split():
        m = re.fullmatch(r"(\d+)-(\d+)(?:\(\d+\))?", s)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2))
        out.append((a, b, {a, b} == {7, 6}))
    return out


def settle(bet: dict, sets: list[tuple[int, int, bool]], p1_won: bool) -> bool:
    s = [(a, b, t) if p1_won else (b, a, t) for a, b, t in sets]  # p1 perspective
    g1, g2 = sum(a for a, _, _ in s), sum(b for _, b, _ in s)
    s1, s2 = sum(a > b for a, b, _ in s), sum(b > a for a, b, _ in s)
    m, sel, line = bet["market"], bet["selection"], bet["line"]
    side = lambda x: x if sel == "p1" else -x
    ou = lambda v: (v > line) if sel == "over" else (v < line)
    return {"match_winner": lambda: (sel == "p1") == p1_won,
            "set1_winner": lambda: (sel == "p1") == (s[0][0] > s[0][1]),
            "total_games": lambda: ou(g1 + g2),
            "set1_total_games": lambda: ou(s[0][0] + s[0][1]),
            "player1_games": lambda: ou(g1), "player2_games": lambda: ou(g2),
            "game_handicap": lambda: side(g1 - g2) + line > 0,
            "set_handicap": lambda: side(s1 - s2) + line > 0,
            "tiebreak_in_match": lambda: any(t for *_, t in s) == (sel == "yes"),
            "set1_tiebreak": lambda: s[0][2] == (sel == "yes")}[m]()


def main(day: str, at: str = "07:15", bankroll: float = 100):
    load_dotenv(ROOT / ".env")
    con = connect()
    t0 = datetime.fromisoformat(f"{day}T{at}").astimezone(timezone.utc)
    fx_file = ROOT / "data" / "raw" / "oddspapi" / f"fixtures_{day}.json"
    fx = json.loads(fx_file.read_text(encoding="utf-8")) if fx_file.exists() else _get(
        con, "/fixtures", sportId=12, **{"from": day, "to": str((t0 + timedelta(days=1)).date())})
    target = {f["fixtureId"]: f for f in fx
              if f["tournamentName"].startswith(("ATP", "WTA")) and "Doubles" not in f["tournamentName"]
              and t0 < ts(f["startTime"]) < t0 + timedelta(hours=24)
              and not re.fullmatch(r"R\d+P\d+", f["participant1Name"])}
    HIST.mkdir(parents=True, exist_ok=True)
    payload = {"betano.pt": [], "pinnacle": []}
    closing = {}
    no_hist = []
    for fid, f in list(target.items()):
        cache = HIST / f"{fid}.json"
        if not cache.exists():
            if f["statusName"] != "Finished":  # free plan: history only for finished fixtures
                no_hist.append(f"{player(f['participant1Name'])} - {player(f['participant2Name'])} ({f['statusName']})")
                del target[fid]
                continue
            cache.write_text(json.dumps(_get(con, "/historical-odds", fixtureId=fid, bookmakers="betano.pt,pinnacle")))
        h = json.loads(cache.read_text())
        snap = as_of(h, t0)
        for book in payload:
            payload[book].append(snap)
        closing[fid] = as_of(h, ts(f.get("trueStartTime") or f["startTime"]))
    cat = catalogue(con)
    bet_rows = normalise(payload["betano.pt"], target, "betano.pt", cat)
    pin_rows = normalise(payload["pinnacle"], target, "pinnacle", cat)
    sharp, ref = sharp_probs(pin_rows)
    alerts, stats = detect(con, bet_rows, ref, bankroll=bankroll, sharp=sharp)
    print(f"{day} {at}: {len(target)} matches, {len({r['event_id'] for r in bet_rows})} with Betano, "
          f"{len(ref)} with Pinnacle; {stats}; {len(alerts)} bets\n")

    # ---- only now look at results ----
    res = []
    for f in ("challenger_ongoing_tourneys.csv", "ongoing_tourneys.csv", "wta_ongoing_tourneys.csv"):
        txt = requests.get(TML + f, timeout=30).text
        head, *lines = txt.strip().splitlines()
        cols = head.split(",")
        res += [dict(zip(cols, l.split(","))) for l in lines]
    close_sharp = {fid: sharp_probs(normalise([c], target, "pinnacle", cat))[0] for fid, c in closing.items()}

    pnl = staked = 0.0
    for a in alerts:
        f = target[a["event_id"]]
        p1, p2 = player(f["participant1Name"]), player(f["participant2Name"])
        r = next((r for r in res if min(same_player(p1, r["winner_name"]), same_player(p2, r["loser_name"])) >= 70
                  or min(same_player(p2, r["winner_name"]), same_player(p1, r["loser_name"])) >= 70), None)
        clv_p = close_sharp[a["event_id"]].get((a["event_id"], a["market_id"], a["selection"]))
        clv = f"{a['odd'] * clv_p - 1:+.1%}" if clv_p else "n/a"
        if f["statusName"] == "Cancelled":
            outcome, gain = "VOID (cancelado)", 0.0
        elif not r:
            outcome, gain = "sem resultado", None
        else:
            sets = sets_from_score(r["score"])
            if sets is None:
                outcome, gain = f"VOID? ({r['score']})", 0.0
            else:
                won = settle(a, sets, same_player(p1, r["winner_name"]) >= 70)
                outcome, gain = ("GANHA" if won else "PERDE"), (a["stake"] * (a["odd"] - 1) if won else -a["stake"])
        if gain is not None:
            pnl += gain
            staked += a["stake"] if not outcome.startswith("VOID") else 0
        sel = {"p1": p1, "p2": p2}.get(a["selection"], a["selection"])
        print(f"{f['startTime'][11:16]} {p1} - {p2} | {a['market']} {sel} {a['line']:+g} @ {a['odd']:.2f} "
              f"(justa {a['fair_odd']:.2f}, edge {a['edge']:+.1%}, {a['basis']}) stake {a['stake']:.0f}€ | "
              f"CLV {clv} | {r['score'] if r else '-'} -> {outcome} {'' if gain is None else f'{gain:+.2f}€'}")
    print(f"\nTotal: {len(alerts)} apostas, {staked:.0f} € apostados, resultado {pnl:+.2f} €"
          + (f" (ROI {pnl / staked:+.1%})" if staked else ""))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "07:15")
