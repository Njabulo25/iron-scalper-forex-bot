# src/data/downloader.py 
Historical data downloader for Forex and Indices 
import MetaTrader5 as mt5 
import pandas as pd 
from pathlib import Path 
 
class DataDownloader: 
    def __init__(self, data_dir="data/raw"): 
        self.data_dir = Path(data_dir) 
        self.data_dir.mkdir(parents=True, exist_ok=True) 
    def download_symbol(self, symbol, timeframe, bars=500): 
        if not mt5.initialize(): 
            print("? MT5 initialization failed") 
            return None 
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars) 
        if rates is None or len(rates) == 0: 
            print(f"? No data for {symbol}") 
            mt5.shutdown() 
            return None 
        df = pd.DataFrame(rates) 
        df['time'] = pd.to_datetime(df['time'], unit='s') 
        df['symbol'] = symbol 
        mt5.shutdown() 
        return df 
