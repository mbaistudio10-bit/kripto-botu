# Kripto Botu

Multi-currency crypto trading bot. **Phase 2: risk engine, kill switch, Telegram (Achilles) alerts, Jarvis link.** No order code exists yet.
Not financial advice. Turkish setup guide: [KURULUM.md](KURULUM.md).

## Layout
```
config/settings.yaml          all user settings (validated at startup)
src/cryptobot/
  config.py                   typed settings (pydantic); only READ_ONLY / PAPER_TRADING allowed
  domain/models.py            Symbol, Instrument, Ticker, OrderBook (Decimal everywhere)
  exchanges/base.py           ExchangeAdapter port (read-only)
  exchanges/okx.py            OKX v5 public REST + WebSocket adapter
  exchanges/registry.py       adapter lookup by name (add Binance / Bybit here)
  market_data/service.py      pair availability check, ticker stream/poll, order book refresh, staleness
  market_data/fx.py           USD/TRY via USDT/TRY, 5-minute freshness rule
  risk/engine.py              Risk Engine + Kill-Switch: loss limits, drawdown, per-trade risk,
                              position count, FX freshness, TRY liquidity, delist rule
  risk/state.py               persisted halts/limits (data/risk_state.json)
  events.py                   event log (data/events.jsonl) + trade/stop-loss/error events
  notify/telegram.py          Telegram alerts + owner commands (/durum /limitler /rapor /durdur /devam /onayla)
  api/jarvis.py               local HTTP API for Jarvis (read + emergency stop/resume) and signed webhook
  report.py                   profit / coins / trades summary for the owner's "X report"
  envfile.py                  .env secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, JARVIS_* keys)
  status.py                   JSON snapshot (data/status.json), console table, Telegram /durum text
  __main__.py                 python -m cryptobot [--config PATH] [--once]
tests/                        pytest, including a fake OKX REST/WS server
```

## Run
```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m cryptobot          # live table every 5s
PYTHONPATH=src python -m cryptobot --once   # one snapshot, exit code 0 if all pairs priced
python -m pytest -q
```

## Kill-switch levels
| Level | Trigger | Cleared by |
|---|---|---|
| NO_NEW_POSITIONS | daily loss limit | next local day (cannot be overridden) |
| NO_NEW_POSITIONS | weekly loss limit | next Monday (cannot be overridden) |
| NO_NEW_POSITIONS | `/durdur` or Jarvis stop | `/devam` (Jarvis may only lift its own stop) |
| NO_NEW_POSITIONS | pair delisted with an open position | owner `/devam` |
| CLOSE_ALL | total drawdown limit | owner `/devam ONAY` (peak reset) |

Looser risk limits in settings.yaml wait for `/onayla`; tighter ones apply immediately. Changes are logged.

## Jarvis API
Runs on `127.0.0.1:8765` (config `jarvis`). Keys are generated into `.env` on first run.
Send `Authorization: Bearer <key>`.

| Method | Path | Key |
|---|---|---|
| GET | `/api/v1/health`, `/api/v1/status`, `/api/v1/limits` | read or control |
| GET | `/api/v1/events?since=<ISO-8601>&limit=100` | read or control |
| GET | `/api/v1/report?period=gün\|hafta\|ay` (categories: Kazanç, Kripto birimleri, Al-sat işlemleri + Turkish `text`) | read or control |
| POST | `/api/v1/control/stop` body `{"reason": "..."}` | control |
| POST | `/api/v1/control/resume` | control |

There is no endpoint to place orders, change limits or lift loss/drawdown halts.
If `JARVIS_WEBHOOK_URL` is set, every notified event is POSTed as JSON with header
`X-KriptoBotu-Signature: hex(HMAC-SHA256(JARVIS_READ_KEY, body))`.

```
curl -H "Authorization: Bearer $JARVIS_READ_KEY" "http://127.0.0.1:8765/api/v1/report?period=gün"
curl -X POST -H "Authorization: Bearer $JARVIS_CONTROL_KEY" -d '{"reason":"acil"}' http://127.0.0.1:8765/api/v1/control/stop
```

## Roadmap
1. Skeleton, config, exchange adapter, market data (done)
2. Risk engine, kill switch, Telegram alerts/commands, Jarvis link (this)
3. Paper trading + PnL in TRY and USD (emits trade_opened / trade_closed / stop_loss_hit)
4. Simple trend strategy + backtest
5. Phone status page
