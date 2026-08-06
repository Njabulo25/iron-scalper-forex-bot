"""
Iron Scraper - Main Entry Point
Forex and Indices Trading Bot
"""
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent / "src"))

def main():
    print("=" * 60)
    print("🦾 IRON SCRAPER - FOREX TRADING BOT")
    print("=" * 60)
    print("\n✅ Environment is ready!")
    print("📊 Your Forex Bot is set up successfully!")
    
    # Test imports
    try:
        import numpy as np
        import pandas as pd
        import MetaTrader5 as mt5
        print(f"\n✅ NumPy: {np.__version__}")
        print(f"✅ Pandas: {pd.__version__}")
        print("✅ MetaTrader5: Ready")
        print("\n🎉 All systems ready!")
    except ImportError as e:
        print(f"\n❌ Import error: {e}")
        print("Please install missing packages: pip install -r requirements.txt")

if __name__ == "__main__":
    main()