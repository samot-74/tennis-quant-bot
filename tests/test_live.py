import json
from pathlib import Path

import pytest

from src.betano_scraper import league_events, league_urls, parse_markets
from src.edge_detector import model_prob
from src.entity_resolver import best_match, same_player
from src.markov import match
from src.odds_scraper import fair_probs, slug_names

FX = Path(__file__).parent / "fixtures"
load = lambda n: json.loads((FX / n).read_text(encoding="utf-8"))


def test_betano_parser():
    urls = league_urls(load("betano_index.json"))
    assert "/sport/tenis/challenger/florianapolis/198209/" in urls
    assert not any("duplas" in u or "vencedor-final" in u for u in urls)

    evs = league_events(load("betano_league.json"))
    ev = next(e for e in evs if e["event_id"] == "96069412")
    assert (ev["player1"], ev["player2"]) == ("Matheus Pucinelli De Almeida", "Carlos Maria Zarate")

    rows = parse_markets(load("betano_event.json"), ev)
    got = {(r["market"], r["selection"], r["line"]): r["odd"] for r in rows}
    assert got[("match_winner", "p1", 0)] == 1.12 and got[("match_winner", "p2", 0)] == 4.45
    assert got[("total_games", "over", 19.5)] == 1.7 and got[("total_games", "under", 19.5)] == 1.8
    assert got[("game_handicap", "p1", -5.5)] == 1.8 and got[("game_handicap", "p2", 5.5)] == 1.72
    assert ("set1_total_games", "over", 9.5) in got
    assert got[("player1_games", "over", 12.5)] == 1.95
    assert len(got) == len(rows)  # duplicates across tabs removed


def test_name_matching_and_siblings():
    assert same_player("Ryan Nijboer", "Nijboer R.") > 70
    assert same_player("Ryan Nijboer", "nijboer ryan") == 100
    assert same_player("Matheus Pucinelli De Almeida", "Pucinelli De Almeida M.") > 70
    assert same_player("Juan Manuel Cerundolo", "Cerundolo F.") == 0
    assert same_player("Juan Manuel Cerundolo", "Francisco Cerundolo") == 0
    assert same_player("Alexander Zverev", "Mischa Zverev") == 0
    cands = {"A": "Francisco Cerundolo", "B": "Juan Manuel Cerundolo"}
    assert best_match("Cerundolo J. M.", cands)[0] == "B"
    assert best_match("Cerundolo F.", cands)[0] == "A"
    assert best_match("Cerundolo", cands) is None  # ambiguous -> refuse


def test_oddsportal_helpers():
    assert slug_names("https://www.oddsportal.com/tennis/h2h/nijboer-ryan-Yexh5F5g/roncadelli-franco-Aw1hl8Sn/#x") \
        == ("nijboer ryan", "roncadelli franco")
    rec = {"match_winner_market": [
        {"player_1": "2.70", "player_2": "1.27", "bookmaker_name": "Betclic.pt", "period": "FullTime"},
        {"player_1": "2.85", "player_2": "1.28", "bookmaker_name": "bwin.pt", "period": "FullTime"}]}
    h, a, src = fair_probs(rec)
    assert src == "consensus(2)" and h + a == pytest.approx(1)
    rec["match_winner_market"].append({"player_1": "2.90", "player_2": "1.45", "bookmaker_name": "Pinnacle",
                                       "period": "FullTime"})
    assert fair_probs(rec)[2] == "Pinnacle"


def test_model_prob_markets_are_complementary():
    m = match(0.65, 0.60)
    for mk, a, b, line in [("total_games", "over", "under", 21.5), ("game_handicap", "p1", "p2", -2.5),
                           ("set_handicap", "p1", "p2", -1.5), ("player1_games", "over", "under", 11.5),
                           ("tiebreak_in_match", "yes", "no", 0), ("set1_winner", "p1", "p2", 0)]:
        pa = model_prob(m, mk, a, line)
        pb = model_prob(m, mk, b, -line if mk.endswith("handicap") else line)
        assert pa + pb == pytest.approx(1), mk
    assert model_prob(m, "set_handicap", "p1", -1.5) == pytest.approx(m["correct_score_sets"][(2, 0)])


def test_pairing_tolerates_kickoff_offset_and_swaps_sides():
    from datetime import datetime, timezone
    from src.odds_scraper import pair
    ev = {"event_id": "1", "player1": "Carlos Maria Zarate", "player2": "Matheus Pucinelli De Almeida",
          "kickoff": datetime(2026, 11, 25, 13, 40, tzinfo=timezone.utc)}
    links = [{"match_link": "https://www.oddsportal.com/tennis/h2h/pucinelli-de-almeida-matheus-6J1eXqaF/"
                            "zarate-carlos-maria-8WEmZwq6/#QNYHIyFc", "kickoff_utc": "2026-11-25 16:30:00 UTC"},
             {"match_link": "https://www.oddsportal.com/tennis/h2h/fernandez-rebelo-YmTgy47q/nikles-zarate-rN9JzXam/#d",
              "kickoff_utc": "2026-11-25 14:00:00 UTC"}]
    assert "pucinelli" in pair(links, [ev])["1"]


def test_orientation_from_record_not_slug():
    from src.odds_scraper import orient
    ev = {"player1": "Pedro Sakamoto", "player2": "Guido Ivan Justo"}
    assert orient(ev, {"home_team": "Sakamoto P.", "away_team": "Justo G. I."}) is False
    assert orient(ev, {"home_team": "Justo G. I.", "away_team": "Sakamoto P."}) is True
    assert orient(ev, {"home_team": "Someone X.", "away_team": "Other Y."}) is None


def test_abbreviated_long_names():
    assert same_player("Matheus Pucinelli De Almeida", "Pucinelli M.") >= 70
    assert same_player("Joaquin Aguilar Cardozo", "Aguilar Cardozo J.") >= 70
    assert same_player("Genaro Alberto Olivieri", "Olivieri G. A.") >= 70


def test_notifier_dedup_and_daily_cap(monkeypatch, capsys):
    from datetime import datetime, timezone
    import duckdb
    from src.db import SCHEMA
    from src.telegram_notifier import notify
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    base = {"event_id": "1", "region": "Challenger", "tournament": "X", "player1": "A B", "player2": "C D",
            "kickoff": datetime(2026, 11, 25, tzinfo=timezone.utc), "market": "total_games", "selection": "over",
            "line": 21.5, "odd": 2.0, "tier": "tier1", "p_model": 0.55, "fair_odd": 1.82, "basis": "markov",
            "ref": None, "markov": None, "edge": 0.10, "stake": 25, "min_odd": 1.88}
    assert notify(con, [base], bankroll=1000) == 1
    assert notify(con, [base], bankroll=1000) == 0                      # duplicate
    assert notify(con, [{**base, "edge": 0.115}], bankroll=1000) == 0   # +1.5 pp: still a duplicate
    assert notify(con, [{**base, "edge": 0.125}], bankroll=1000) == 1   # +2.5 pp: re-alert
    others = [{**base, "event_id": str(i)} for i in range(2, 10)]       # 8 x 25 EUR, 50 EUR already used
    assert notify(con, others, bankroll=1000) == 2                      # 100 EUR daily cap
    out = capsys.readouterr().out
    assert "Mais de 21.5" in out and "≥ <b>1.88</b>" in out
    assert notify(con, [base], bankroll=1000, summary=True) == 0
    assert "Hoje sem apostas" in capsys.readouterr().out   # header even when nothing new


def test_oddsapi_pairing():
    from datetime import datetime, timezone
    from src.odds_scraper import oddsapi_pair
    ev = {"event_id": "9", "player1": "Jannik Sinner", "player2": "Carlos Alcaraz",
          "kickoff": datetime(2026, 1, 20, 8, tzinfo=timezone.utc)}
    api = [{"commence_time": "2026-01-20T09:00:00Z", "home_team": "Carlos Alcaraz", "away_team": "Jannik Sinner",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Carlos Alcaraz", "price": 2.10}, {"name": "Jannik Sinner", "price": 1.80}]}]}]}]
    r = oddsapi_pair(api, [ev])["9"]
    assert r["source"] == "Pinnacle" and r["p1"] > r["p2"]   # Sinner is the 1.80 favourite
    assert oddsapi_pair(api, [{**ev, "kickoff": datetime(2026, 1, 22, tzinfo=timezone.utc)}]) == {}


def test_phase_a_rows_and_deep_scan():
    from src.edge_detector import deep_scan
    st = load("betano_league.json")
    raw = {e["id"]: e for b in st["data"]["blocks"] for e in b["events"]}
    evs = league_events(st)
    rows = [r for ev in evs for r in parse_markets({"data": {"event": raw[ev["event_id"]]}}, ev)]
    assert {(r["event_id"], r["market"]) for r in rows} == {(e["event_id"], "match_winner") for e in evs}
    assert len(deep_scan(evs, rows, {})) == len(evs)  # all Challenger -> target tour
    atp = [{**e, "region": "ATP", "tournament": "Basel"} for e in evs]
    e0 = atp[0]["event_id"]  # Betano 1.12 / 4.45
    assert deep_scan(atp, rows, {}) == []
    assert [e["event_id"] for e in deep_scan(atp, rows, {e0: {"p1": 0.80, "p2": 0.20}})] == []
    assert [e["event_id"] for e in deep_scan(atp, rows, {e0: {"p1": 0.76, "p2": 0.24}})] == [e0]  # 4.45*.24=1.07




def test_oddspapi_normalise_and_sharp():
    from src.oddspapi import normalise, sharp_probs
    d = load("oddspapi.json")
    cat = {str(m["marketId"]): m for m in d["markets"]}
    fx = {f["fixtureId"]: f for f in d["fixtures"]}
    bet = normalise(d["betano"], fx, "betano.pt", cat)
    pin = normalise(d["pinnacle"], fx, "pinnacle", cat)
    g = {(r["market"], r["selection"], r["line"]): r["odd"] for r in bet}
    assert bet[0]["player1"] == "Matheus Pucinelli de Almeida"
    # same prices the Betano page showed
    assert g[("match_winner", "p1", 0)] == 1.12 and g[("match_winner", "p2", 0)] == 4.45
    assert g[("game_handicap", "p1", -5.5)] == 1.8 and g[("game_handicap", "p2", 5.5)] == 1.72
    assert g[("total_games", "over", 19.5)] == 1.7 and g[("player1_games", "over", 12.5)] == 1.95
    sharp, ref = sharp_probs(pin)
    eid = bet[0]["event_id"]
    assert ref[eid]["p1"] + ref[eid]["p2"] == pytest.approx(1) and ref[eid]["p1"] > 0.7
    for (e, mid, sel), p in sharp.items():
        other = {"p1": "p2", "p2": "p1", "over": "under", "under": "over", "yes": "no", "no": "yes"}[sel]
        assert p + sharp[(e, mid, other)] == pytest.approx(1)



def test_backtest_settlement():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bt", Path(__file__).parents[1] / "scripts" / "backtest_day.py")
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    sets = bt.sets_from_score("6-2 6-7(5) 7-6(3)")   # winner first: 19-15 games, 2 tiebreaks
    assert sets == [(6, 2, False), (6, 7, True), (7, 6, True)]
    assert bt.sets_from_score("6-3 2-1 RET") is None
    b = lambda m, s, l=0.0: {"market": m, "selection": s, "line": l}
    # p1 lost this match (p1_won=False): p1 games 15, p2 games 19
    assert bt.settle(b("match_winner", "p2"), sets, False)
    assert bt.settle(b("total_games", "over", 33.5), sets, False)
    assert not bt.settle(b("player1_games", "over", 15.5), sets, False)
    assert bt.settle(b("game_handicap", "p1", 4.5), sets, False)       # 15 - 19 + 4.5 > 0
    assert not bt.settle(b("game_handicap", "p1", 3.5), sets, False)
    assert bt.settle(b("set_handicap", "p1", 1.5), sets, False)        # lost 1-2 sets
    assert bt.settle(b("set1_winner", "p2"), sets, False) and bt.settle(b("set1_total_games", "over", 7.5), sets, False)
    assert bt.settle(b("tiebreak_in_match", "yes"), sets, False) and not bt.settle(b("set1_tiebreak", "yes"), sets, False)
