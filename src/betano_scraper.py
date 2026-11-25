"""Betano.pt pre-match tennis odds.

Betano server-renders every page into `window.initial_state`; the JSON /api/ endpoints answer 403
to direct calls, so we just open the pages in a real (visible) browser and read that state:
  /sport/tenis/                -> list of leagues
  league page                  -> events (ids, players, kickoff)
  /odds/<slug>/<id>/?bt=5      -> every market of one event ("Todos os mercados" tab)

Phase A reads only the league pages (they already carry Vencedor for every match);
phase B opens ?bt=5 only for the events the pipeline selects (see edge_detector.deep_scan).
Runs in the Windows-installed Edge (channel="msedge") with its own persistent profile.

python -m src.betano_scraper            # scrape, write data/processed/betano_today.json + DuckDB
"""
import json
import sys
import time
from datetime import datetime, timezone

from src.db import ROOT, connect

BASE = "https://www.betano.pt"
OUT = ROOT / "data" / "processed" / "betano_today.json"
SKIP_LEAGUE_WORDS = ("duplas", "vencedor final", "h2h", "especiais")


# ---------- pure parsing (tested on saved samples) ----------

def league_urls(index_state: dict) -> list[str]:
    d = index_state["data"]
    urls = {l["url"] for l in d.get("topLeagues", [])}
    for g in d.get("regionGroups", []):
        for r in g["regions"]:
            urls |= {l["url"] for l in r["leagues"]}
    return sorted(u for u in urls if not any(w in u.replace("-", " ") for w in SKIP_LEAGUE_WORDS))


def league_events(league_state: dict) -> list[dict]:
    out = []
    for block in league_state["data"].get("blocks", []):
        for e in block.get("events", []):
            ps = e.get("participants", [])
            if len(ps) != 2 or any("/" in p["name"] for p in ps) or e.get("liveNow"):
                continue
            out.append({"event_id": e["id"], "url": e["url"], "region": e.get("regionName", ""),
                        "tournament": e.get("leagueName", ""), "player1": ps[0]["name"],
                        "player2": ps[1]["name"],
                        "kickoff": datetime.fromtimestamp(e["startTime"] / 1000, timezone.utc)})
    return out


def _side(sel_name: str, p1: str, p2: str) -> str | None:
    if sel_name.startswith(p1):
        return "p1"
    if sel_name.startswith(p2):
        return "p2"
    return None


def _ou(sel_name: str) -> str | None:
    return "over" if sel_name.startswith("Mais") else "under" if sel_name.startswith("Menos") else None


def parse_markets(event_state: dict, ev: dict) -> list[dict]:
    """Event page state -> normalised rows (market, selection, line, odd). Unmapped markets are dropped."""
    p1, p2 = ev["player1"], ev["player2"]
    rows, seen = [], set()
    for m in event_state["data"]["event"]["markets"]:
        t, name = m["type"], m["name"]
        set1 = "(Set 1)" in name
        for s in m["selections"]:
            sn, line = s["name"], s.get("handicap") or 0.0
            mk = sel = None
            if t == "HTOH":
                mk, sel = "match_winner", ("p1", "p2")[s.get("columnIndex", 0)]
            elif t == "STWN" and set1:
                mk, sel = "set1_winner", ("p1", "p2")[s.get("columnIndex", 0)]
            elif t == "FTGO":
                mk, sel = "total_games", _ou(sn)
            elif t == "FSTO" and set1:
                mk, sel = "set1_total_games", _ou(sn)
            elif t in ("OUG1", "OUG2"):
                mk, sel = ("player1_games" if t == "OUG1" else "player2_games"), _ou(sn)
            elif t == "TGHC":
                mk, sel = "game_handicap", _side(sn, p1, p2)
            elif "handicap" in name.lower() and "set" in name.lower():
                mk, sel = "set_handicap", _side(sn, p1, p2)
            elif "tie" in name.lower() and "break" in name.lower():
                mk = "set1_tiebreak" if set1 else "tiebreak_in_match"
                sel = {"sim": "yes", "não": "no", "nao": "no"}.get(sn.strip().lower())
            if mk and sel and s.get("price"):
                key = (mk, sel, line)
                if key not in seen:  # Betano repeats some markets under several tabs
                    seen.add(key)
                    rows.append({**{k: ev[k] for k in ("event_id", "region", "tournament", "player1",
                                                         "player2", "kickoff")},
                                 "market": mk, "selection": sel, "line": float(line), "odd": float(s["price"])})
    return rows


# ---------- browser ----------

def _state(page, url: str, wait_s: int = 60) -> dict:
    resp = page.goto(BASE + url, wait_until="domcontentloaded")
    blocked = (resp and resp.status == 403) or "splash" in page.title().lower()
    if blocked:
        print(f"Betano blocked {url} (403 splash). Solve any check in the window; waiting {wait_s}s.",
              file=sys.stderr)
    for _ in range(wait_s * 2):
        st = page.evaluate("() => window.initial_state || null")
        if st and st.get("data"):
            return st
        page.wait_for_timeout(500)
    raise BlockedError(url) if blocked else RuntimeError(f"no initial_state on {url}")


class BlockedError(RuntimeError):
    """Betano refused the page. We stop the run instead of retrying around it."""


def _browser(p):
    """The Edge installed on Windows (not Playwright's bundled Chromium), own persistent profile."""
    return p.chromium.launch_persistent_context(
        str(ROOT / "data" / "browser_profile_edge"), channel="msedge", headless=False, locale="pt-PT")


def _visit(urls_fn, delay_s: float):
    """Open one browser, call urls_fn(page) which yields results; stop at the first block."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _browser(p)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return urls_fn(page, delay_s)
        finally:
            ctx.close()


def scrape_leagues(hours_ahead: float = 36, delay_s: float = 4.0) -> tuple[list[dict], list[dict]]:
    """Phase A: index + one page per league. Returns (events, match-winner rows for all of them)."""
    now = datetime.now(timezone.utc)

    def run(page, delay):
        events, rows = [], []
        for lu in league_urls(_state(page, "/sport/tenis/")):
            time.sleep(delay)
            try:
                st = _state(page, lu)
            except BlockedError:
                raise
            except Exception as e:
                print(f"league {lu}: {e}", file=sys.stderr)
                continue
            raw = {e["id"]: e for b in st["data"].get("blocks", []) for e in b.get("events", [])}
            for ev in league_events(st):
                if 0 < (ev["kickoff"] - now).total_seconds() < hours_ahead * 3600:
                    events.append(ev)
                    rows += parse_markets({"data": {"event": raw[ev["event_id"]]}}, ev)
        print(f"Phase A: {len(events)} pre-match events from league pages")
        return events, rows

    return _visit(run, delay_s)


def scrape_events(events: list[dict], delay_s: float = 4.0) -> list[dict]:
    """Phase B: full market list (?bt=5) for the chosen events only."""
    if not events:
        return []

    def run(page, delay):
        rows = []
        for i, ev in enumerate(events, 1):
            time.sleep(delay)
            try:
                rows += parse_markets(_state(page, ev["url"] + "?bt=5"), ev)
            except BlockedError:
                raise
            except Exception as e:
                print(f"event {ev['url']}: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  {i}/{len(events)}")
        return rows

    return _visit(run, delay_s)


def save(rows: list[dict], run_tag: str):
    ts = datetime.now(timezone.utc)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, default=str, ensure_ascii=False, indent=1), encoding="utf-8")
    if not rows:
        return
    con = connect()
    con.executemany(
        "INSERT INTO betano_odds VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, run_tag, r["event_id"], r["region"], r["tournament"], r["player1"], r["player2"], r["kickoff"],
          r["market"], r["selection"], r["line"], r["odd"]) for r in rows])


if __name__ == "__main__":
    evs, rows = scrape_leagues()
    save(rows, sys.argv[1] if len(sys.argv) > 1 else "T1")
    print(f"{len(rows)} odds rows, {len({r['event_id'] for r in rows})} events -> {OUT}")
