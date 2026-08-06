# fix_backtest.py
code = open('backtest_combined.py', 'r').read()
code = code.replace(
    "scalper = LondonScalper(df_5m, sys.modules[__name__])",
    "df_15m = pd.read_csv('data/raw/XAUUSD_15Min.csv', parse_dates=['time'])\ndf_15m = df_15m.sort_values('time').reset_index(drop=True)\nscalper = LondonScalper(df_15m, df_5m, sys.modules[__name__])"
)
open('backtest_combined.py', 'w').write(code)
print("Backtest updated to pass 15-min data")