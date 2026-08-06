# download_more.py
import MetaTrader5 as mt5
import pandas as pd
import os

print("=" * 60)
print("DOWNLOADING EXTENDED XAUUSD DATA")
print("=" * 60)

if not mt5.initialize():
    print("MT5 init failed")
    exit()

os.makedirs("data/raw", exist_ok=True)

# Request MAX bars for each timeframe
configs = [
    ("XAUUSD", mt5.TIMEFRAME_M5, "M5", 50000),
    ("XAUUSD", mt5.TIMEFRAME_M15, "15Min", 30000),
    ("XAUUSD", mt5.TIMEFRAME_H1, "Hourly", 10000),
    ("XAUUSD", mt5.TIMEFRAME_D1, "Daily", 5000),
]

for symbol, tf, name, bars_count in configs:
    print(f"\nDownloading {symbol} {name}...")
    
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars_count)
    
    if rates is not None and len(rates) > 0:
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        
        filename = f"data/raw/XAUUSD_{name}.csv"
        df.to_csv(filename, index=False)
        
        print(f"  Got {len(df)} bars")
        print(f"  Range: {df['time'].min()} to {df['time'].max()}")
        print(f"  Saved: {filename}")
    else:
        print(f"  Failed - MT5 returned no data")

mt5.shutdown()
print("\nDone!")