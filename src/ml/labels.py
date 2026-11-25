"""Label generation: did this signal reach its target before its stop?

A label is built by replaying the bars after the signal, in order, against the
actual stop and target the strategy would have sent to MT5. That makes the
label identical to the outcome a trade taken at that bar would have had, which
is what the confidence gate is ultimately deciding.

Two conventions are deliberate and conservative:

* If one bar spans both the stop and the target, the stop is assumed to have
  been hit first. The bar's intrabar sequence is unknowable from OHLC, so the
  pessimistic assumption is used instead of the flattering one.
* A signal that reaches neither barrier inside ``horizon_bars`` is *excluded*
  from training (it is not quietly counted as a loss). Its marked-to-market
  R is still returned so evaluation can include it.
"""

import numpy as np

WIN = 1
LOSS = 0
TIMEOUT = -1


def simulate_trade(bars, index, side, entry, stop_loss, take_profit, horizon):
    """Replay one signal forward and return ``(label, r_multiple, exit_index)``.

    ``label`` is 1 (target first), 0 (stop first) or -1 (timeout).
    """
    risk = abs(entry - stop_loss)
    if not np.isfinite(risk) or risk <= 0:
        return TIMEOUT, 0.0, index

    final = min(index + horizon, len(bars) - 1)
    for step in range(index + 1, final + 1):
        high = bars.high[step]
        low = bars.low[step]

        if side > 0:
            hit_stop = low <= stop_loss
            hit_target = high >= take_profit
        else:
            hit_stop = high >= stop_loss
            hit_target = low <= take_profit

        if hit_stop:
            # Ambiguous bars are resolved against the trade.
            return LOSS, -1.0, step
        if hit_target:
            reward = abs(take_profit - entry) / risk
            return WIN, reward, step

    exit_price = bars.close[final]
    move = (exit_price - entry) * (1 if side > 0 else -1)
    return TIMEOUT, move / risk, final


def build_labels(bars, side, stop_loss, take_profit, horizon):
    """Vector of labels and R multiples aligned with the bars."""
    count = len(bars)
    labels = np.full(count, TIMEOUT, dtype=np.int8)
    outcomes = np.zeros(count, dtype=np.float64)
    exits = np.arange(count, dtype=np.int64)

    for index in np.where(side != 0)[0]:
        label, outcome, exit_index = simulate_trade(
            bars, int(index), int(side[index]), float(bars.close[index]),
            float(stop_loss[index]), float(take_profit[index]), horizon,
        )
        labels[index] = label
        outcomes[index] = outcome
        exits[index] = exit_index
    return labels, outcomes, exits
