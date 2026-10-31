"""Daily broad-crypto sentiment context from Alternative.me's free FNG API."""
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from config import SETTINGS
from logger import get_logger
from telegram_notify import send_telegram

log = get_logger("crypto_fear_greed")
FNG_API_URL = "https://api.alternative.me/fng/"
FNG_STATE_FILE = os.path.join("state", "crypto_fear_greed_state.json")
_FNG_LOCK = threading.Lock()


def _load_fng_state() -> Dict[str, Any]:
    if not os.path.exists(FNG_STATE_FILE):
        return {}
    try:
        with open(FNG_STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
        log.warning("Fear & Greed state biçimi geçersiz; boş state kullanılıyor")
    except Exception as e:
        log.warning(f"Fear & Greed state yükleme hatası: {e}")
    return {}


def _save_fng_state(state: Dict[str, Any]) -> None:
    tmp = FNG_STATE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(FNG_STATE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        for attempt in range(5):
            try:
                os.replace(tmp, FNG_STATE_FILE)
                return
            except (PermissionError, OSError) as e:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        shutil.copy2(tmp, FNG_STATE_FILE)
                        os.remove(tmp)
                    except Exception as fallback_error:
                        log.warning(f"Fear & Greed state fallback yazma hatası: {fallback_error}")
                        raise e
    except Exception as e:
        log.warning(f"Fear & Greed state kaydetme hatası: {e}")


def _parse_index_row(row: Dict[str, Any]) -> Dict[str, Any]:
    value = int(row["value"])
    timestamp = int(row["timestamp"])
    if not 0 <= value <= 100 or timestamp <= 0:
        raise ValueError("index value/timestamp sınır dışında")
    return {
        "value": value,
        "classification": str(row["value_classification"]),
        "timestamp": timestamp,
    }


def _build_report(rows: List[Dict[str, Any]], state: Dict[str, Any], notable_delta: int) -> Dict[str, Any]:
    current = rows[0]
    previous = rows[1] if len(rows) > 1 else None
    week_ago = rows[7] if len(rows) >= 8 else None
    previous_value = previous["value"] if previous else state.get("value")
    week_value = week_ago["value"] if len(rows) >= 7 else None
    day_delta = current["value"] - previous_value if previous_value is not None else None
    week_delta = current["value"] - week_value if week_value is not None else None
    return {
        **current,
        "previous_value": previous_value,
        "week_value": week_value,
        "day_delta": day_delta,
        "week_delta": week_delta,
        "notable_change": day_delta is not None and abs(day_delta) >= notable_delta,
        "history_count": len(rows),
    }


def run_fear_greed_check(state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not getattr(SETTINGS, "CRYPTO_FNG_ENABLED", True):
        return None
    request_timeout = max(1, int(getattr(SETTINGS, "CRYPTO_FNG_REQUEST_TIMEOUT", 10)))
    max_age_seconds = max(1, int(getattr(SETTINGS, "CRYPTO_FNG_MAX_AGE_HOURS", 48))) * 3600
    notable_delta = max(1, int(getattr(SETTINGS, "CRYPTO_FNG_ALERT_DELTA", 8)))
    try:
        response = requests.get(FNG_API_URL, params={"limit": 8, "format": "json"}, timeout=request_timeout)
        response.raise_for_status()
        payload = response.json()
        metadata = {}
        if isinstance(payload, dict):
            metadata = payload.get("metadata") or {}
        if not isinstance(payload, dict) or not isinstance(metadata, dict) or metadata.get("error"):
            raise ValueError(f"API metadata error: {metadata.get('error') if isinstance(metadata, dict) else 'invalid response'}")
        raw_rows = payload.get("data")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("API data list is empty or invalid")
        rows = []
        for row in raw_rows:
            try:
                if isinstance(row, dict):
                    rows.append(_parse_index_row(row))
            except (KeyError, TypeError, ValueError) as e:
                log.debug(f"Fear & Greed history satırı atlandı: {e}")
        rows.sort(key=lambda item: item["timestamp"], reverse=True)
        if not rows:
            raise ValueError("no valid index rows")
    except (requests.RequestException, ValueError, TypeError) as e:
        log.warning(f"Alternative.me Fear & Greed verisi alınamadı: {e}")
        return None

    current = rows[0]
    now = int(time.time())
    age = now - current["timestamp"]
    if age < -300 or age > max_age_seconds:
        log.warning(f"Fear & Greed verisi güncel değil (yaş={age}s); state korunuyor")
        return None

    with _FNG_LOCK:
        old_state = dict(state if state is not None else _load_fng_state())
    first_run = old_state.get("timestamp") is None
    report = _build_report(rows, old_state, notable_delta)
    changed = current["timestamp"] != old_state.get("timestamp") or current["value"] != old_state.get("value")
    if changed:
        new_state = {"value": current["value"], "classification": current["classification"],
                     "timestamp": current["timestamp"], "updatedAt": now}
        with _FNG_LOCK:
            _save_fng_state(new_state)
    if first_run:
        if getattr(SETTINGS, "CRYPTO_FNG_FIRST_RUN_NOTIFY", True):
            return report
        log.info("Fear & Greed ilk snapshot kaydedildi; ilk bildirim kapalı")
        return None
    if not changed:
        log.info("Fear & Greed indeksi değişmedi; Telegram mesajı gönderilmiyor")
        return None
    log.info(f"Fear & Greed yeni günlük değer: {current['value']} ({current['classification']})")
    return report


def fmt_fear_greed(report: Dict[str, Any]) -> str:
    updated = datetime.fromtimestamp(report["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["🌡️ KRİPTO PİYASA DUYARLILIĞI", "",
             f"Fear & Greed: {report['value']}/100 — {report['classification']}"]
    if report.get("day_delta") is not None:
        sign = "+" if report["day_delta"] > 0 else ""
        lines.append(f"24 saatlik değişim: {sign}{report['day_delta']} puan (önceki: {report['previous_value']})")
    if report.get("week_delta") is not None and report.get("history_count", 0) >= 2:
        sign = "+" if report["week_delta"] > 0 else ""
        lines.append(f"7 günlük değişim: {sign}{report['week_delta']} puan (önceki: {report['week_value']})")
    if report.get("notable_change"):
        lines.append("⚠️ Günlük hareket belirgin.")
    lines.extend(["", f"Veri zamanı: {updated}",
                  "Not: Bu, Alternative.me'nin geniş kripto piyasası duyarlılık endeksidir; Binance vadeli pozisyonlanmasını ölçmez ve tek başına işlem sinyali değildir.",
                  "Kaynak: Alternative.me Crypto Fear & Greed Index (alternative.me)"])
    return "\n".join(lines)


def send_fear_greed(report: Dict[str, Any]) -> None:
    send_telegram(fmt_fear_greed(report))
