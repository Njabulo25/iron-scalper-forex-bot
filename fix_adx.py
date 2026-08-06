# fix_adx.py
path = 'src/strategies/gold_scalper.py'
with open(path, 'r') as f:
    code = f.read()

code = code.replace('> 20)', '> 25)')

with open(path, 'w') as f:
    f.write(code)

print("ADX threshold updated to > 25")