"""Pick an adapter by name from the config. Add new exchanges here."""

from __future__ import annotations

from collections.abc import Callable

from cryptobot.config import ExchangeConfig
from cryptobot.exchanges.base import ExchangeAdapter
from cryptobot.exchanges.okx import OkxAdapter

_ADAPTERS: dict[str, Callable[[ExchangeConfig], ExchangeAdapter]] = {
    "okx": OkxAdapter,
    # "binance": BinanceAdapter, "binance_tr": ..., "bybit": ...  (later phases)
}


def create_adapter(config: ExchangeConfig) -> ExchangeAdapter:
    factory = _ADAPTERS.get(config.name.lower())
    if factory is None:
        raise ValueError(f"unknown exchange {config.name!r}; available: {sorted(_ADAPTERS)}")
    return factory(config)
