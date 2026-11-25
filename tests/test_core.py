import random

import pytest

from src.market import apply_daily_cap, edge, kelly_stake, shin_probs
from src.markov import game, match, over, set_dist, tiebreak


def test_shin_sums_to_one_and_shrinks_longshot():
    p = shin_probs([1.25, 4.20])
    assert sum(p) == pytest.approx(1, abs=1e-9)
    naive_dog = (1 / 4.2) / (1 / 1.25 + 1 / 4.2)
    assert p[1] < naive_dog  # Shin removes more margin from the longshot


def test_kelly_caps_and_rounds():
    assert kelly_stake(0.40, 2.0, 1000) == 0  # negative edge
    assert kelly_stake(0.60, 2.0, 1000, round_to=5) == 25  # 0.25*0.2=5% -> capped at 2.5%
    assert kelly_stake(0.53, 2.0, 1000, round_to=5) == 15  # 1.5% = 15
    assert kelly_stake(0.53, 2.0, 1000, sub_probs=[0.45, 0.6, 0.55], round_to=5) % 5 == 0
    assert sum(apply_daily_cap([25] * 8, 1000)) <= 100
    # 100 EUR bankroll, 1 EUR steps
    assert kelly_stake(0.60, 2.0, 100) == 2      # cap 2.50 -> 2
    assert kelly_stake(0.53, 2.0, 100) == 1      # 1.50 -> 1
    assert kelly_stake(0.51, 2.0, 100) == 1      # 0.50 -> bumped to the 1 EUR minimum
    assert kelly_stake(0.505, 2.0, 100) == 0     # 0.25 -> below trigger
    assert edge(0.55, 2.0) == pytest.approx(0.10)


def test_known_values():
    assert game(0.5) == pytest.approx(0.5)
    assert game(0.6) == pytest.approx(0.7357, abs=1e-4)  # textbook value
    assert tiebreak(0.6, 0.6) == pytest.approx(0.5)
    assert match(0.62, 0.62)["match_winner"] == pytest.approx(0.5)


def test_distributions_sum_to_one_and_symmetry():
    m = match(0.66, 0.60, best_of=5)
    for k in ("correct_score_sets", "total_games", "game_handicap", "1st_set_total_games"):
        assert sum(m[k].values()) == pytest.approx(1)
    assert m["match_winner"] > m["1st_set_winner"] > 0.5  # bo5 amplifies the better player
    swapped = match(0.60, 0.66, best_of=5, a_serves_first=False)
    assert swapped["match_winner"] == pytest.approx(1 - m["match_winner"])


def test_against_monte_carlo():
    pa, pb, n = 0.64, 0.58, 40_000
    rng = random.Random(1)

    def sim_set(srv):  # srv True = A serves first; returns (ga, gb, next_srv)
        ga = gb = 0
        while True:
            if ga == gb == 6:
                a = b = 0
                k = 0
                while not ((a >= 7 or b >= 7) and abs(a - b) >= 2):
                    a_srv = (((k + 1) // 2) % 2 == 0) == srv
                    win = rng.random() < (pa if a_srv else 1 - pb)
                    a, b, k = a + win, b + (not win), k + 1
                return (7, 6, not srv) if a > b else (6, 7, not srv)
            a_srv = ((ga + gb) % 2 == 0) == srv
            win = rng.random() < (game(pa) if a_srv else 1 - game(pb))
            ga, gb = ga + win, gb + (not win)
            if max(ga, gb) >= 6 and abs(ga - gb) >= 2:
                return ga, gb, srv if (ga + gb) % 2 == 0 else not srv

    wins = games = 0
    for _ in range(n):
        sa = sb = tg = 0
        srv = True
        while sa < 2 and sb < 2:
            ga, gb, srv = sim_set(srv)
            sa, sb, tg = sa + (ga > gb), sb + (gb > ga), tg + ga + gb
        wins += sa == 2
        games += tg > 22.5
    m = match(pa, pb)
    assert wins / n == pytest.approx(m["match_winner"], abs=0.01)
    assert games / n == pytest.approx(over(m["total_games"], 22.5), abs=0.01)


def test_set_dist_scores_valid():
    for (a, b) in set_dist(0.6, 0.6):
        assert (max(a, b) == 6 and abs(a - b) >= 2) or {a, b} in ({7, 5}, {7, 6})
