# fix_signals_time.py
old = '''upcoming.append({
                                    "strategy": "BREAKOUT", "type": t["type"],
                                    "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
                                    "time": t["time"].strftime("%H:%M")
                                })'''

new = '''upcoming.append({
                                    "strategy": "BREAKOUT", "type": t["type"],
                                    "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
                                    "time": t["time"].strftime("%H:%M"),
                                    "alert": f"At {t['time'].strftime('%H:%M')} GMT, watch for {t['type']} at {t['entry']:.2f}"
                                })'''

code = open('src/engine/live_trader.py', 'r').read()
code = code.replace(old, new)

old2 = '''upcoming.append({
                                    "strategy": "SCALPER", "type": t["type"],
                                    "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
                                    "time": t["time"].strftime("%H:%M")
                                })'''

new2 = '''upcoming.append({
                                    "strategy": "SCALPER", "type": t["type"],
                                    "entry": t["entry"], "sl": t["sl"], "tp": t["tp"],
                                    "time": t["time"].strftime("%H:%M"),
                                    "alert": f"At {t['time'].strftime('%H:%M')} GMT, watch for {t['type']} at {t['entry']:.2f}"
                                })'''

code = code.replace(old2, new2)
open('src/engine/live_trader.py', 'w').write(code)
print("Signals now include alert messages!")