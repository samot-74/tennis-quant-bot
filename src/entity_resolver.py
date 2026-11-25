"""Player name matching across Betano ("Ryan Nijboer"), OddsPortal ("Nijboer R." / slug "nijboer-ryan")
and TennisMyLife ("Ryan Nijboer").

Anti-collision rule: surnames must match AND every leftover first-name token/initial must agree,
so "Cerundolo F." never matches "Juan Manuel Cerundolo", nor "Zverev M." -> "Alexander Zverev".
"""
import re
import unicodedata
from datetime import date, timedelta

from rapidfuzz import fuzz


def tokens(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z]+", s) if t]


def same_player(a: str, b: str) -> float:
    """0..100 similarity, 0 if the names cannot be the same person."""
    ta, tb = tokens(a), tokens(b)
    fa, fb = [t for t in ta if len(t) > 1], [t for t in tb if len(t) > 1]
    shared = [(x, y) for x in fa for y in fb if fuzz.ratio(x, y) >= 90]
    if not shared:
        return 0.0
    used_a, used_b = {x for x, _ in shared}, {y for _, y in shared}
    rest_a, rest_b = [t for t in ta if t not in used_a], [t for t in tb if t not in used_b]
    if rest_a and rest_b:
        # an initial must match the first letter of some leftover token on the other side
        ia, ib = {t[0] for t in rest_a}, {t[0] for t in rest_b}
        if not (ia & ib):
            return 0.0
        full_a, full_b = [t for t in rest_a if len(t) > 1], [t for t in rest_b if len(t) > 1]
        if full_a and full_b and max(fuzz.ratio(x, y) for x in full_a for y in full_b) < 80:
            return 0.0  # two different full first names (siblings)
    # checks passed: score on the overlap, so "Pucinelli M." vs a 4-word full name isn't penalised
    return fuzz.token_set_ratio(" ".join(ta), " ".join(tb))


def best_match(name: str, candidates: dict[str, str], min_score: float = 70) -> tuple[str, float] | None:
    """candidates: {id: name}. Returns (id, score) or None. Refuses ties (ambiguous)."""
    scored = sorted(((same_player(name, n), i) for i, n in candidates.items()), reverse=True)
    if not scored or scored[0][0] < min_score:
        return None
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 3:
        return None  # two near-equal candidates -> don't guess
    return scored[0][1], scored[0][0]


def resolve_tml(con, names: list[str], source: str = "betano") -> dict[str, str]:
    """Map names to TML player ids (players active in the last 2 years), cached in player_map."""
    cached = dict(con.execute("SELECT source_name, player_id FROM player_map WHERE source = ?",
                              [source]).fetchall())
    todo = [n for n in set(names) if n not in cached]
    if todo:
        since = date.today() - timedelta(days=730)
        rows = con.execute("""
            SELECT winner_id, winner_name FROM matches WHERE tourney_date >= ?
            UNION SELECT loser_id, loser_name FROM matches WHERE tourney_date >= ?""",
                           [since, since]).fetchall()
        by_surname = {}
        for pid, pname in rows:
            for t in tokens(pname):
                if len(t) > 1:
                    by_surname.setdefault(t, {})[pid] = pname
        for n in todo:
            cands = {}
            for t in tokens(n):
                cands.update(by_surname.get(t, {}))
            hit = best_match(n, cands)
            if hit:
                cached[n] = hit[0]
                con.execute("INSERT OR REPLACE INTO player_map VALUES (?,?,?,?)", [source, n, hit[0], hit[1]])
    return {n: cached[n] for n in names if n in cached}
