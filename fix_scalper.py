# fix_scalper.py
code = open('src/strategies/london_scalper.py', 'r').read()
code = code.replace("SCALPER_ADX_MIN = 25", "SCALPER_ADX_MIN = 30")
code = code.replace("row[\"adx\"] > self.config.SCALPER_ADX_MIN", "row[\"adx\"] > 30")
open('src/strategies/london_scalper.py', 'w').write(code)
print("Scalper ADX raised to 30")