"""Binance USDT-M sözleşme durumları ve risk parametrelerindeki değişiklikleri izler."""
import json
import os
import shutil
import threading
import time
from typing import Any, Dict, Optional

from config import SETTINGS
from exchange import get_exchange_info
from logger import get_logger
from telegram_notify import send_telegram

log = get_logger("exchange_risk_watch")
RISK_STATE_FILE = os.path.join("state", "exchange_risk_state.json")
_RISK_LOCK = threading.Lock()


def _load_risk_state() -> Dict[str, Any]:
    if not os.path.exists(RISK_STATE_FILE):
        return {}
    try:
        with open(RISK_STATE_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        if isinstance(value, dict):
            return value
        log.warning("Risk watch state biçimi geçersiz; boş state kullanılıyor")
    except Exception as e:
        log.warning(f"Risk watch state yükleme hatası: {e}")
    return {}


def _save_risk_state(state: Dict[str, Any]) -> None:
    tmp = RISK_STATE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        for attempt in range(5):
            try:
                os.replace(tmp, RISK_STATE_FILE)
                return
            except (PermissionError, OSError) as e:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        shutil.copy2(tmp, RISK_STATE_FILE)
                        os.remove(tmp)
                    except Exception as fallback_error:
                        log.warning(f"Risk watch state fallback yazma hatası: {fallback_error}")
                        raise e
    except Exception as e:
        log.warning(f"Risk watch state kaydetme hatası: {e}")


def _build_diff(old_snapshot: Dict[str, Any], new_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    old_symbols = old_snapshot.get("symbols", {})
    new_symbols = new_snapshot.get("symbols", {})
    report = {"new": [], "status": [], "delisted": [], "leverage": [], "margin": []}
    notify_new = getattr(SETTINGS, "RISK_WATCH_NOTIFY_NEW_SYMBOLS", True)
    notify_status = getattr(SETTINGS, "RISK_WATCH_NOTIFY_STATUS_CHANGES", True)
    notify_delisted = getattr(SETTINGS, "RISK_WATCH_NOTIFY_DELISTED", True)
    for symbol, current in new_symbols.items():
        previous = old_symbols.get(symbol)
        if previous is None:
            if notify_new and current.get("contractType") == "PERPETUAL":
                report["new"].append({"symbol": symbol, "onboardDate": current.get("onboardDate")})
            continue
        if notify_status and previous.get("status") != current.get("status"):
            report["status"].append({"symbol": symbol, "from": previous.get("status"), "to": current.get("status")})
        old_lev, new_lev = previous.get("maxLeverage"), current.get("maxLeverage")
        if old_lev is not None and new_lev is not None and old_lev != new_lev:
            report["leverage"].append({"symbol": symbol, "from": old_lev, "to": new_lev})
        old_margin, new_margin = previous.get("maintMarginRatio"), current.get("maintMarginRatio")
        if old_margin is not None and new_margin is not None and old_margin != new_margin:
            report["margin"].append({"symbol": symbol, "from": old_margin, "to": new_margin})
    if notify_delisted:
        report["delisted"] = [{"symbol": symbol} for symbol in old_symbols if symbol not in new_symbols]
    return report


def _snapshot_from_exchange_info(data: Dict[str, Any]) -> Dict[str, Any]:
    symbols: Dict[str, Any] = {}
    blacklist = set(getattr(SETTINGS, "SYMBOL_BLACKLIST", ()))
    blacklist.update(getattr(SETTINGS, "NON_STANDARD_SYMBOLS", ()))
    for item in data.get("symbols", []):
        if not isinstance(item, dict):
            log.debug("exchangeInfo sembol satırı dict değil; atlandı")
            continue
        symbol = item.get("symbol")
        if (not symbol or symbol in blacklist or item.get("contractType") != "PERPETUAL"
                or item.get("quoteAsset") != "USDT"):
            continue
        symbols[symbol] = {
            "status": item.get("status"),
            "contractType": item.get("contractType"),
            "onboardDate": item.get("onboardDate"),
            "maxLeverage": None,
            "maintMarginRatio": None,
        }
    return {"symbols": symbols, "updatedAt": int(time.time())}


def run_risk_watch_check(state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not getattr(SETTINGS, "RISK_WATCH_ENABLED", True):
        return None
    if getattr(SETTINGS, "RISK_WATCH_LEVERAGE_ENABLED", False):
        log.warning("Leverage izleme istendi ancak exchange.py içinde signed GET altyapısı yok; yalnızca exchangeInfo izleniyor")
    try:
        data = get_exchange_info()
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            log.warning("Risk watch exchangeInfo yanıtı geçersiz; snapshot korunuyor")
            return None
        if len(data["symbols"]) < 50:
            log.warning(f"Risk watch exchangeInfo anormal küçük ({len(data['symbols'])} sembol); snapshot korunuyor")
            return None
    except Exception as e:
        log.warning(f"Risk watch exchangeInfo alınamadı; snapshot korunuyor: {e}")
        return None

    new_snapshot = _snapshot_from_exchange_info(data)
    # Eski snapshot persist öncesi bellekte kopyalanır; diff yeni veri yazılmadan hesaplanır.
    with _RISK_LOCK:
        old_snapshot = dict(state if state is not None else _load_risk_state())
        old_snapshot["symbols"] = dict(old_snapshot.get("symbols", {}))
    if not old_snapshot.get("symbols"):
        with _RISK_LOCK:
            _save_risk_state(new_snapshot)
        return {"initial": True, "count": len(new_snapshot["symbols"])}

    report = _build_diff(old_snapshot, new_snapshot)
    with _RISK_LOCK:
        _save_risk_state(new_snapshot)
    total = sum(len(items) for items in report.values())
    if total:
        log.info(f"Risk watch değişiklikleri bulundu: {total}")
        return report
    log.info("Risk watch: sözleşme veya risk parametresi değişikliği yok")
    return None


def fmt_risk_watch(report: Dict[str, Any]) -> str:
    if report.get("initial"):
        return f"🛡️ Borsa risk izlemesi başlatıldı: {report.get('count', 0)} sembol kaydedildi."
    max_items = max(1, int(getattr(SETTINGS, "RISK_WATCH_MAX_ITEMS_PER_MSG", 15)))
    sections = ["🛡️ BORSA RİSK DEĞİŞİKLİĞİ ALARMI"]
    categories = [
        ("status", "⚠️ Durum Değişikliği", lambda x: f"• {x['symbol']}: {x['from']} → {x['to']}" + ("  ❗pozisyon varsa kontrol edin" if x.get("from") == "TRADING" and x.get("to") != "TRADING" else "")),
        ("leverage", "🔻 Kaldıraç Değişikliği", lambda x: f"• {x['symbol']}: {x['from']}x → {x['to']}x"),
        ("margin", "📈 Bakım Marjı Değişikliği", lambda x: f"• {x['symbol']}: {x['from']} → {x['to']}"),
        ("new", "🆕 Yeni Listelenen", lambda x: f"• {x['symbol']} (onboard: {x.get('onboardDate') or 'bilinmiyor'})"),
        ("delisted", "🗑️ Listeden Kaldırılan", lambda x: f"• {x['symbol']}"),
    ]
    for key, title, render in categories:
        items = report.get(key, [])
        if not items:
            continue
        sections.append(f"\n{title}:")
        sections.extend(render(item) for item in items[:max_items])
        if len(items) > max_items:
            sections.append(f"• ... ve {len(items) - max_items} tane daha")
    sections.append("\nNot: Kaldıraç/marjin değişiklikleri tek başına alım/satım sinyali değildir. Durum değişikliklerinde açık pozisyonlarınızı kontrol edin.")
    return "\n".join(sections)


def send_risk_watch(report: Dict[str, Any]) -> None:
    if report.get("initial") and getattr(SETTINGS, "RISK_WATCH_FIRST_RUN_SILENT", False):
        return
    send_telegram(fmt_risk_watch(report))
