"""Independent Real-time Market Data & Settlement Collector.

Streams Binance BTC/USDT price and Polymarket CLOB books, saving
raw immutable inputs into data/1_market/ without sharing or leaking state.
"""
import sys
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config

BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
POLYMARKET_MARKETS_URL = "https://clob.polymarket.com/markets"


def fetch_btc_spot_price() -> float:
    """Fetch live BTC/USDT spot price."""
    try:
        req = urllib.request.Request(BINANCE_PRICE_URL, headers={"User-Agent": "pm-quant/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return float(data.get("price", 0.0))
    except Exception:
        return 0.0


def save_market_event(category: str, event_data: dict):
    """Append raw market stream record to data/1_market/."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    out_dir = config.MARKET_DIR / category / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{category}_{today}.jsonl"
    with open(out_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data) + "\n")


if __name__ == "__main__":
    print("Testing market data connectivity...")
    px = fetch_btc_spot_price()
    print(f"BTC Spot Price: ${px:,.2f}")
    if px > 0:
        save_market_event("spot", {"t": time.time(), "price": px, "source": "binance"})
        print("Market record written to data/1_market/spot/")
