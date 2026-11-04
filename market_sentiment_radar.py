"""Market-wide sentiment proxies from Binance Futures long/short data endpoints."""
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import SETTINGS
from exchange import (
    get_global_long_short_ratio,
    get_top_long_short_account_ratio,
    get_top_long_short_position_ratio,
    get_taker_long_short_ratio,
    get_top_symbols_by_volume,
)
from logger import get_logger
from telegram_notify import fmt_pct, send_telegram

log = get_logger("market_sentiment_radar")
SENTIMENT_STATE_FILE = os.path.join("state", "market_sentiment_state.json")
_SENTIMENT_LOCK = threading.Lock()

_ENDPOINTS = {
    "global_account": ("SENTIMENT_RADAR_USE_GLOBAL_RATIO", get_global_long_short_ratio, "longShortRatio"),
    "top_account": ("SENTIMENT_RADAR_USE_TOP_ACCOUNT_RATIO", get_top_long_short_account_ratio, "longShortRatio"),
    "top_position": ("SENTIMENT_RADAR_USE_TOP_POSITION_RATIO", get_top_long_short_position_ratio, "longShortRatio"),
    "taker": ("SENTIMENT_RADAR_USE_TAKER_RATIO", get_taker_long_short_ratio, "buySellRatio"),
}


def _load_sentiment_state() -> Dict[str, Any]:
    if not os.path.exists(SENTIMENT_STATE_FILE):
        return {}
    try:
        with open(SENTIMENT_STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
        log.warning("Sentiment state biçimi geçersiz; boş state kullanılıyor")
    except Exception as e:
        log.warning(f"Sentiment state yükleme hatası: {e}")
    return {}


def _save_sentiment_state(state: Dict[str, Any]) -> None:
    tmp = SENTIMENT_STATE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(SENTIMENT_STATE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        for attempt in range(5):
            try:
                os.replace(tmp, SENTIMENT_STATE_FILE)
                return
            except (PermissionError, OSError) as e:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        shutil.copy2(tmp, SENTIMENT_STATE_FILE)
                        os.remove(tmp)
                    except Exception as fallback_error:
                        log.warning(f"Sentiment state fallback yazma hatası: {fallback_error}")
                        raise e
    except Exception as e:
        log.warning(f"Sentiment state kaydetme hatası: {e}")


def _period_milliseconds(period: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    if not period or period[-1] not in units:
        raise ValueError(f"Geçersiz sentiment period: {period}")
    return int(period[:-1]) * units[period[-1]]


def _build_report(payload: List[Dict[str, Any]], state: Dict[str, Any], change_eps: float) -> Dict[str, Any]:
    metric_names = list(payload[0]["metrics"]) if payload else []
    previous_symbols = state.get("symbols", {})
    previous_averages = state.get("last_averages", {})
    averages, trends, metric_counts = {}, {}, {}
    long_share_averages = {}
    symbol_changes = {}
    for item in payload:
        previous_metrics = previous_symbols.get(item["symbol"], {}).get("metrics", {})
        deltas = {name: item["metrics"].get(name) - previous_metrics[name]
                  for name in metric_names
                  if item["metrics"].get(name) is not None and previous_metrics.get(name) is not None}
        if deltas:
            symbol_changes[item["symbol"]] = deltas
    for name in metric_names:
        values = [item["metrics"].get(name) for item in payload if item["metrics"].get(name) is not None]
        averages[name] = sum(values) / len(values) if values else None
        metric_counts[name] = len(values)
        if name in ("global_account", "top_account", "top_position"):
            shares = [100.0 * value / (1.0 + value) for value in values if value >= 0]
            long_share_averages[name] = sum(shares) / len(shares) if shares else None
        old_value = previous_averages.get(name)
        if averages[name] is None or old_value is None:
            trends[name] = "ilk ölçüm" if averages[name] is not None else "veri yok"
        elif averages[name] - float(old_value) > change_eps:
            trends[name] = "artıyor"
        elif float(old_value) - averages[name] > change_eps:
            trends[name] = "azalıyor"
        else:
            trends[name] = "sabit"

    divergences = []
    threshold_pp = float(getattr(SETTINGS, "SENTIMENT_RADAR_DIVERGENCE_THRESHOLD", 10.0))
    for item in payload:
        retail = item["metrics"].get("global_account")
        top_position = item["metrics"].get("top_position")
        if retail is not None and top_position is not None:
            retail_share = 100.0 * retail / (1.0 + retail)
            position_share = 100.0 * top_position / (1.0 + top_position)
            difference_pp = abs(retail_share - position_share)
            if difference_pp >= threshold_pp:
                divergences.append({"symbol": item["symbol"], "global_account": retail,
                                    "top_position": top_position,
                                    "global_account_long_share_pct": retail_share,
                                    "top_position_long_share_pct": position_share,
                                    "difference_pp": difference_pp})
    divergences.sort(key=lambda item: item["difference_pp"], reverse=True)
    return {"watchlist_size": len(payload), "averages": averages, "trends": trends,
            "metric_counts": metric_counts, "long_share_averages": long_share_averages,
            "symbol_changes": symbol_changes,
            "divergences": divergences, "symbols": payload}


def run_sentiment_radar_check(state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not getattr(SETTINGS, "SENTIMENT_RADAR_ENABLED", True):
        return None
    watchlist_size = max(1, int(getattr(SETTINGS, "SENTIMENT_RADAR_WATCHLIST_SIZE", 12)))
    period = str(getattr(SETTINGS, "SENTIMENT_RADAR_PERIOD", "15m"))
    try:
        period_ms = _period_milliseconds(period)
        tolerance_ms = period_ms * max(1, int(getattr(SETTINGS, "SENTIMENT_RADAR_STALENESS_TOLERANCE_MULT", 2)))
    except (TypeError, ValueError) as e:
        log.warning(f"Sentiment radar period ayarı geçersiz: {e}")
        return None
    try:
        watchlist = get_top_symbols_by_volume(watchlist_size)
        if not watchlist:
            log.warning("Sentiment radar watchlist boş; state korunuyor")
            return None
    except Exception as e:
        log.warning(f"Sentiment radar watchlist alınamadı; state korunuyor: {e}")
        return None

    active_endpoints = [(name, getter, field) for name, (flag, getter, field) in _ENDPOINTS.items()
                        if getattr(SETTINGS, flag, True)]
    if not active_endpoints:
        log.warning("Sentiment radar için aktif endpoint yok; state korunuyor")
        return None
    with _SENTIMENT_LOCK:
        old_state = dict(state if state is not None else _load_sentiment_state())
        old_symbols = dict(old_state.get("symbols", {}))

    payload = []
    symbols_with_data = 0
    now_ms = int(time.time() * 1000)
    for symbol in watchlist:
        metrics, timestamps = {}, {}
        for name, getter, field in active_endpoints:
            metrics[name] = None
            try:
                response = getter(symbol, period)
                if not isinstance(response, list) or not response:
                    log.debug(f"Sentiment {name} verisi boş ({symbol})")
                    continue
                point = response[-1]
                if not isinstance(point, dict):
                    log.debug(f"Sentiment {name} noktası geçersiz ({symbol})")
                    continue
                timestamp = int(point.get("timestamp", 0))
                if timestamp <= 0 or now_ms - timestamp > tolerance_ms:
                    log.debug(f"Sentiment {name} verisi eski/geçersiz ({symbol}, ts={timestamp})")
                    continue
                value = float(point.get(field))
                if value <= 0:
                    log.debug(f"Sentiment {name} oranı geçersiz ({symbol}: {value})")
                    continue
                metrics[name] = value
                timestamps[name] = timestamp
            except Exception as e:
                log.debug(f"Sentiment {name} çağrısı/parse hatası ({symbol}): {e}")
        if any(value is not None for value in metrics.values()):
            symbols_with_data += 1
        else:
            log.debug(f"Sentiment radar sembolünde kullanılabilir metrik yok ({symbol})")
        payload.append({"symbol": symbol, "metrics": metrics, "ts": timestamps})

    if symbols_with_data < len(watchlist) / 2:
        log.warning(f"Sentiment radar sembollerinin yarısından fazlasında tüm metrikler başarısız ({len(watchlist) - symbols_with_data}/{len(watchlist)}); state korunuyor")
        return None

    change_eps = float(getattr(SETTINGS, "SENTIMENT_RADAR_CHANGE_EPS", 0.05))
    report = _build_report(payload, old_state, change_eps)
    report["period"] = period
    new_symbols = {item["symbol"]: {"metrics": item["metrics"], "ts": item["ts"]} for item in payload}
    if len(new_symbols) < len(old_symbols):
        log.debug(f"Sentiment state'ten watchlist dışındaki {len(old_symbols) - len(new_symbols)} sembol prune edildi")
    first_run = not old_symbols
    new_state = {"symbols": new_symbols, "last_averages": report["averages"], "updatedAt": int(time.time())}
    with _SENTIMENT_LOCK:
        _save_sentiment_state(new_state)
    if first_run:
        log.info(f"Sentiment radar ilk snapshot kaydedildi ({len(payload)} sembol); alarm üretilmedi")
        return None
    only_on_change = getattr(SETTINGS, "SENTIMENT_RADAR_SEND_ONLY_ON_CHANGE", True)
    old_averages = old_state.get("last_averages", {})
    changed = any(value is not None and (old_averages.get(name) is None or abs(value - float(old_averages[name])) >= change_eps)
                  for name, value in report["averages"].items())
    changed = changed or any(abs(delta) >= change_eps for deltas in report["symbol_changes"].values()
                             for delta in deltas.values())
    if only_on_change and not changed:
        log.info("Sentiment radar ortalamalarında bildirim eşiğini aşan değişiklik yok")
        return None
    log.info(f"Sentiment radar raporu hazır: {symbols_with_data}/{len(watchlist)} sembol")
    return report


def fmt_sentiment_radar(report: Dict[str, Any]) -> str:
    period = report["period"]
    lines = [f"🧭 PİYASA SENTIMENT RADARI (Top {report['watchlist_size']} Hacim, {period})", ""]
    labels = {
        "global_account": "Perakende proxy Ort. L/S (Account)",
        "top_account": "Top Trader Ort. L/S (Account)",
        "top_position": "Top Trader Ort. L/S (Position)",
        "taker": "Taker emir akışı Ort. Buy/Sell",
    }
    for name, label in labels.items():
        value = report["averages"].get(name)
        if value is not None:
            count = report.get("metric_counts", {}).get(name, report["watchlist_size"])
            suffix = f" | {count}/{report['watchlist_size']} sembol"
            share = report.get("long_share_averages", {}).get(name)
            if share is not None:
                suffix += f" | ort. long payı %{fmt_pct(share, 1)}"
            lines.append(f"{label}: {fmt_pct(value, 3)} — {report['trends'].get(name, 'veri yok')}{suffix}")
    top_n = max(1, int(getattr(SETTINGS, "SENTIMENT_RADAR_TOP_N_DIVERGENCE", 5)))
    divergences = report["divergences"][:top_n]
    if divergences:
        lines.extend(["", "⚡ Long payı farkı (Perakende vs Top Trader Position):"])
        for index, item in enumerate(divergences, 1):
            retail_side = "long" if item["global_account"] > 1 else "short"
            top_side = "long" if item["top_position"] > 1 else "short"
            lines.append(
                f"{index}. {item['symbol']}: Perakende {fmt_pct(item['global_account_long_share_pct'], 1)}% {retail_side} hesap payı "
                f"(L/S {fmt_pct(item['global_account'], 3)}) vs Top Trader {fmt_pct(item['top_position_long_share_pct'], 1)}% {top_side} pozisyon payı "
                f"(L/S {fmt_pct(item['top_position'], 3)}) — fark: {fmt_pct(item['difference_pp'], 1)} yüzde puan"
            )
    lines.extend(["", "Not: Her metrik ayrı popülasyon/akış proxy'sidir ve basit, ağırlıksız ortalamadır. Long payı farkı yönlerin zıt olduğu anlamına gelmez. Taker oranı agresif emir akışını gösterir, doğrudan piyasa yönü değildir. İzlenen Top hacimli sembollerle sınırlıdır."])
    return "\n".join(lines)


def send_sentiment_radar(report: Dict[str, Any]) -> None:
    send_telegram(fmt_sentiment_radar(report))
