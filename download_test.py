# download_test.py
import MetaTrader5 as mt5
import pandas as pd
import os

print("=" * 60)
print("📊 IRON SCRAPER - DATA DOWNLOADER TEST")
print("=" * 60)

print("\n🔌 Connecting to MetaTrader 5...")
if not mt5.initialize():
    print("❌ MT5 initialization failed!")
    print("Please make sure MetaTrader 5 is installed and running.")
    mt5.shutdown()
    exit()
else:
    print("✅ MT5 connected successfully!")

symbol = "XAUUSD"
print(f"\n📈 Downloading {symbol}...")

bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 100)

if bars is not None and len(bars) > 0:
    df = pd.DataFrame(bars)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    print(f"✅ Downloaded {len(df)} bars")
    print(f"   Date range: {df['time'].min()} to {df['time'].max()}")
    print(f"   Columns: {df.columns.tolist()}")
    
    # Save to CSV
    os.makedirs("data/raw", exist_ok=True)
    df.to_csv("data/raw/XAUUSD_Test.csv", index=False)
    print(f"✅ Saved to: data/raw/XAUUSD_Test.csv")
    
    print(f"\n📊 First 5 rows:")
    print(df.head())
else:
    print(f"❌ No data for {symbol}")

mt5.shutdown()
print("\n" + "=" * 60)
print("✅ Data download test complete!")