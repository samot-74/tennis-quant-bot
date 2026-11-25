"""Telegram alerts with de-duplication through `sent_alerts`.

An alert is (re)sent only if that (event, market, selection+line) was never sent, or its edge has
risen by >= realert_edge_jump since the last send. Today's already-sent stakes count toward the
daily exposure cap. Without TELEGRAM_BOT_TOKEN the messages are printed instead (dry run).
"""
import html
import math
import os
from datetime import date, datetime

import requests

from src.edge_detector import CFG

MARKET_PT = {"match_winner": "Vencedor", "total_games": "Total de Jogos", "game_handicap": "Handicap de Jogos",
             "set_handicap": "Handicap de Sets", "set1_winner": "Vencedor 1.º Set",
             "set1_total_games": "Total de Jogos 1.º Set", "player1_games": "Jogos ganhos",
             "player2_games": "Jogos ganhos", "tiebreak_in_match": "Tie-break no jogo",
             "set1_tiebreak": "Tie-break 1.º Set"}


def selection_label(a: dict) -> str:
    sel, line = a["selection"], a["line"]
    if sel in ("p1", "p2"):
        name = a["player1"] if sel == "p1" else a["player2"]
        return f"{name} {line:+g}" if a["market"].endswith("handicap") else name
    if sel in ("over", "under"):
        who = {"player1_games": a["player1"] + " ", "player2_games": a["player2"] + " "}.get(a["market"], "")
        return f"{who}{'Mais' if sel == 'over' else 'Menos'} de {line:g}"
    return {"yes": "Sim", "no": "Não"}[sel]


def key(a: dict) -> tuple[str, str, str]:
    return a["event_id"], a["market"], f"{a['selection']}:{a['line']:g}"


def format_alert(a: dict) -> str:
    e = html.escape
    lines = [
        f"🎾 <b>{e(a['player1'])} vs {e(a['player2'])}</b>",
        f"🏆 {e(' – '.join(x for x in (a['region'], a['tournament']) if x))} ({a['tier']})"
        f" · {a['kickoff']:%d/%m %H:%M} UTC",
        f"📊 {MARKET_PT.get(a['market'], a['market'])}: <b>{e(selection_label(a))}</b>",
        f"💶 Odd Betano <b>{a['odd']:.2f}</b> vs odd justa {a['fair_odd']:.2f} ({e(a['basis'])})",
        f"📈 Edge <b>{a['edge']:+.1%}</b> · P modelo {a['p_model']:.1%} vs implícita Betano {1 / a['odd']:.1%}",
        f"💰 Stake sugerida: <b>{a['stake']:.0f} €</b> · só se a odd ainda for ≥ <b>{a['min_odd']:.2f}</b>",
    ]
    if a.get("ref"):
        r = a["ref"]
        lines.append(f"⚖️ Justa {r['source']}: {r['p1']:.1%} / {r['p2']:.1%}")
    if a.get("markov"):
        mk = a["markov"]
        lines.append(f"🔗 Markov ({mk['surface']}, n={mk['n'][0]}/{mk['n'][1]}): vencedor {mk['match_winner']:.1%}"
                     f" · p_serv {mk['pa']:.3f}/{mk['pb']:.3f}")
    return "\n".join(lines)


def send(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(text, "\n")
        return True
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text[:200]}")
    return r.ok


def notify(con, alerts: list[dict], bankroll: float | None = None, summary: bool = False) -> int:
    """Send new alerts (de-dup + daily cap). summary=True: a header first, also when there is nothing to bet."""
    bankroll = bankroll or float(os.getenv("BANKROLL_EUR", "1000"))
    last = {(m, k, s): e for m, k, s, e in con.execute("""
        SELECT match_id, market, selection, arg_max(edge_sent, ts) FROM sent_alerts GROUP BY ALL""").fetchall()}
    spent = con.execute("SELECT coalesce(sum(stake), 0) FROM sent_alerts WHERE ts::DATE = ?",
                        [date.today()]).fetchone()[0]
    room = CFG["max_daily_pct"] * bankroll - spent
    todo = []
    for a in alerts:
        k = key(a)
        if k in last and a["edge"] < last[k] + CFG["realert_edge_jump"]:
            continue
        stake = math.floor(min(a["stake"], room) / CFG["stake_round_eur"] + 1e-9) * CFG["stake_round_eur"]
        if stake <= 0:
            print("daily exposure cap reached")
            break
        todo.append({**a, "stake": stake})
        room -= stake
    if summary:
        now = f"{datetime.now():%d/%m %H:%M}"
        send(f"📋 <b>Apostas de hoje</b> ({now}): {len(todo)} · total {sum(a['stake'] for a in todo):.0f} €\n"
             "Odds de agora: se na Betano já estiver abaixo da odd mínima indicada, salta essa."
             if todo else f"📋 <b>Hoje sem apostas</b> ({now}): nenhuma odd da Betano chega ao edge mínimo face à Pinnacle.")
    sent = 0
    for a in todo:
        if send(format_alert(a)):
            con.execute("INSERT INTO sent_alerts (match_id, market, selection, odd_sent, edge_sent, stake) "
                        "VALUES (?,?,?,?,?,?)", [*key(a), a["odd"], a["edge"], a["stake"]])
            sent += 1
    return sent

