"""
Futures Volume Dominance (Piyasa Geneli Hacim Hakimiyeti)
==========================================================

Binance USDT-M Futures piyasasındaki TÜM perpetual sembollerin 24s
hacim verisini (/fapi/v1/ticker/24hr, tek çağrı) kullanarak piyasa
geneli hacim dağılımı ve genişlik (breadth) raporu üretir.

Metrikler:
- Toplam piyasa quote volume
- Majors (BTC+ETH vb.) toplam hacim payı ("Majors Dominance")
- Top-N sembol konsantrasyon oranı
- Piyasa genişliği: yükseliş/düşüş sembol sayısı ve oranı
- Hacim-ağırlıklı ortalama fiyat değişimi (piyasa yönü proxy'si)
- Majors dominance yönü (önceki ölçüme göre artıyor/azalıyor/sabit)

Veri kaynağı: Binance /fapi/v1/ticker/24hr (tüm semboller, tek çağrı)
exchange.py'deki get_24hr_tickers() aracılığıyla erişilir.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import SETTINGS
from exchange import get_24hr_tickers
from logger import get_logger
from telegram_notify import fmt_pct, fmt_usd_compact, send_telegram

log = get_logger("volume_dominance")

# State dosyası
DOMINANCE_STATE_FILE = os.path.join("state", "volume_dominance_state.json")

# Thread-safety
_DOMINANCE_LOCK = threading.Lock()

# Son başarılı veri cache'i (stale kullanımı için)
_LAST_SUCCESS_DATA: Optional[Dict[str, Any]] = None


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _load_dominance_state() -> Dict[str, Any]:
    if not os.path.exists(DOMINANCE_STATE_FILE):
        return {}
    try:
        with open(DOMINANCE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Volume dominance state yükleme hatası: {e}")
        return {}


def _save_dominance_state(state: Dict[str, Any]) -> None:
    """state_manager.py atomic-write pattern'ini takip eder"""
    try:
        _ensure_dir(DOMINANCE_STATE_FILE)
        tmp = DOMINANCE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str, ensure_ascii=False)
        # atomic replace with retry
        for attempt in range(5):
            try:
                os.replace(tmp, DOMINANCE_STATE_FILE)
                return
            except (PermissionError, OSError):
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        import shutil

                        shutil.copy2(tmp, DOMINANCE_STATE_FILE)
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception as e:
                        log.warning(f"Volume dominance state yazma uyarısı: {e}")
                    return
    except Exception as e:
        log.warning(f"Volume dominance state kaydetme hatası: {e}")


def _is_valid_number(v: Any) -> bool:
    if v is None:
        return False
    try:
        # NaN kontrolü
        if isinstance(v, float) and (v != v):  # NaN
            return False
        float(v)
        return True
    except Exception:
        return False


def _safe_float(v: Any, default: float = 0.0) -> float:
    if not _is_valid_number(v):
        return default
    try:
        return float(v)
    except Exception:
        return default


def run_volume_dominance_check(state: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """
    Piyasa geneli hacim hakimiyeti analizi çalıştırır.
    state: önceki ölçümü taşımak için kullanılabilir (load/persist pattern uyumlu)
    """
    global _LAST_SUCCESS_DATA

    if state is None:
        state = _load_dominance_state()

    enabled = getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_ENABLED", True)
    if not enabled:
        return None

    min_quote_vol = getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_MIN_QUOTE_VOLUME", 0.0)
    change_eps = getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_CHANGE_EPS", 0.5)

    try:
        tickers = get_24hr_tickers()
    except Exception as e:
        log.warning(f"24hr ticker çağrısı başarısız: {e}")
        with _DOMINANCE_LOCK:
            cached = _LAST_SUCCESS_DATA
        if cached is not None:
            return _build_report(cached, state, stale=True, change_eps=change_eps)
        return None

    if not isinstance(tickers, list) or len(tickers) < 10:
        log.warning("24hr ticker yanıtı beklenmedik türde veya anormal derecede küçük, işlem iptal edildi")
        with _DOMINANCE_LOCK:
            cached = _LAST_SUCCESS_DATA
        if cached is not None:
            return _build_report(cached, state, stale=True, change_eps=change_eps)
        return None

    # Parse ve filtrele (tüm USDT sembolleri, quoteVolume > 0)
    rows = []
    for t in tickers:
        try:
            if not isinstance(t, dict):
                continue
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            if symbol in getattr(SETTINGS, "SYMBOL_BLACKLIST", ()):
                continue
            if symbol in getattr(SETTINGS, "NON_STANDARD_SYMBOLS", ()):
                continue
            quote_volume = _safe_float(t.get("quoteVolume"), 0.0)
            if quote_volume <= 0.0:
                continue
            if min_quote_vol > 0 and quote_volume < min_quote_vol:
                continue
            price_change_pct = _safe_float(t.get("priceChangePercent"), None)
            if price_change_pct is None:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "quote_volume": quote_volume,
                    "price_change_percent": price_change_pct,
                }
            )
        except Exception as e:
            log.debug(f"Ticker satırı atlandı: {e}")

    if len(rows) < 10:
        log.warning("Geçerli satır sayısı çok düşük, işlem iptal edildi")
        with _DOMINANCE_LOCK:
            cached = _LAST_SUCCESS_DATA
        if cached is not None:
            return _build_report(cached, state, stale=True, change_eps=change_eps)
        return None

    payload = {
        "rows": rows,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _DOMINANCE_LOCK:
        _LAST_SUCCESS_DATA = payload

    return _build_report(payload, state, stale=False, change_eps=change_eps)


def _build_report(payload: Dict[str, Any], state: Dict[str, Any], stale: bool = False, change_eps: float = 0.5) -> Optional[Dict[str, Any]]:
    rows = payload.get("rows", [])
    if not rows:
        return None

    # Karşılaştırma için ESKİ dominance state'ini persist'ten ÖNCE yakala
    prev_dominance = dict(state.get("dominance_state", {})) if isinstance(state.get("dominance_state"), dict) else {}

    major_symbols = tuple(getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_MAJOR_SYMBOLS", ("BTCUSDT", "ETHUSDT")))
    top_n = getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_TOP_N", 10)

    vol_map: Dict[str, float] = {r["symbol"]: r["quote_volume"] for r in rows}
    total_quote_volume = sum(vol_map.values())

    # Majors payı (API yanıtında yoksa %0 pay, sadece uyarı, exception yok)
    major_volume = 0.0
    for sym in major_symbols:
        if sym in vol_map:
            major_volume += vol_map[sym]
        else:
            log.warning(f"Major sembol API yanıtında bulunamadı, %0 pay ile sayılıyor: {sym}")
    majors_share = (major_volume / total_quote_volume * 100.0) if total_quote_volume > 0 else 0.0

    # Top-N konsantrasyon oranı
    top_rows = sorted(rows, key=lambda x: x["quote_volume"], reverse=True)[:top_n]
    top_volume = sum(r["quote_volume"] for r in top_rows)
    concentration = (top_volume / total_quote_volume * 100.0) if total_quote_volume > 0 else 0.0

    # Piyasa genişliği (breadth): yükseliş / düşüş
    advancers = [r for r in rows if r["price_change_percent"] > 0]
    decliners = [r for r in rows if r["price_change_percent"] < 0]
    total = len(rows)
    advancers_cnt = len(advancers)
    decliners_cnt = len(decliners)
    advancers_ratio = (advancers_cnt / total * 100.0) if total > 0 else 0.0
    decliners_ratio = (decliners_cnt / total * 100.0) if total > 0 else 0.0

    # Hacim-ağırlıklı ortalama fiyat değişimi (piyasa yönü proxy'si)
    weighted_sum = sum(r["price_change_percent"] * r["quote_volume"] for r in rows)
    vwap_change = (weighted_sum / total_quote_volume) if total_quote_volume > 0 else 0.0

    # Majors dominance yönü (önceki ölçüme göre, tek change_eps config'ten)
    prev_majors_share = prev_dominance.get("majors_share")
    direction = "sabit"
    if _is_valid_number(prev_majors_share):
        prev_m = float(prev_majors_share)
        if majors_share > prev_m + change_eps:
            direction = "artıyor"
        elif majors_share < prev_m - change_eps:
            direction = "azalıyor"
        else:
            direction = "sabit"

    report = {
        "total_quote_volume": total_quote_volume,
        "majors": list(major_symbols),
        "majors_volume": major_volume,
        "majors_share": majors_share,
        "majors_share_prev": float(prev_majors_share) if _is_valid_number(prev_majors_share) else None,
        "top_n": top_n,
        "top_volume": top_volume,
        "concentration": concentration,
        "top_rows": top_rows,
        "advancers_cnt": advancers_cnt,
        "decliners_cnt": decliners_cnt,
        "advancers_ratio": advancers_ratio,
        "decliners_ratio": decliners_ratio,
        "vwap_change": vwap_change,
        "direction": direction,
        "total_symbols": total,
        "stale": stale,
        "ts": payload.get("ts"),
        "prev_dominance_state": prev_dominance,
    }

    # State güncelle
    new_state = dict(state)
    new_state["dominance_state"] = {
        "majors_share": majors_share,
        "direction": direction,
        "concentration": concentration,
        "ts": report["ts"],
    }
    if not stale:
        new_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    # stale modda updated_at güncellenmez

    # State'i kaydet (atomic) - ayrı bir lock bloğu, kendi içinde kilidi tekrar almaz
    try:
        with _DOMINANCE_LOCK:
            dom_state = _load_dominance_state()
            dom_state.update(new_state)
            _save_dominance_state(dom_state)
    except Exception as e:
        log.warning(f"Dominance state persist hatası: {e}")

    report["state"] = new_state
    return report


def fmt_volume_dominance(report: Dict[str, Any]) -> str:
    if not report:
        return ""

    stale = report.get("stale", False)
    header = "📊 *PİYASA HACİM HAKİMİYETİ*"
    if stale:
        header = "⚠️ Veri güncellenemedi, son bilinen değerler:\n📊 *PİYASA HACİM HAKİMİYETİ*"

    total_vol = report.get("total_quote_volume", 0.0)
    majors = report.get("majors", [])
    majors_share = report.get("majors_share", 0.0)
    direction = report.get("direction", "sabit")
    concentration = report.get("concentration", 0.0)
    top_rows = report.get("top_rows", [])
    top_n = report.get("top_n", 10)

    dir_emoji = "➡️"
    if direction == "artıyor":
        dir_emoji = "📈"
    elif direction == "azalıyor":
        dir_emoji = "📉"

    advancers_cnt = report.get("advancers_cnt", 0)
    decliners_cnt = report.get("decliners_cnt", 0)
    advancers_ratio = report.get("advancers_ratio", 0.0)
    vwap_change = report.get("vwap_change", 0.0)

    lines: List[str] = []
    lines.append(header)
    lines.append(f"Toplam Piyasa Hacmi (24s): {fmt_usd_compact(total_vol)}")
    lines.append(f"Majors ({' + '.join(majors)}) Payı: {fmt_pct(majors_share)}% {dir_emoji} {direction}")
    lines.append(f"Top {top_n} Konsantrasyonu: {fmt_pct(concentration)}%")
    lines.append("")
    lines.append("🏆 Hacim Liderleri:")
    if top_rows and total_vol > 0:
        for i, c in enumerate(top_rows, 1):
            pay = c["quote_volume"] / total_vol * 100.0
            lines.append(f"{i}. {c['symbol']} - {fmt_pct(pay, 1)}%")
    else:
        lines.append("1. -")
    lines.append("")
    lines.append(f"📈 Piyasa Genişliği: {advancers_cnt} yükseliş / {decliners_cnt} düşüş (%{fmt_pct(advancers_ratio, 1)} pozitif)")
    vwap_sign = "+" if vwap_change >= 0 else ""
    lines.append(f"Hacim-Ağırlıklı Ort. Değişim (proxy): {vwap_sign}{fmt_pct(vwap_change)}%")
    lines.append("")
    lines.append("[Not: Bu rapor piyasa genelindeki hacim dağılımını ve genişliğini özetler; hacim-ağırlıklı değişim dolaylı bir piyasa yönü göstergesidir, doğrudan alım/satım sinyali değildir.]")

    return "\n".join(lines)


def send_volume_dominance(report: Dict[str, Any]) -> None:
    if not report:
        return

    send_only_on_change = getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_SEND_ONLY_ON_CHANGE", True)
    change_eps = getattr(SETTINGS, "FUTURES_VOLUME_DOMINANCE_CHANGE_EPS", 0.5)

    if send_only_on_change and not report.get("stale", False):
        prev = report.get("prev_dominance_state", {}) if isinstance(report.get("prev_dominance_state"), dict) else {}
        if prev:
            prev_share = _safe_float(prev.get("majors_share"), 0.0)
            same_share = abs(prev_share - float(report.get("majors_share", 0.0))) < change_eps
            same_direction = prev.get("direction") == report.get("direction")
            if same_share and same_direction:
                log.info("Hacim hakimiyeti: anlamlı değişiklik yok, mesaj atlanıyor")
                return

    try:
        msg = fmt_volume_dominance(report)
        send_telegram(msg)
    except Exception as e:
        log.warning(f"Hacim hakimiyeti mesaj gönderimi hatası: {e}")


if __name__ == "__main__":
    log.info("Volume Dominance bağımsız test modu")
    state = _load_dominance_state()
    rep = run_volume_dominance_check(state)
    if rep:
        print(fmt_volume_dominance(rep))
    else:
        print("Rapor üretilemedi")
