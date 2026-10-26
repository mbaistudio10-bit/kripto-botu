# Kripto Botu

Multi-currency crypto trading bot. **Phase 1: read-only market data.** No order code exists yet.
Not financial advice. Turkish setup guide: [KURULUM.md](KURULUM.md).

## Layout
```
config/settings.yaml          all user settings (validated at startup)
src/cryptobot/
  config.py                   typed settings (pydantic); Phase 1 allows READ_ONLY / PAPER_TRADING only
  domain/models.py            Symbol, Instrument, Ticker, OrderBook (Decimal everywhere)
  exchanges/base.py           ExchangeAdapter port (read-only)
  exchanges/okx.py            OKX v5 public REST + WebSocket adapter
  exchanges/registry.py       adapter lookup by name (add Binance / Bybit here)
  market_data/service.py      pair availability check, ticker stream/poll, order book refresh, staleness
  market_data/fx.py           USD/TRY via USDT/TRY, 5-minute freshness rule
  status.py                   JSON snapshot (data/status.json) + Turkish console table
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

## Roadmap
1. Skeleton, config, exchange adapter, market data (this)
2. Risk engine + kill switch
3. Paper trading + PnL in TRY and USD
4. Simple trend strategy + backtest
5. Phone dashboard + Telegram alerts
