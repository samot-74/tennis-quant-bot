"""Vig removal (Shin), edge, and fractional Kelly staking."""
import math


def shin_probs(odds: list[float], tol: float = 1e-12) -> list[float]:
    """Fair probabilities from bookmaker decimal odds via Shin's method.

    Solves for insider share z in [0, 1) such that the implied probabilities sum to 1.
    """
    pi = [1 / o for o in odds]
    s = sum(pi)
    if s <= 1:  # no overround (e.g. exchange) -> just normalise
        return [x / s for x in pi]

    def probs(z):
        return [(math.sqrt(z * z + 4 * (1 - z) * x * x / s) - z) / (2 * (1 - z)) for x in pi]

    lo, hi = 0.0, 0.999
    while hi - lo > tol:  # sum(probs) is decreasing in z
        mid = (lo + hi) / 2
        if sum(probs(mid)) > 1:
            lo = mid
        else:
            hi = mid
    return probs((lo + hi) / 2)


def edge(p_model: float, odd: float) -> float:
    return p_model * odd - 1


def kelly_stake(p: float, odd: float, bankroll: float,
                fraction: float = 0.25, max_pct: float = 0.025, round_to: float = 1,
                min_stake: float = 1, min_trigger: float = 0.5) -> float:
    """Fractional Kelly, capped, rounded DOWN to `round_to` EUR.

    Anything from `min_trigger` up to the first rounding step becomes `min_stake` (small bankrolls).
    The daily cap is applied when sending (telegram_notifier.notify), best edge first.
    """
    b = odd - 1
    f = (b * p - (1 - p)) / b
    if f <= 0:
        return 0.0
    stake = min(fraction * f, max_pct) * bankroll
    if stake < min_trigger:
        return 0.0
    return max(min_stake, math.floor(stake / round_to + 1e-9) * round_to)
