"""Exact Markov model of a tennis match from point-on-serve probabilities.

pa = P(A wins a point on A's serve), pb = P(B wins a point on B's serve).
Assumes i.i.d. points (O'Malley / Newton-Keller). Standard tiebreak at 6-6 in every set.
"""
from collections import defaultdict
from functools import lru_cache


def game(p: float) -> float:
    """P(server holds)."""
    q = 1 - p
    return p**4 * (1 + 4 * q + 10 * q * q) + 20 * p**3 * q**3 * p * p / (1 - 2 * p * q)


def tiebreak(pa: float, pb: float) -> float:
    """P(A wins a first-to-7 tiebreak), A serving the first point."""
    # At 6-6 (and every even score after), each 2-point pair has one serve each.
    win2, lose2 = pa * (1 - pb), (1 - pa) * pb
    deuce = win2 / (win2 + lose2)

    @lru_cache(None)
    def f(a, b):
        if a == 7 and b <= 5:
            return 1.0
        if b == 7 and a <= 5:
            return 0.0
        if a == 6 and b == 6:
            return deuce
        a_serves = ((a + b + 1) // 2) % 2 == 0
        p = pa if a_serves else 1 - pb
        return p * f(a + 1, b) + (1 - p) * f(a, b + 1)

    return f(0, 0)


def set_dist(pa: float, pb: float) -> dict[tuple[int, int], float]:
    """Distribution of final set scores (games A, games B), A serving first."""
    ha, hb = game(pa), game(pb)  # hold probabilities
    tb = tiebreak(pa, pb)
    out = defaultdict(float)
    states = {(0, 0): 1.0}
    while states:
        nxt = defaultdict(float)
        for (a, b), pr in states.items():
            if a == 6 and b == 6:
                out[(7, 6)] += pr * tb
                out[(6, 7)] += pr * (1 - tb)
                continue
            p = ha if (a + b) % 2 == 0 else 1 - hb
            for (na, nb), pp in (((a + 1, b), p), ((a, b + 1), 1 - p)):
                done = (max(na, nb) >= 6 and abs(na - nb) >= 2) or max(na, nb) == 7
                (out if done else nxt)[(na, nb)] += pr * pp
        states = nxt
    return dict(out)


def match(pa: float, pb: float, best_of: int = 3, a_serves_first: bool = True) -> dict:
    """Full market distribution for a match.

    Returns probabilities for match winner, set scores, total games, game handicap,
    first-set markets and tiebreak markets. All from A's perspective.
    """
    need = best_of // 2 + 1
    sd = {True: set_dist(pa, pb), False: {(b, a): p for (a, b), p in set_dist(pb, pa).items()}}

    # state: (sets A, sets B, A serves first in this set) -> {(games A, games B, had_tb): prob}
    joint = defaultdict(float)  # (sets A, sets B, games A, games B, had_tb) at match end
    states = {(0, 0, a_serves_first): {(0, 0, False): 1.0}}
    while states:
        nxt = defaultdict(lambda: defaultdict(float))
        for (sa, sb, srv), pmf in states.items():
            for (ga, gb), ps in sd[srv].items():
                nsa, nsb = sa + (ga > gb), sb + (gb > ga)
                nsrv = srv if (ga + gb) % 2 == 0 else not srv
                for (ta, tbg, had), pr in pmf.items():
                    key = (ta + ga, tbg + gb, had or ga + gb == 13)
                    if nsa == need or nsb == need:
                        joint[(nsa, nsb) + key] += pr * ps
                    else:
                        nxt[(nsa, nsb, nsrv)][key] += pr * ps
        states = nxt

    set_scores, total_games, handicap = defaultdict(float), defaultdict(float), defaultdict(float)
    games_a, games_b = defaultdict(float), defaultdict(float)
    tb_match = 0.0
    for (sa, sb, ga, gb, had), p in joint.items():
        set_scores[(sa, sb)] += p
        total_games[ga + gb] += p
        handicap[ga - gb] += p
        games_a[ga] += p
        games_b[gb] += p
        tb_match += p * had

    first = sd[a_serves_first]
    first_total = defaultdict(float)
    for (a, b), p in first.items():
        first_total[a + b] += p

    return {
        "match_winner": sum(p for (a, b), p in set_scores.items() if a == need),
        "correct_score_sets": dict(set_scores),
        "total_games": dict(sorted(total_games.items())),
        "game_handicap": dict(sorted(handicap.items())),     # games A - games B
        "player1_games": dict(sorted(games_a.items())),
        "player2_games": dict(sorted(games_b.items())),
        "1st_set_winner": sum(p for (a, b), p in first.items() if a > b),
        "1st_set_score": first,
        "1st_set_total_games": dict(sorted(first_total.items())),
        "1st_set_tiebreak": first_total[13],
        "tiebreak_in_match": tb_match,
    }


def over(pmf: dict[int, float], line: float) -> float:
    """P(X > line) for an integer pmf; use .5 lines."""
    return sum(p for k, p in pmf.items() if k > line)


def fit_to_price(pa: float, pb: float, target: float, best_of: int = 3) -> tuple[float, float]:
    """Shift serve strength between players (pa+d, pb-d) until P(A wins match) == target.

    Keeps the pair's total serve level (which drives totals/tiebreaks) from the box-score model,
    while taking the relative strength from the sharp market price.
    """
    lo, hi = -0.3, 0.3
    for _ in range(40):
        d = (lo + hi) / 2
        a, b = min(max(pa + d, 0.3), 0.95), min(max(pb - d, 0.3), 0.95)
        if match(a, b, best_of)["match_winner"] < target:
            lo = d
        else:
            hi = d
    return a, b
