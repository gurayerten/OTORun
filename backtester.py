import numpy as np

from config import SETTINGS
from strategy import SweepEngine, atr
from risk import build_trade_plan


def run_backtest(df, symbol, engine="sweep"):
    """Tek bir sinyal motorunu ileri barlarda canlı pozisyon kurallarıyla simüle eder.

    Sinyal barının kapanışında karar verilir; varsayılan dolum sonraki barın
    açılışında yapılır. Böylece sinyal barının kapanışını önceden bilme varsayımı
    kaldırılır.
    """
    engine_name = str(engine).strip().lower()
    if engine_name == "sweep":
        signal_engine = SweepEngine()
    elif engine_name == "impulse":
        from impulse_engine import ImpulseEngine
        signal_engine = ImpulseEngine()
    else:
        raise ValueError("engine 'sweep' veya 'impulse' olmalı")

    atr_v = atr(df, SETTINGS.ATR_LEN).values
    close, high, low = df["close"].values, df["high"].values, df["low"].values
    open_ = df["open"].values
    setups_by_bar = {}
    disabled_types = (SETTINGS.DISABLED_SETUP_TYPES if engine_name == "sweep"
                      else getattr(SETTINGS, "DISABLED_IMPULSE_SETUP_TYPES", ()))
    for setup in signal_engine.iter_signals(df):
        if setup.setup_type in disabled_types:
            continue
        setups_by_bar.setdefault(setup.bar_index, []).append(setup)
    trades = []
    position = None
    pending_setup = None

    for i in range(len(df)):
        if position is not None:
            position = _check_bar(position, high[i], low[i], close[i],
                                  i - position["opened_i"], trades, symbol)

        # Sinyal bir önceki barın kapanışında oluştu; dolum bu barın açılışında.
        if position is None and pending_setup is not None:
            setup = pending_setup
            entry = open_[i]
            slip = entry * SETTINGS.SLIPPAGE
            entry_exec = entry + slip if setup.direction == 1 else entry - slip
            plan = build_trade_plan(setup.direction, entry_exec, setup.anchor_extreme,
                                    atr_v[setup.bar_index], regime=setup.market_regime)
            position = {
                "plan": plan, "tp1": False, "tp2": False, "tp3": False,
                "be": False, "trailing_active": False, "extreme_price": entry_exec,
                "opened_i": i,
                "risk": abs(plan.entry - plan.sl),
                "signal_bar_index": setup.bar_index,
                "setup_type": getattr(setup, "setup_type", ""),
                "score": float(getattr(setup, "score", 0.0)),
                "grade": getattr(setup, "grade", ""),
                "market_regime": getattr(setup, "market_regime", ""),
                "signal_bar_time": (str(df["open_time"].iloc[setup.bar_index])
                                    if "open_time" in df.columns else None),
                "entry_bar_time": (str(df["open_time"].iloc[i])
                                   if "open_time" in df.columns else None),
                "initial_sl": plan.sl,
                "initial_tp1": plan.tp1,
                "initial_tp2": plan.tp2,
                "initial_tp3": plan.tp3,
            }
            pending_setup = None
            # Pozisyon açılış barının high/low aralığı da simüle edilir.
            position = _check_bar(position, high[i], low[i], close[i], 0, trades, symbol)

        if position is None and pending_setup is None and i in setups_by_bar and i + 1 < len(df):
            if not np.isnan(atr_v[i]) and atr_v[i] > 0:
                pending_setup = setups_by_bar[i][-1]

    if position:
        trade = _finalize_trade(position, close[-1], symbol, "EOD")
        trade["bars_held"] = int(len(df) - 1 - position["opened_i"])
        trades.append(trade)

    for trade in trades:
        trade["engine"] = engine_name
    return trades, _metrics(trades)


def _check_bar(position, h, l, c, bars_held, trades, symbol):
    plan = position["plan"]
    d = plan.direction
    risk = position.get("risk", 0) or 0.0
    sl_hit = (l <= plan.sl) if d == 1 else (h >= plan.sl)
    tp1_hit = (h >= plan.tp1) if d == 1 else (l <= plan.tp1)
    tp2_hit = (h >= plan.tp2) if d == 1 else (l <= plan.tp2)
    tp3_hit = (h >= plan.tp3) if d == 1 else (l <= plan.tp3)

    if tp1_hit and not position["tp1"] and not sl_hit:
        position["tp1"] = True
        if SETTINGS.USE_BREAK_EVEN:
            lock = (plan.entry + SETTINGS.BE_LEVEL_R * risk if d == 1
                    else plan.entry - SETTINGS.BE_LEVEL_R * risk)
            if (d == 1 and lock > plan.sl) or (d == -1 and lock < plan.sl):
                plan.sl = lock
                position["be"] = True
    if tp2_hit and not position["tp2"] and not sl_hit:
        position["tp2"] = True
        new_sl = plan.tp1
        if (d == 1 and new_sl > plan.sl) or (d == -1 and new_sl < plan.sl):
            plan.sl = new_sl
    if tp3_hit and not position["tp3"] and not sl_hit:
        position["tp3"] = True

    # Live StateManager ile aynı trailing-stop güncellemesi.
    if getattr(SETTINGS, "USE_TRAILING_STOP", False) and not sl_hit and risk > 0:
        activation = risk * getattr(SETTINGS, "TRAILING_STOP_ACTIVATION_R", 1.0)
        trail_distance = risk * getattr(SETTINGS, "TRAILING_STOP_DISTANCE_R", 1.0)
        if d == 1:
            position["extreme_price"] = max(position.get("extreme_price", plan.entry), h)
            if position["extreme_price"] >= plan.entry + activation:
                position["trailing_active"] = True
                plan.sl = max(plan.sl, position["extreme_price"] - trail_distance)
        else:
            position["extreme_price"] = min(position.get("extreme_price", plan.entry), l)
            if position["extreme_price"] <= plan.entry - activation:
                position["trailing_active"] = True
                plan.sl = min(plan.sl, position["extreme_price"] + trail_distance)

    # OHLC bars do not reveal the exact intrabar path. Keep an existing stop
    # first (above), then model favorable TP/trailing activation before a
    # retrace to the newly ratcheted stop. This matches StateManager live logic.
    sl_hit = (l <= plan.sl) if d == 1 else (h >= plan.sl)

    exit_price = None
    reason = None
    if sl_hit:
        exit_price = plan.sl
        reason = "BE" if position["be"] else "SL"
    elif tp3_hit:
        exit_price = plan.tp3
        reason = "TP3"
    elif bars_held >= SETTINGS.MAX_HOLD_BARS:
        r_now = (((c - plan.entry) * d) / risk) if risk > 0 else 0.0
        if r_now < SETTINGS.TIME_STOP_MIN_R:
            exit_price = c
            reason = "TIME"

    if exit_price is not None:
        trade = _finalize_trade(position, exit_price, symbol, reason)
        trade["bars_held"] = int(bars_held)
        trades.append(trade)
        return None
    return position


def _finalize_trade(position, exit_price, symbol, reason="SL"):
    plan = position["plan"]
    d = plan.direction
    risk = position.get("risk", 0) or 0.0
    gross = (exit_price - plan.entry) * d
    fee = (plan.entry + exit_price) * SETTINGS.TAKER_FEE
    net = gross - fee
    r_multiple = (((exit_price - plan.entry) * d) / risk) if risk > 0 else 0.0
    if abs(r_multiple) < SETTINGS.BE_OUTCOME_THRESHOLD and reason != "TP3":
        outcome = "BREAKEVEN"
    elif r_multiple > 0:
        outcome = "WIN"
    else:
        outcome = "LOSS"
    return {
        "symbol": symbol, "direction": d, "entry": plan.entry, "exit": exit_price,
        "signal_bar_index": position.get("signal_bar_index"),
        "signal_bar_time": position.get("signal_bar_time"),
        "entry_bar_time": position.get("entry_bar_time"),
        "setup_type": position.get("setup_type", ""),
        "score": position.get("score"),
        "grade": position.get("grade", ""),
        "market_regime": position.get("market_regime", ""),
        "direction_label": "LONG" if d == 1 else "SHORT",
        "initial_sl": position.get("initial_sl"),
        "tp1": position.get("initial_tp1"),
        "tp2": position.get("initial_tp2"),
        "tp3": position.get("initial_tp3"),
        "net_pnl_pct": net / plan.entry * 100,
        "risk_distance": risk,
        "r_multiple": round(r_multiple, 4),
        "r_realized": round(r_multiple, 4),
        "reason": reason,
        "outcome": outcome,
        "win": outcome == "WIN",
    }


def _metrics(trades):
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t.get("outcome") == "WIN" or ("outcome" not in t and t["win"])]
    losses = [t for t in trades if t.get("outcome") == "LOSS" or ("outcome" not in t and not t["win"])]
    breakevens = [t for t in trades if t.get("outcome") == "BREAKEVEN"]

    longs = [t for t in trades if t["direction"] == 1]
    shorts = [t for t in trades if t["direction"] == -1]
    long_wins = [t for t in longs if t["win"]]
    short_wins = [t for t in shorts if t["win"]]

    initial_balance = float(getattr(SETTINGS, "BACKTEST_INITIAL_BALANCE", 1000.0))
    risk_per_trade_pct = max(0.0, float(getattr(SETTINGS, "BACKTEST_RISK_PER_TRADE_PCT", 1.0)))
    equity = [initial_balance]
    for t in trades:
        entry = float(t.get("entry", 0.0))
        risk_distance = float(t.get("risk_distance", 0.0))
        stop_pct = (risk_distance / entry * 100.0) if entry > 0 else 0.0
        position_fraction = min(1.0, risk_per_trade_pct / stop_pct) if stop_pct > 0 else 0.0
        account_return_pct = t["net_pnl_pct"] * position_fraction
        t["account_return_pct"] = account_return_pct
        equity.append(equity[-1] * (1.0 + account_return_pct / 100.0))
    peak, max_dd = initial_balance, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100.0)

    gross_profit = sum(t.get("account_return_pct", t["net_pnl_pct"]) for t in wins)
    gross_loss = abs(sum(t.get("account_return_pct", t["net_pnl_pct"]) for t in losses))

    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 2),
        "breakevens": len(breakevens),
        "net_pnl_pct": round((equity[-1] / initial_balance - 1.0) * 100.0, 2) if initial_balance > 0 else 0.0,
        "final_balance": round(equity[-1], 2),
        "max_drawdown_pct": round(max_dd, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        # LONG/SHORT detayları
        "long_count": len(longs),
        "short_count": len(shorts),
        "long_win_rate": round(len(long_wins) / len(longs) * 100, 2) if longs else 0.0,
        "short_win_rate": round(len(short_wins) / len(shorts) * 100, 2) if shorts else 0.0,
    }
