# tennis-quant-bot

Betano.pt vs Pinnacle (same market, same line, Shin de-vig) → +EV alerts on Telegram.
Markov (SPW/RPW from TennisMyLife) only prices markets Pinnacle doesn't offer for that match.

| File | What |
|---|---|
| `src/oddspapi.py` | OddsPapi: fixtures + odds-by-tournaments for `betano.pt` and `pinnacle` (5 tournaments per request), monthly budget guard (`api_usage`) |
| `src/edge_detector.py` | Pinnacle fair price per market/line; Markov fallback; tier thresholds; one bet per event+market; ¼-Kelly |
| `src/telegram_notifier.py` | Alerts, de-dup via `sent_alerts` (re-send only if edge +2 pp), daily 10% cap |
| `src/markov.py`, `src/serve_stats.py`, `src/entity_resolver.py`, `src/historical_loader.py` | Markov model, serve stats, name matching, TML data |
| `src/pipeline.py` | One run (`--t=` limits it to some tournaments) |
| `scripts/run_pipeline.ps1` | What the Windows tasks call; logs to `data/logs/YYYY-MM-DD.log` |
| `src/betano_scraper.py`, `src/odds_scraper.py`, `scripts/capture_betano.py` | Old direct-scraping path (Betano blocks automated browsers); not used by the pipeline |

```powershell
pip install -r requirements.txt
copy .env.example .env   # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BANKROLL_EUR, ODDSPAPI_KEY
python -X utf8 -m src.historical_loader      # once: full history
python -X utf8 -m src.pipeline T1
python -X utf8 -m pytest -q
```

Free OddsPapi tier: 250 requests/month; a run costs 1 + 2 × ceil(tournaments / 5) ≈ 5, so one daily send ≈ 5–7 requests (≈150–210/month).
The PC must be on and you logged in at run time; missed runs start when it wakes, but alerts for matches already started are skipped.

## Daily send (07:15)

One send a day, when you place every bet: 07:15 Lisbon is before the first European Challenger matches
(10:00 local in Greece/Turkey = 08:00 Lisbon) and covers Europe + the Americas (~85% of the day).
Overnight Asia/Australia is usually not scheduled yet at that hour. Each alert says the minimum odd:
if Betano has dropped below it by the time you bet, skip it. Windows task (PowerShell, once):

```powershell
$a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -File "<project>\scripts\run_pipeline.ps1" pipeline T1' -WorkingDirectory "<project>"
Register-ScheduledTask -TaskPath '\tennis-quant-bot\' -TaskName daily -Action $a -Trigger (New-ScheduledTaskTrigger -Daily -At 07:15) -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) -Force
```
