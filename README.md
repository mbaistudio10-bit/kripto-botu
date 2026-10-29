# Kripto Botu

Multi-currency crypto trading bot. **Phase 4: trend strategy + backtest** on top of paper trading (simulated fills against the live OKX book, PnL in TRY and USD), the risk engine, kill switch, Telegram (Achilles) alerts and Jarvis link. No code sends orders to an exchange.
Not financial advice. Turkish setup guide: [KURULUM.md](KURULUM.md).

## Layout
```
config/settings.yaml          all user settings (validated at startup)
src/cryptobot/
  config.py                   typed settings (pydantic); only READ_ONLY / PAPER_TRADING allowed
  domain/models.py            Symbol, Instrument, Ticker, OrderBook (Decimal), Candle (float, analytics only)
  exchanges/base.py           ExchangeAdapter port (read-only)
  exchanges/okx.py            OKX v5 public REST + WebSocket adapter
  exchanges/registry.py       adapter lookup by name (add Binance / Bybit here)
  market_data/service.py      pair availability check, ticker stream/poll, order book refresh, staleness
  market_data/candles.py      candle history download + CSV cache (data/candles/), live CandleService
  market_data/fx.py           USD/TRY via USDT/TRY, 5-minute freshness rule
  risk/engine.py              Risk Engine + Kill-Switch: loss limits, drawdown, per-trade risk,
                              position count, FX freshness, TRY liquidity, delist rule
  risk/state.py               persisted halts/limits (data/risk_state.json)
  paper/portfolio.py          paper account (PortfolioView): balances, positions, PnL in TRY + USD
                              (data/paper_state.json)
  paper/trader.py             simulated taker fills (book walk + fee), intrabar stop/target, emergency close
  strategy/base.py            Strategy port: generate_signal(market, portfolio, risk) -> Signal
  strategy/indicators.py      causal EMA, ATR, higher-timeframe resampling (finished bars only)
  strategy/trend.py           TrendBreakoutStrategy (4h trend filter + 1h 20-bar breakout, ATR stop)
  strategy/exits.py           exit module: ATR stop, break-even, ATR trailing, time and trend exits
  strategy/runner.py          live: new closed candle -> exits, then signal -> PaperTrader.open (RiskEngine)
  backtest/engine.py          event-by-bar backtest: next-open fills, fees, slippage, gap stops, risk halts
  backtest/report.py          metrics + Turkish report; backtest/run.py: in-/out-of-sample CLI
  events.py                   event log (data/events.jsonl) + trade/stop-loss/error events
  notify/telegram.py          Telegram alerts + owner commands (/durum /limitler /rapor /durdur /devam /onayla)
  api/jarvis.py               local HTTP API for Jarvis (read + emergency stop/resume) and signed webhook
  report.py                   profit / coins / trades summary for the owner's "X report"
  envfile.py                  .env secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, JARVIS_* keys)
  status.py                   JSON snapshot (data/status.json), console table, Telegram /durum text
  __main__.py                 python -m cryptobot [--config PATH] [--once] [--backtest [--days N]]
tests/                        pytest, including a fake OKX REST/WS server
```

## Run
```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m cryptobot          # live table every 5s
PYTHONPATH=src python -m cryptobot --once   # one snapshot, exit code 0 if all pairs priced
PYTHONPATH=src python -m cryptobot --backtest --days 730   # report -> data/backtest_report.txt (+ Telegram)
PYTHONPATH=src python -m cryptobot --background  # no console table, no browser; restarts itself 60s after a crash
KriptoBotu.exe --autostart on|off               # Windows: start hidden at logon (Startup folder) and run now / remove
python -m pytest -q
```
The bot holds a single-instance lock (TCP port `panel.port - 1` on 127.0.0.1) and, with `keep_awake: true`,
keeps Windows from sleeping while it runs. `packaging/ArkaPlanda-Calistir.bat` / `ArkaPlan-Durdur.bat` wrap
`--autostart on/off` for the owner.

## Capital guard
`capital_guard` keeps equity above a floor: `base × (1 − max_loss_from_start_pct) + lock_profit_pct × max(0, peak − base)`
(defaults 2% and 50%). New positions are sized so that all open stops together cannot take equity below the floor
(budget split over the free position slots). Touching the floor raises a CLOSE_ALL `floor` halt; `/devam ONAY`
lifts it and rebuilds the base from current equity. The backtester applies the same rule. Fees and gaps can still
push equity slightly below the floor; this limits losses, it cannot rule them out.
Entries also need order-book imbalance within the depth band ≥ `liquidity.min_book_imbalance` (default −0.3).

## Strategy and backtest
- Entry (`strategy` in settings.yaml): on a closed 1h bar, LONG if the last *finished* 4h bar closes above
  its EMA50 and EMA20 > EMA50, the 1h close is above EMA50 and above the highest high of the previous
  20 bars, and ATR(14) is 0.3-6% of price. Invalidation (first stop) = close - 2.5 ATR.
- Exits (`exits`): intrabar stop; at bar close break-even at +1R (+20 bps), then trailing at 3 ATR below
  the best close, sell after 48 bars without reaching break-even, after 240 bars, or when the 4h trend breaks.
- Live: signals older than 10 minutes are ignored; every entry is sized by `RiskEngine.max_quantity`
  and must pass `RiskEngine.check_trade`. Exit state (initial stop, best close, break-even) persists in
  `data/paper_state.json`.
- Backtest: same `prepare`/`signal_at`/`on_bar_close` code as live; decisions at bar close fill at the next
  open; fee (`paper.fee_bps`) + `backtest.slippage_bps` per side; stop gaps fill at the open; risk-per-trade,
  max positions, 45% position cap, daily/weekly halts (UTC+3) and the drawdown stop are simulated.
  The period is split chronologically (70% in-sample / 30% out-of-sample); parameters are fixed, not fitted.
  After a drawdown halt the test pauses `backtest.drawdown_pause_days` (7) and resumes with a peak reset
  (simulated "/devam ONAY"), so the whole period is measured.
- Variants (`backtest/variants.py`, `compare_variants`): the current settings plus two fixed slower rule sets
  (4h candles + daily trend, daily candles + daily trend), all built by resampling the downloaded 1h candles and
  traded over identical dates. The verdict requires profit and PF >= 1.1 out of sample with >= 10 trades.
  A test (`test_no_look_ahead_full_history_equals_truncated_history`) checks that decisions on full history
  equal decisions on history cut at that bar. On pure random walks the strategy loses slightly (costs), as it should.

## Kill-switch levels
| Level | Trigger | Cleared by |
|---|---|---|
| NO_NEW_POSITIONS | daily loss limit | next local day (cannot be overridden) |
| NO_NEW_POSITIONS | weekly loss limit | next Monday (cannot be overridden) |
| NO_NEW_POSITIONS | `/durdur` or Jarvis stop | `/devam` (Jarvis may only lift its own stop) |
| NO_NEW_POSITIONS | pair delisted with an open position | owner `/devam` |
| CLOSE_ALL | total drawdown limit | owner `/devam ONAY` (peak reset) |
| CLOSE_ALL | capital floor reached | owner `/devam ONAY` (guard base reset) |

Looser risk limits in settings.yaml wait for `/onayla`; tighter ones apply immediately. Changes are logged.

## Jarvis API
Runs on `127.0.0.1:8765` (config `jarvis`). Keys are generated into `.env` on first run.
Send `Authorization: Bearer <key>`.

| Method | Path | Key |
|---|---|---|
| GET | `/api/v1/health`, `/api/v1/status`, `/api/v1/limits` | read or control |
| GET | `/api/v1/events?since=<ISO-8601>&limit=100` | read or control |
| GET | `/api/v1/report?period=gün\|hafta\|ay` (categories: Kazanç, Kripto birimleri, Al-sat işlemleri + Turkish `text`) | read or control |
| GET | `/api/v1/daily-summary?date=YYYY-MM-DD` (default: latest; 404 if none) | read or control |
| GET | `/api/v1/overview`: status + `health` score + `reports` (gün/hafta/ay) + `limits_text` + last 50 `events` + `backtest` summary | read or control |
| POST | `/api/v1/control/stop` body `{"reason": "..."}` | control |
| POST | `/api/v1/control/resume` | control |

There is no endpoint to place orders, change limits or lift loss/drawdown halts.
If `JARVIS_WEBHOOK_URL` is set, events are POSTed as JSON with header
`X-KriptoBotu-Signature: hex(HMAC-SHA256(JARVIS_READ_KEY, body))`. With `jarvis.webhook_events: summary`
(default) only the `daily_summary` event is sent, retried with backoff for about 24 h; `all` sends every notified event.

The daily summary (`daily_summary.at`, default 23:55 local) goes to Telegram, the webhook and
`data/daily_summaries.jsonl`; a summary missed while the bot was off is written at the next start.
The bot never depends on Jarvis being up.

```
curl -H "Authorization: Bearer $JARVIS_READ_KEY" "http://127.0.0.1:8765/api/v1/report?period=gün"
curl -X POST -H "Authorization: Bearer $JARVIS_CONTROL_KEY" -d '{"reason":"acil"}' http://127.0.0.1:8765/api/v1/control/stop
```

## Control Center (panel)
Served on port 8770 (config `panel`; `home_network: true` binds 0.0.0.0 so a phone on the same Wi-Fi can open it).
`PANEL_KEY` is generated into `.env`; links carry it after `#k=` so it never reaches a server log.
`GET /panel/api/state` (same JSON as Jarvis `/overview`), `POST /panel/api/stop`, `POST /panel/api/resume`
(`{"confirm": true}` also lifts a drawdown halt). No order or limit endpoints. 10 wrong keys per address → 15 min lockout.
The page is self-contained HTML/JS (no external scripts), with a web manifest and service worker for "install as app".

## Roadmap
1. Skeleton, config, exchange adapter, market data (done)
2. Risk engine, kill switch, Telegram alerts/commands, Jarvis link (done)
3. Paper trading + PnL in TRY and USD (done)
4. Simple trend strategy + backtest (done)
5. Control Center: one-screen web app for PC, phone and tablet (installable), health score, Jarvis overview (done)
6. Capital guard, order-book entry filter, daily summary for Jarvis, background running (this)
7. Later, only after weeks of paper results: news/on-chain filters; small live spot with owner approval.
   Futures and leverage are left out on purpose (lowest risk).

