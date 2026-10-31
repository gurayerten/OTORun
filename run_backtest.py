"""Kullanım:
    python run_backtest.py BTCUSDT ETHUSDT SOLUSDT
    python run_backtest.py            # dinamik sembol listesinden ilk 15'i test eder
    python run_backtest.py --detail   # her işlemin detayını gösterir
"""
import sys

from config import SETTINGS
from exchange import get_klines_paginated, get_trading_symbols
from backtester import run_backtest


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_detail = "--detail" in sys.argv

    symbols = args or get_trading_symbols()[:15]
    print(f"📊 {len(symbols)} sembol, {SETTINGS.BACKTEST_DAYS} gün, {SETTINGS.INTERVAL} backtest ediliyor...\n")

    all_rows = []
    all_trades = []

    for sym in symbols:
        df = get_klines_paginated(sym, SETTINGS.INTERVAL, SETTINGS.BACKTEST_DAYS)
        if df is None or len(df) < 200:
            print(f"❌ {sym}: yetersiz veri")
            continue
        trades, m = run_backtest(df, sym)
        if m.get("trades", 0) == 0:
            print(f"➖ {sym}: işlem oluşmadı")
            continue

        all_rows.append(m)
        all_trades.extend(trades)

        # Sembol özeti
        print(f"✅ {sym}: {m['trades']} işlem | Win rate: {m['win_rate']}% | "
              f"Net: {m['net_pnl_pct']:+.2f}% | Max DD: {m['max_drawdown_pct']:.2f}% | PF: {m['profit_factor']}")
        print(f"   📈 LONG: {m['long_count']} ({m['long_win_rate']}% win) | "
              f"📉 SHORT: {m['short_count']} ({m['short_win_rate']}% win)")

        # Detaylı işlem listesi
        if show_detail and trades:
            for t in trades:
                side = "\033[32mLONG \033[0m" if t["direction"] == 1 else "\033[31mSHORT\033[0m"
                result = "\033[32mWIN \033[0m" if t["win"] else "\033[31mLOSS\033[0m"
                print(f"      {side} giriş={t['entry']:.6f} → çıkış={t['exit']:.6f}  "
                      f"Bakiye etkisi={t.get('account_return_pct', t['net_pnl_pct']):+.2f}%  {result}")
            print()

    if all_rows:
        avg_wr = sum(r["win_rate"] for r in all_rows) / len(all_rows)
        total_trades = sum(r["trades"] for r in all_rows)
        total_long = sum(r["long_count"] for r in all_rows)
        total_short = sum(r["short_count"] for r in all_rows)
        avg_symbol_net = sum(r["net_pnl_pct"] for r in all_rows) / len(all_rows)

        print(f"\n{'═' * 60}")
        print(f"📈 GENEL ÖZET")
        print(f"{'═' * 60}")
        print(f"  Sembol sayısı:    {len(all_rows)}")
        print(f"  Toplam işlem:     {total_trades}")
        print(f"  Ortalama win rate: {avg_wr:.1f}%")
        print(f"  Ortalama sembol net PnL (bileşiksiz): {avg_symbol_net:+.2f}%")
        print(f"  LONG / SHORT:     {total_long} / {total_short} "
              f"({total_long / total_trades * 100:.0f}% / {total_short / total_trades * 100:.0f}%)" if total_trades > 0 else "")

        # En iyi / en kötü işlem
        if all_trades:
            best = max(all_trades, key=lambda t: t.get("account_return_pct", t["net_pnl_pct"]))
            worst = min(all_trades, key=lambda t: t.get("account_return_pct", t["net_pnl_pct"]))
            best_side = "LONG" if best["direction"] == 1 else "SHORT"
            worst_side = "LONG" if worst["direction"] == 1 else "SHORT"
            print(f"  En iyi işlem:     {best['symbol']} {best_side} {best.get('account_return_pct', best['net_pnl_pct']):+.2f}%")
            print(f"  En kötü işlem:    {worst['symbol']} {worst_side} {worst.get('account_return_pct', worst['net_pnl_pct']):+.2f}%")
        print(f"{'═' * 60}")


if __name__ == "__main__":
    main()
