"""Betano odds vs fair probabilities -> +EV candidates.

Match winner: p = reference fair price (Pinnacle / consensus, Shin). Markov (box-score model) only
             feeds the disagreement penalty in the stake.
Other markets: Markov with serve strengths fitted to the reference price (fit_to_price), so the
             relative strength comes from the market and the serve level (totals, tiebreaks)
             from SPW/RPW. Without a reference: raw Markov, only if require_reference is false
             (uncalibrated: in testing it showed 8-9% "edges" on several lines of the same match).
"""
import os
from collections import defaultdict

import yaml

from src.db import ROOT
from src.entity_resolver import resolve_tml
from src.market import apply_daily_cap, edge, kelly_stake
from src.markov import fit_to_price, match, over
from src.serve_stats import serve_probs, surface_for

CFG = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
SET_MARKETS = ("set_handicap", "correct_score", "both_win_set", "p1_win_set", "p2_win_set")
SLAMS = ("open da austr", "australian open", "roland garros", "french open", "wimbledon", "us open")


def tier(region: str, tournament: str) -> str:
    r = f"{region} {tournament}".lower()
    if any(s in r for s in SLAMS):
        return "tier3"
    if "challenger" in r or "itf" in r or "125" in r or "quali" in r:
        return "tier1"
    return "tier2"  # ATP/WTA main tour: Betano doesn't say 250 vs 1000, so be conservative


def best_of(region: str, tournament: str) -> int:
    r = f"{region} {tournament}".lower()
    women = "(f)" in r or "wta" in r or "women" in r
    return 5 if any(s in r for s in SLAMS) and not women else 3


def deep_scan(events: list[dict], winner_rows: list[dict], reference: dict[str, dict]) -> list[dict]:
    """Events worth a full ?bt=5 page (phase B):
    - target tours: Challenger / ITF / WTA / 125 / qualifying (where Betano lags most), or
    - Betano's Vencedor price is close to or above fair on either side (edge >= deep_scan_min_edge),
      i.e. a soft line, so its other markets probably are too.
    """
    odds = {(r["event_id"], r["selection"]): r["odd"] for r in winner_rows if r["market"] == "match_winner"}
    out = []
    for ev in events:
        name = f"{ev['region']} {ev['tournament']}".lower()
        target = any(w in name for w in CFG["deep_scan_tours"])
        ref = reference.get(ev["event_id"])
        soft = bool(ref) and any(
            (o := odds.get((ev["event_id"], sel))) and edge(ref[sel], o) >= CFG["deep_scan_min_edge"]
            for sel in ("p1", "p2"))
        if target or soft:
            out.append(ev)
    return out


def model_prob(m: dict, market: str, sel: str, line: float) -> float | None:
    if market == "match_winner":
        return m["match_winner"] if sel == "p1" else 1 - m["match_winner"]
    if market == "set1_winner":
        return m["1st_set_winner"] if sel == "p1" else 1 - m["1st_set_winner"]
    pmf_key = {"total_games": "total_games", "set1_total_games": "1st_set_total_games",
               "player1_games": "player1_games", "player2_games": "player2_games"}.get(market)
    if pmf_key:
        p = over(m[pmf_key], line)
        return p if sel == "over" else 1 - p
    if market == "game_handicap":  # selection line is that player's handicap
        h = m["game_handicap"]  # pmf of games A - games B
        return sum(p for d, p in h.items() if (d if sel == "p1" else -d) + line > 0)
    if market == "set_handicap":
        diffs = defaultdict(float)
        for (a, b), p in m["correct_score_sets"].items():
            diffs[a - b] += p
        return sum(p for d, p in diffs.items() if (d if sel == "p1" else -d) + line > 0)
    if market in ("tiebreak_in_match", "set1_tiebreak"):
        p = m["tiebreak_in_match" if market == "tiebreak_in_match" else "1st_set_tiebreak"]
        return p if sel == "yes" else 1 - p
    return None


def detect(con, betano_rows: list[dict], reference: dict[str, dict],
           bankroll: float | None = None, sharp: dict | None = None) -> tuple[list[dict], dict]:
    """sharp: {(event, market_id, selection): Pinnacle fair p} for the exact same market and line."""
    sharp = sharp or {}
    # markets Pinnacle prices for an event: there, only Pinnacle's own lines count (no Markov guesses)
    pin_markets = {(r["event_id"], r["market"]) for r in betano_rows
                   if (r["event_id"], r.get("market_id"), r["selection"]) in sharp}
    bankroll = bankroll or float(os.getenv("BANKROLL_EUR", "1000"))
    lo, hi = CFG["odds_range"]
    stats = defaultdict(int)
    by_event = defaultdict(list)
    for r in betano_rows:
        by_event[r["event_id"]].append(r)

    alerts = []
    for eid, rows in by_event.items():
        ev = rows[0]
        ref = reference.get(eid)
        bo, tr = best_of(ev["region"], ev["tournament"]), tier(ev["region"], ev["tournament"])
        ids = resolve_tml(con, [ev["player1"], ev["player2"]])
        m_raw = m_fit = None
        sp = None
        if len(ids) == 2:
            surface = surface_for(con, ev["tournament"] or ev["region"], list(ids.values()))
            sp = serve_probs(con, ids[ev["player1"]], ids[ev["player2"]], surface)
            m_raw = match(sp["pa"], sp["pb"], bo)
            m_fit = match(*fit_to_price(sp["pa"], sp["pb"], ref["p1"], bo), bo) if ref else m_raw
        else:
            stats["unresolved_players"] += 1
        stats["events"] += 1
        stats["with_reference"] += bool(ref)

        for r in rows:
            if not lo <= r["odd"] <= hi:
                continue
            pin = sharp.get((eid, r.get("market_id"), r["selection"]))
            if pin is not None:
                p, basis = pin, "Pinnacle"
                thr = CFG["edge_min"][tr] + (0 if r["market"] == "match_winner" else CFG["pinnacle_secondary_extra"])
                subs = [p, model_prob(m_raw, "match_winner", r["selection"], 0)] if m_raw and r["market"] == "match_winner" else []
                stats["priced_by_pinnacle"] += 1
            elif (eid, r["market"]) in pin_markets:
                continue
            elif r["market"] == "match_winner" and ref:
                p, basis, thr = ref[r["selection"]], ref["source"], CFG["edge_min"][tr]
                subs = [p, model_prob(m_raw, "match_winner", r["selection"], 0)] if m_raw else []
            elif m_fit and (ref or not CFG["require_reference"]):
                p = model_prob(m_fit, r["market"], r["selection"], r["line"])
                basis = f"markov+{ref['source']}" if ref else "markov"
                thr = max(CFG["edge_min"][tr], CFG["markov_market"])
                subs = []
            else:
                continue
            if p is None:
                continue
            e = edge(p, r["odd"])
            if e > CFG["max_edge"]:
                stats["skipped_suspicious_edge"] += 1
                continue
            if e < thr:
                continue
            stake = kelly_stake(p, r["odd"], bankroll, subs, CFG["kelly_fraction"],
                                CFG["max_stake_pct"], CFG["stake_round_eur"],
                                CFG["min_stake_eur"], CFG["min_stake_trigger_eur"])
            if stake <= 0:
                continue
            alerts.append({**r, "tier": tr, "p_model": p, "edge": e, "stake": stake, "basis": basis,
                           "fair_odd": 1 / p, "min_odd": (1 + thr) / p, "ref": ref, "markov": m_raw and {
                               "match_winner": m_raw["match_winner"], "pa": sp["pa"], "pb": sp["pb"],
                               "surface": sp["surface"], "n": (sp["n_a"], sp["n_b"])}})

    # one bet per (event, market): Over 20.5 / 21.5 / 22.5 are the same bet, keep the best edge.
    # The set markets are all one bet on the set score (2-0 = handicap -1.5 = "other player wins no set").
    best = {}
    for a in alerts:
        k = (a["event_id"], "sets" if a["market"] in SET_MARKETS else a["market"])
        if k not in best or a["edge"] > best[k]["edge"]:
            best[k] = a
    alerts = sorted(best.values(), key=lambda a: -a["edge"])
    for a, s in zip(alerts, apply_daily_cap([a["stake"] for a in alerts], bankroll,
                                            CFG["max_daily_pct"], CFG["stake_round_eur"])):
        a["stake"] = s
    return [a for a in alerts if a["stake"] > 0], dict(stats)
