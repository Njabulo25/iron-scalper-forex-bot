"""Broker settings for South African micro-account trading.

Set ``ACTIVE_BROKER`` to ``"RCG"``, ``"VAULT_MARKETS"``, or leave it as
``"DEFAULT"``. Unknown broker keys intentionally fall back to ``DEFAULT``.
"""


RCG = {
    "min_deposit": 100,
    "min_lot": 0.01,
    "max_lot": 10.0,
    "lot_step": 0.01,
    "leverage": "1:500",
    "currency": "ZAR",
    "xauusd_contract_size": 100,
    "spread_typical": 30,
}

VAULT_MARKETS = {
    "min_deposit": 100,
    "min_lot": 0.01,
    "max_lot": 10.0,
    "lot_step": 0.01,
    "leverage": "1:500",
    "currency": "ZAR",
    "xauusd_contract_size": 100,
    "spread_typical": 30,
}

DEFAULT = {
    "min_deposit": 100,
    "min_lot": 0.01,
    "max_lot": 10.0,
    "lot_step": 0.01,
    "leverage": "1:500",
    "currency": "ZAR",
    "xauusd_contract_size": 100,
    "spread_typical": 30,
}

BROKERS = {
    "RCG": RCG,
    "VAULT_MARKETS": VAULT_MARKETS,
    "DEFAULT": DEFAULT,
}

ACTIVE_BROKER = "DEFAULT"


def get_broker_config():
    """Return a copy of the selected broker configuration or the fallback."""
    return BROKERS.get(ACTIVE_BROKER, DEFAULT).copy()
