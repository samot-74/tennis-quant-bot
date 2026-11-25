"""Point-on-serve probabilities for the Markov model from TML box scores.

SPW/RPW over each player's last N matches on the surface (18 months, strictly before `as_of`),
shrunk toward the tour/surface mean by K pseudo-points, combined Barnett-Clarke style:
    p_A = avg_spw + (spw_A - avg_spw) - (rpw_B - avg_rpw)
"""
from datetime import date, timedelta

from rapidfuzz import fuzz

from src.entity_resolver import tokens

N_MATCHES, K_POINTS, WINDOW_DAYS = 50, 1500, 548

POINTS_VIEW = """
CREATE OR REPLACE TEMP VIEW player_points AS
SELECT tour, tourney_date d, tourney_name, surface, winner_id pid,
       try_cast(w_svpt AS INT) svpt, try_cast(w_1stWon AS INT) + try_cast(w_2ndWon AS INT) sv_won,
       try_cast(l_svpt AS INT) rpt, try_cast(l_svpt AS INT) - try_cast(l_1stWon AS INT) - try_cast(l_2ndWon AS INT) rp_won
FROM matches
UNION ALL
SELECT tour, tourney_date, tourney_name, surface, loser_id,
       try_cast(l_svpt AS INT), try_cast(l_1stWon AS INT) + try_cast(l_2ndWon AS INT),
       try_cast(w_svpt AS INT), try_cast(w_svpt AS INT) - try_cast(w_1stWon AS INT) - try_cast(w_2ndWon AS INT)
FROM matches
"""


def _gender(con, pid: str) -> str:
    t = con.execute("SELECT any_value(tour) FROM player_points WHERE pid = ?", [pid]).fetchone()[0]
    return "wta" if t == "wta" else "atp"


def _tour_avg(con, gender: str, surface: str, as_of: date) -> tuple[float, float]:
    tours = ["wta"] if gender == "wta" else ["atp", "challenger", "atp_quali"]
    spw, rpw = con.execute(f"""
        SELECT sum(sv_won) / sum(svpt), sum(rp_won) / sum(rpt) FROM player_points
        WHERE tour IN ({','.join('?' * len(tours))}) AND surface = ? AND d < ? AND d >= ? AND svpt > 0 AND rpt > 0
    """, [*tours, surface, as_of, as_of - timedelta(days=WINDOW_DAYS)]).fetchone()
    return spw or 0.62, rpw or 0.38


def player_rates(con, pid: str, surface: str, as_of: date, avg: tuple[float, float]) -> dict:
    sv, svn, rp, rpn, n = con.execute("""
        SELECT coalesce(sum(sv_won),0), coalesce(sum(svpt),0), coalesce(sum(rp_won),0), coalesce(sum(rpt),0), count(*)
        FROM (SELECT * FROM player_points WHERE pid = ? AND surface = ? AND d < ? AND d >= ?
              AND svpt > 0 AND rpt > 0 ORDER BY d DESC LIMIT ?)
    """, [pid, surface, as_of, as_of - timedelta(days=WINDOW_DAYS), N_MATCHES]).fetchone()
    return {"spw": (sv + K_POINTS * avg[0]) / (svn + K_POINTS),
            "rpw": (rp + K_POINTS * avg[1]) / (rpn + K_POINTS), "n": n}


def serve_probs(con, id_a: str, id_b: str, surface: str, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    con.execute(POINTS_VIEW)
    avg = _tour_avg(con, _gender(con, id_a), surface, as_of)
    a, b = (player_rates(con, i, surface, as_of, avg) for i in (id_a, id_b))
    clamp = lambda x: min(max(x, 0.35), 0.92)
    return {"pa": clamp(avg[0] + (a["spw"] - avg[0]) - (b["rpw"] - avg[1])),
            "pb": clamp(avg[0] + (b["spw"] - avg[0]) - (a["rpw"] - avg[1])),
            "n_a": a["n"], "n_b": b["n"], "surface": surface}


def surface_for(con, tournament: str, ids: list[str]) -> str:
    """Same tournament in TML within the last ~13 months, else the players' latest surface, else Hard."""
    since = date.today() - timedelta(days=400)
    t = tokens(tournament.replace("(F)", ""))
    for name, surf in con.execute("""SELECT DISTINCT tourney_name, surface FROM matches
                                     WHERE tourney_date >= ? AND surface IS NOT NULL""", [since]).fetchall():
        if t and fuzz.token_set_ratio(" ".join(t), " ".join(tokens(name))) >= 85:
            return surf
    row = con.execute(f"""SELECT surface FROM matches WHERE tourney_date >= ? AND
                          (winner_id IN ({','.join('?' * len(ids))}) OR loser_id IN ({','.join('?' * len(ids))}))
                          ORDER BY tourney_date DESC LIMIT 1""",
                      [date.today() - timedelta(days=21), *ids, *ids]).fetchone()
    return row[0] if row else "Hard"
