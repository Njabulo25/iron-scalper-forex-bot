# full_downloader.py
import MetaTrader5 as mt5
import pandas as pd
import os
from datetime import datetime

print("=" * 60)
print("📊 IRON SCRAPER - FULL DATA DOWNLOADER")
print("=" * 60)

if not mt5.initialize():
    print("❌ MT5 initialization failed!")
    mt5.shutdown()
    exit()
else:
    print("✅ MT5 connected successfully!")
    print(f"   MT5 Version: {mt5.version()}")

# Symbols and timeframes
symbols = ["XAUUSD", "GBPUSD", "US30"]
timeframes = [
    (mt5.TIMEFRAME_D1, "Daily", 500),
    (mt5.TIMEFRAME_H1, "Hourly", 1000),
    (mt5.TIMEFRAME_M15, "15Min", 2000),   # Increased for better backtesting
    (mt5.TIMEFRAME_M5, "M5", 2000),        # NEW: 5-minute for scalping
]

# Create data directory
os.makedirs("data/raw", exist_ok=True)

total_files = 0

for symbol in symbols:
    print(f"\n{'='*50}")
    print(f"📈 Processing {symbol}")
    print(f"{'='*50}")
    
    for timeframe, tf_name, bars_count in timeframes:
        print(f"   📊 Downloading {tf_name}...")
        
        bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars_count)
        
        if bars is not None and len(bars) > 0:
            df = pd.DataFrame(bars)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            
            filename = f"data/raw/{symbol}_{tf_name}.csv"
            df.to_csv(filename, index=False)
            
            print(f"   ✅ {len(df)} bars saved")
            print(f"      📅 {df['time'].min()} to {df['time'].max()}")
            print(f"      💾 {filename}")
            total_files += 1
        else:
            print(f"   ❌ No data available")

mt5.shutdown()

print("\n" + "=" * 60)
print(f"✅ Download complete! {total_files} files saved.")
print("📁 Location: data/raw/")
print("=" * 60)

# List downloaded files
print("\n📁 Downloaded files:")
for file in os.listdir("data/raw"):
    if file.endswith(".csv"):
        size = os.path.getsize(f"data/raw/{file}")
        print(f"   📄 {file} ({size:,} bytes)")