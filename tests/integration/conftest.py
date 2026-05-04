import os
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env.testnet")

from utils.general import build_client
from utils import account as account_mod
from utils import orders as orders_mod
from utils import positions as positions_mod


@pytest.fixture(scope="session", autouse=True)
def require_testnet():
    missing = [k for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET") if not os.getenv(k)]
    if missing:
        pytest.skip(f"Missing in .env.testnet: {missing}")
    if os.getenv("BINANCE_TESTNET", "").lower() != "true":
        pytest.skip("BINANCE_TESTNET != 'true' in .env.testnet — refusing to run against production")


@pytest.fixture(scope="session")
def client():
    return build_client(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
        testnet=True,
    )


@pytest.fixture(scope="session")
def symbol() -> str:
    return "BTCUSDT"


@pytest.fixture(scope="session")
def sym_info(client, symbol) -> dict:
    return account_mod.get_symbol_info(client, symbol)


@pytest.fixture
def clean_orders(client, symbol):
    """Cancel all open orders before and after each test."""
    orders_mod.cancel_all_orders(client, symbol)
    yield
    orders_mod.cancel_all_orders(client, symbol)


@pytest.fixture
def open_position(client, symbol, sym_info):
    """Open a minimal long position. Cleans up orders and position on teardown."""
    orders_mod.cancel_all_orders(client, symbol)
    positions_mod.close_position(client, symbol)

    qty = max(Decimal("0.001"), sym_info["min_qty"]).quantize(sym_info["step_size"])
    orders_mod.place_market_order(client, symbol, "BUY", qty)
    pos = account_mod.get_futures_positions(client, symbol)[0]
    yield pos

    try:
        orders_mod.cancel_all_orders(client, symbol)
    except Exception:
        pass
    try:
        positions_mod.close_position(client, symbol)
    except Exception:
        pass
