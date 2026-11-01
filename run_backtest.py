"""Kullanım:
    python run_backtest.py BTCUSDT ETHUSDT SOLUSDT
    python run_backtest.py            # dinamik sembol listesinden ilk 15'i test eder
    python run_backtest.py --detail   # her işlemin detayını gösterir
    python run_backtest.py --engine impulse  # yalnızca ImpulseEngine
    python run_backtest.py --engine both     # aynı veride iki motoru karşılaştırır
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import SETTINGS
from exchange import get_klines_paginated, get_trading_symbols
from backtester import run_backtest


def _json_safe(value):
    """JSON standardında olmayan NaN/Infinity değerlerini null yap."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def main():
    raw_args = sys.argv[1:]
    args = [a for a in raw_args if not a.startswith("--") and a not in ("sweep", "impulse", "both")]
    show_detail = "--detail" in sys.argv
    engine = "sweep"
    if "--engine" in raw_args:
        engine_idx = raw_args.index("--engine")
        if engine_idx + 1 >= len(raw_args):
            raise SystemExit("--engine için sweep, impulse veya both belirtin")
        engine = raw_args[engine_idx + 1].lower()
    elif any(a.startswith("--engine=") for a in raw_args):
        engine = next(a.split("=", 1)[1].lower() for a in raw_args if a.startswith("--engine="))
    if engine not in ("sweep", "impulse", "both"):
        raise SystemExit("--engine sweep, impulse veya both olmalı")
    engines = ("sweep", "impulse") if engine == "both" else (engine,)

    symbols = args or get_trading_symbols()[:15]
    print(f"📊 {len(symbols)} sembol, {SETTINGS.BACKTEST_DAYS} gün, {SETTINGS.INTERVAL} | "
          f"Motor: {', '.join(engines)}\n")

    all_rows = []
    all_trades = []

    for sym in symbols:
        df = get_klines_paginated(sym, SETTINGS.INTERVAL, SETTINGS.BACKTEST_DAYS)
        if df is None or len(df) < 200:
            print(f"❌ {sym}: yetersiz veri")
            continue
        for engine_name in engines:
            trades, m = run_backtest(df, sym, engine=engine_name)
            if m.get("trades", 0) == 0:
                print(f"➖ {sym} [{engine_name}]: işlem oluşmadı")
                continue

            all_rows.append({**m, "symbol": sym, "engine": engine_name})
            all_trades.extend(trades)

            print(f"✅ {sym} [{engine_name}]: {m['trades']} işlem | Win rate: {m['win_rate']}% | "
                  f"Net: {m['net_pnl_pct']:+.2f}% | Max DD: {m['max_drawdown_pct']:.2f}% | PF: {m['profit_factor']}")
            print(f"   📈 LONG: {m['long_count']} ({m['long_win_rate']}% win) | "
                  f"📉 SHORT: {m['short_count']} ({m['short_win_rate']}% win)")

            if show_detail and trades:
                for t in trades:
                    side = "\033[32mLONG \033[0m" if t["direction"] == 1 else "\033[31mSHORT\033[0m"
                    result = "\033[32mWIN \033[0m" if t["win"] else "\033[31mLOSS\033[0m"
                    print(f"      {side} giriş={t['entry']:.6f} → çıkış={t['exit']:.6f}  "
                          f"Bakiye etkisi={t.get('account_return_pct', t['net_pnl_pct']):+.2f}%  {result}")
                print()

    if all_rows:
        for engine_name in engines:
            rows = [r for r in all_rows if r["engine"] == engine_name]
            trades = [t for t in all_trades if t["engine"] == engine_name]
            if not rows:
                continue
            avg_wr = sum(r["win_rate"] for r in rows) / len(rows)
            total_trades = sum(r["trades"] for r in rows)
            total_long = sum(r["long_count"] for r in rows)
            total_short = sum(r["short_count"] for r in rows)
            avg_symbol_net = sum(r["net_pnl_pct"] for r in rows) / len(rows)

            print(f"\n{'═' * 60}")
            print(f"📈 GENEL ÖZET [{engine_name}]")
            print(f"{'═' * 60}")
            print(f"  Sembol sayısı:    {len(rows)}")
            print(f"  Toplam işlem:     {total_trades}")
            print(f"  Ortalama win rate: {avg_wr:.1f}%")
            print(f"  Ortalama sembol net PnL (bileşiksiz): {avg_symbol_net:+.2f}%")
            print(f"  LONG / SHORT:     {total_long} / {total_short} "
                  f"({total_long / total_trades * 100:.0f}% / {total_short / total_trades * 100:.0f}%)" if total_trades > 0 else "")

            if trades:
                best = max(trades, key=lambda t: t.get("account_return_pct", t["net_pnl_pct"]))
                worst = min(trades, key=lambda t: t.get("account_return_pct", t["net_pnl_pct"]))
                best_side = "LONG" if best["direction"] == 1 else "SHORT"
                worst_side = "LONG" if worst["direction"] == 1 else "SHORT"
                print(f"  En iyi işlem:     {best['symbol']} {best_side} {best.get('account_return_pct', best['net_pnl_pct']):+.2f}%")
                print(f"  En kötü işlem:    {worst['symbol']} {worst_side} {worst.get('account_return_pct', worst['net_pnl_pct']):+.2f}%")
            print(f"{'═' * 60}")

    # Her çalıştırmayı ayrı sakla; önceki raporların üzerine yazma.
    generated_at = datetime.now(timezone.utc)
    report = {
        "schema_version": 1,
        "report_type": "historical_backtest_simulation",
        "generated_at_utc": generated_at.isoformat(),
        "symbols_requested": symbols,
        "interval": SETTINGS.INTERVAL,
        "days_requested": SETTINGS.BACKTEST_DAYS,
        "engines": list(engines),
        "configuration": {
            "live_impulse_enabled": SETTINGS.USE_IMPULSE_ENGINE,
            "min_impulse_score": SETTINGS.MIN_IMPULSE_SCORE,
            "disabled_sweep_setup_types": list(SETTINGS.DISABLED_SETUP_TYPES),
            "disabled_impulse_setup_types": list(getattr(SETTINGS, "DISABLED_IMPULSE_SETUP_TYPES", ())),
            "slippage": SETTINGS.SLIPPAGE,
            "taker_fee": SETTINGS.TAKER_FEE,
        },
        "execution_model": (
            "Sinyal mum kapanışında doğrulanır; simüle giriş sonraki mum açılışında, "
            "slippage ve komisyon ayarları uygulanarak yapılır. Gerçek emir değildir."
        ),
        "summary_by_symbol": all_rows,
        "trades": all_trades,
    }
    output_dir = Path("state") / "run_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.strftime("%Y%m%d_%H%M%S_%f")
    report_path = output_dir / f"run_backtest_{timestamp}.json"
    with report_path.open("w", encoding="utf-8") as report_file:
        json.dump(_json_safe(report), report_file, ensure_ascii=False, indent=2, allow_nan=False)
        report_file.write("\n")
    print(f"\nBacktest raporu kaydedildi: {report_path}")


if __name__ == "__main__":
    main()
