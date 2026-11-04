"""Top-volume Binance Futures symbols' open-interest breadth monitor."""
import json
import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from config import SETTINGS
from exchange import get_open_interest, get_top_tickers_by_volume
from logger import get_logger
from telegram_notify import fmt_pct, send_telegram

log = get_logger("oi_market_breadth")
OI_BREADTH_STATE_FILE = os.path.join("state", "oi_market_breadth_state.json")
_OI_BREADTH_LOCK = threading.Lock()


def _load_oi_breadth_state() -> Dict[str, Any]:
    if not os.path.exists(OI_BREADTH_STATE_FILE):
        return {}
    try:
        with open(OI_BREADTH_STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
        log.warning("OI breadth state biçimi geçersiz; boş state kullanılıyor")
    except Exception as e:
        log.warning(f"OI breadth state yükleme hatası: {e}")
    return {}


def _save_oi_breadth_state(state: Dict[str, Any]) -> None:
    tmp = OI_BREADTH_STATE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(OI_BREADTH_STATE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        for attempt in range(5):
            try:
                os.replace(tmp, OI_BREADTH_STATE_FILE)
                return
            except (PermissionError, OSError) as e:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        shutil.copy2(tmp, OI_BREADTH_STATE_FILE)
                        os.remove(tmp)
                    except Exception as fallback_error:
                        log.warning(f"OI breadth state fallback yazma hatası: {fallback_error}")
                        raise e
    except Exception as e:
        log.warning(f"OI breadth state kaydetme hatası: {e}")


def _build_report(payload: List[Dict[str, Any]], state: Dict[str, Any], change_eps: float) -> Dict[str, Any]:
    previous = state.get("symbols", {})
    noise_pct = float(getattr(SETTINGS, "OI_BREADTH_NOISE_PCT", 0.03))
    interval_min = max(1, int(getattr(SETTINGS, "OI_BREADTH_INTERVAL_MIN", 30)))
    max_gap_mult = max(1.0, float(getattr(SETTINGS, "OI_BREADTH_MAX_SNAPSHOT_GAP_MULTIPLIER", 2.5)))
    max_gap_sec = interval_min * 60.0 * max_gap_mult
    rows = []
    snapshot_gaps_min = []
    for item in payload:
        symbol = item["symbol"]
        old = previous.get(symbol)
        if not isinstance(old, dict) or old.get("oi") is None or old.get("price") is None:
            continue
        old_ts, current_ts = int(old.get("ts", 0) or 0), int(item.get("ts", 0) or 0)
        if old_ts > 0 and current_ts > 0:
            gap_sec = current_ts - old_ts
            # Uzun veri kesintisinden sonra oluşan büyük değişimi tek normal
            # tarama aralığıymış gibi breadth hesabına katma.
            if gap_sec <= 0 or gap_sec > max_gap_sec:
                continue
            snapshot_gaps_min.append(gap_sec / 60.0)
        old_oi, old_price = float(old["oi"]), float(old["price"])
        oi_change = ((item["oi"] - old_oi) / old_oi * 100.0) if old_oi else 0.0
        price_change = ((item["price"] - old_price) / old_price * 100.0) if old_price else 0.0
        significant = abs(oi_change) >= noise_pct
        quadrant = None
        if significant and price_change > 0:
            quadrant = "long_opening" if oi_change > 0 else "short_closing"
        elif significant and price_change < 0:
            quadrant = "short_opening" if oi_change > 0 else "long_closing"
        rows.append({"symbol": symbol, "oi_change_pct": oi_change,
                     "price_change_pct": price_change, "quadrant": quadrant,
                     "significant": significant})

    total = len(rows)  # karşılaştırılabilir semboller; yatay OI de paydaya dahildir
    rising = sum(1 for row in rows if row["significant"] and row["oi_change_pct"] > 0)
    falling = sum(1 for row in rows if row["significant"] and row["oi_change_pct"] < 0)
    flat = total - rising - falling
    rising_pct = rising / total * 100.0 if total else 0.0
    previous_report = state.get("last_report", {})
    old_rising_pct = previous_report.get("rising_pct")
    if old_rising_pct is None or previous_report.get("basis") != "all_comparable_symbols":
        direction = "ilk ölçüm"
    elif rising_pct - float(old_rising_pct) > change_eps:
        direction = "genişlik artıyor"
    elif float(old_rising_pct) - rising_pct > change_eps:
        direction = "genişlik azalıyor"
    else:
        direction = "genişlik sabit"
    quadrants = {key: sum(1 for row in rows if row["quadrant"] == key) for key in
                 ("long_opening", "short_opening", "short_closing", "long_closing")}
    return {
        "watchlist_size": len(payload), "measured_symbols": total,
        "rising": rising, "falling": falling, "rising_pct": rising_pct,
        "falling_pct": falling / total * 100.0 if total else 0.0,
        "flat": flat, "changed_symbols": rising + falling,
        "snapshot_interval_min": (sum(snapshot_gaps_min) / len(snapshot_gaps_min)
                                  if snapshot_gaps_min else None),
        "direction": direction, "quadrants": quadrants,
        "top_increases": sorted((row for row in rows if row["significant"] and row["oi_change_pct"] > 0),
                                 key=lambda row: row["oi_change_pct"], reverse=True),
        "top_decreases": sorted((row for row in rows if row["significant"] and row["oi_change_pct"] < 0),
                                 key=lambda row: row["oi_change_pct"]),
    }


def run_oi_breadth_check(state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not getattr(SETTINGS, "OI_BREADTH_ENABLED", True):
        return None
    min_symbols = max(1, int(getattr(SETTINGS, "OI_BREADTH_MIN_SYMBOLS", 10)))
    watchlist_size = max(1, int(getattr(SETTINGS, "OI_BREADTH_WATCHLIST_SIZE", 30)))
    try:
        candidates = get_top_tickers_by_volume(watchlist_size)
        watchlist = []
        for ticker in candidates:
            symbol = ticker.get("symbol")
            try:
                price = float(ticker.get("lastPrice", 0))
                if symbol and price > 0:
                    watchlist.append({"symbol": symbol, "price": price})
            except (TypeError, ValueError) as e:
                log.debug(f"OI breadth fiyat parse hatası ({symbol}): {e}")
    except Exception as e:
        log.warning(f"OI breadth ticker/watchlist verisi alınamadı; state korunuyor: {e}")
        return None
    if len(watchlist) < min_symbols:
        log.warning(f"OI breadth watchlist yetersiz ({len(watchlist)}); state korunuyor")
        return None

    with _OI_BREADTH_LOCK:
        old_state = dict(state if state is not None else _load_oi_breadth_state())
        old_symbols = dict(old_state.get("symbols", {}))

    current, failed = [], []
    call_delay = max(0, int(getattr(SETTINGS, "OI_BREADTH_CALL_DELAY_MS", 0))) / 1000.0
    for index, ticker in enumerate(watchlist):
        symbol = ticker["symbol"]
        try:
            oi = float(get_open_interest(symbol))
            if oi <= 0:
                raise ValueError(f"geçersiz OI: {oi}")
            current.append({"symbol": symbol, "oi": oi, "price": ticker["price"], "ts": int(time.time())})
        except Exception as e:
            log.debug(f"OI breadth OI alınamadı ({symbol}): {e}")
            failed.append(symbol)
        if call_delay and index < len(watchlist) - 1:
            time.sleep(call_delay)

    if len(failed) > len(watchlist) / 2:
        log.warning(f"OI breadth OI çağrılarının yarısından fazlası başarısız ({len(failed)}/{len(watchlist)}); state korunuyor")
        return None
    if len(current) < min_symbols:
        log.warning(f"OI breadth geçerli sembol sayısı yetersiz ({len(current)}); state korunuyor")
        return None

    change_eps = float(getattr(SETTINGS, "OI_BREADTH_CHANGE_EPS", 2.0))
    report = _build_report(current, old_state, change_eps)
    new_symbols = {item["symbol"]: {"oi": item["oi"], "price": item["price"], "ts": item["ts"]} for item in current}
    # Geçici fetch hatası olan mevcut watchlist sembolünün eski kaydı tutulur; diğer eski watchlist kayıtları prune edilir.
    for symbol in failed:
        if symbol in old_symbols:
            new_symbols[symbol] = old_symbols[symbol]
    if len(new_symbols) < len(old_symbols):
        log.debug(f"OI breadth state'ten watchlist dışına çıkan {len(old_symbols) - len(new_symbols)} sembol prune edildi")
    first_measurement = not old_symbols
    new_state = {"symbols": new_symbols,
                 "last_report": {"rising_pct": report["rising_pct"],
                                 "basis": "all_comparable_symbols"},
                 "updatedAt": int(time.time())}
    with _OI_BREADTH_LOCK:
        _save_oi_breadth_state(new_state)
    if first_measurement:
        log.info(f"OI breadth ilk snapshot kaydedildi ({len(current)} sembol); değişim alarmı üretilmedi")
        return None
    if report["measured_symbols"] < min_symbols:
        log.warning(f"OI breadth karşılaştırılabilir sembol sayısı yetersiz ({report['measured_symbols']}); rapor gönderilmiyor")
        return None
    send_only_change = getattr(SETTINGS, "OI_BREADTH_SEND_ONLY_ON_CHANGE", True)
    previous_report = old_state.get("last_report", {})
    prior_pct = previous_report.get("rising_pct")
    has_oi_changes = bool(report["rising"] or report["falling"])
    basis_changed = previous_report.get("basis") != "all_comparable_symbols"
    breadth_changed = (prior_pct is None or basis_changed or
                       abs(report["rising_pct"] - float(prior_pct)) >= change_eps)
    if send_only_change and not (has_oi_changes and breadth_changed):
        log.info("OI breadth değişimi bildirim eşiğini aşmadı")
        return None
    log.info(f"OI breadth raporu hazır: {report['measured_symbols']} sembol, artan oranı %{report['rising_pct']:.1f}")
    return report


def fmt_oi_breadth(report: Dict[str, Any]) -> str:
    highlight_n = max(1, int(getattr(SETTINGS, "OI_BREADTH_TOP_N_HIGHLIGHT", 5)))
    q = report["quadrants"]
    lines = [f"⚖️ PİYASA OI GENİŞLİĞİ (Top {report['watchlist_size']} Hacim)", "",
             f"OI Artan: {report['rising']} (%{fmt_pct(report['rising_pct'], 1)}) | OI Azalan: {report['falling']} (%{fmt_pct(report['falling_pct'], 1)}) | Yatay: {report['flat']}",
             f"Karşılaştırılabilir: {report['measured_symbols']}/{report['watchlist_size']} sembol" +
             (f" | Snapshot aralığı ort.: {fmt_pct(report['snapshot_interval_min'], 1)} dk"
              if report.get("snapshot_interval_min") is not None else ""),
             f"Yön: {report['direction']}", "", "📊 Pozisyon Davranışı (proxy):",
             f"🟢 Long Açılıyor (OI↑ Fiyat↑): {q['long_opening']}",
             f"🔴 Short Açılıyor (OI↑ Fiyat↓): {q['short_opening']}",
             f"🟡 Short Kapanıyor (OI↓ Fiyat↑): {q['short_closing']}",
             f"⚪ Dağıtım (OI↓ Fiyat↓): {q['long_closing']}"]
    increases, decreases = report["top_increases"][:highlight_n], report["top_decreases"][:highlight_n]
    if increases:
        lines.extend(["", "🔥 En Çok OI Artışı:"])
        lines.extend(f"{i}. {row['symbol']} +%{fmt_pct(row['oi_change_pct'], 2)}" for i, row in enumerate(increases, 1))
    if decreases:
        lines.extend(["", "🧊 En Çok OI Azalışı:"])
        lines.extend(f"{i}. {row['symbol']} -%{fmt_pct(abs(row['oi_change_pct']), 2)}" for i, row in enumerate(decreases, 1))
    lines.extend(["", "Not: Rapor izlenen Top hacimli sembollerle sınırlıdır. OI-fiyat kadranı farklı snapshot aralıklarıyla ölçülen dolaylı bir pozisyon davranışı proxy'sidir; doğrudan alım/satım sinyali değildir."])
    return "\n".join(lines)


def send_oi_breadth(report: Dict[str, Any]) -> None:
    send_telegram(fmt_oi_breadth(report))
