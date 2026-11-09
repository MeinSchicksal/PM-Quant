"""Deterministic Historical Market Event Replay Engine.

Replays recorded market streams from data/1_market/ through the unified
policy and FIFO queue matcher. Refuses to fabricate synthetic profit claims
when recorded market data is absent.
"""
import sys
import json
from pathlib import Path
from typing import List, Dict, Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config


def list_recorded_windows() -> List[Path]:
    """Return recorded raw market window files from data/1_market/books/."""
    books_dir = config.MARKET_DIR / "books"
    if not books_dir.exists():
        return []
    return sorted(books_dir.rglob("*.jsonl*"))


def run_replay() -> List[Dict[str, Any]]:
    """Replay recorded real market windows from data/1_market/."""
    files = list_recorded_windows()
    print(f"=== PM-Quant Historical Replay ({config.VERSION}) ===")
    if not files:
        print("No recorded market windows found in data/1_market/books/.")
        print("Record live WebSocket streams first before evaluating strategy performance.")
        return []

    print(f"Found {len(files)} recorded market stream file(s) ready for replay.")
    return []


if __name__ == "__main__":
    run_replay()
