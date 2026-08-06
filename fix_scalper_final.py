# fix_scalper_final.py
code = open('src/strategies/london_scalper.py', 'r').read()

# Widen SL from 1.5 to 2.0 ATR
code = code.replace('1.5 * atr', '2.0 * atr')

# Add pullback completion: price must be above previous candle low (for longs)
# Find the long entry section and add filter
old_long = '''if bull_15 and body > 0 and rsi < 65:
                entry = close
                sl = row["swing_low"] - 2.0 * atr
                tp = entry + 2.5 * atr'''

new_long = '''prev_low = self.df["low"].iloc[i-1] if i > 0 else 0
            if bull_15 and body > 0 and rsi < 65 and close > prev_low:
                entry = close
                sl = row["swing_low"] - 2.0 * atr
                tp = entry + 2.5 * atr'''

code = code.replace(old_long, new_long)

# Same for shorts
old_short = '''elif bear_15 and body < 0 and rsi > 35:
                entry = close
                sl = row["swing_high"] + 2.0 * atr
                tp = entry - 2.5 * atr'''

new_short = '''prev_high = self.df["high"].iloc[i-1] if i > 0 else 99999
            elif bear_15 and body < 0 and rsi > 35 and close < prev_high:
                entry = close
                sl = row["swing_high"] + 2.0 * atr
                tp = entry - 2.5 * atr'''

code = code.replace(old_short, new_short)

open('src/strategies/london_scalper.py', 'w').write(code)
print("SL widened to 2.0 ATR + bounce confirmation added")