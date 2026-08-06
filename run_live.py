# run_live.py
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.engine.live_trader import LiveTrader

print("=" * 60)
print("IRON SCALPER - LIVE TRADING")
print("=" * 60)
print("\nMake sure MT5 is running with your demo account logged in.")
print("Press Ctrl+C to stop.\n")

trader = LiveTrader()
trader.run()