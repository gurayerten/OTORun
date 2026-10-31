"""
Market-Wide Funding Pulse
=========================

Piyasa geneli (tüm USDT-M perpetual) funding rate nabzı raporu üretir.
Mevcut funding_radar.py'nin tek-coin odaklı analizini tüm piyasa seviyesine taşır.

Veri kaynağı: Binance /fapi/v1/premiumIndex (tüm semboller, tek çağrı)
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import SETTINGS
from exchange import get_all_premium_index, get_24hr_tickers
from logger import get_logger
from telegram_notify import fmt_pct, send_telegram

log = get_logger("funding_pulse")

# State dosyası
PULSE_STATE_FILE = os.path.join("state", "funding_pulse_state.json")

# Thread-safety
_PULSE_LOCK = threading.Lock()

# Son başarılı veri cache'i (stale kullanımı için)
_LAST_SUCCESS_DATA: Optional[Dict[str, Any]] = None


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _load_pulse_state() -> Dict[str, Any]:
    if not os.path.exists(PULSE_STATE_FILE):
        return {}
    try:
        with open(PULSE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Funding pulse state yükleme hatası: {e}")
        return {}


def _save_pulse_state(state: Dict[str, Any]) -> None:
    """state_manager.py atomic-write pattern'ini takip eder"""
    try:
        _ensure_dir(PULSE_STATE_FILE)
        tmp = PULSE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str, ensure_ascii=False)
        # atomic replace with retry
        for attempt in range(5):
            try:
                os.replace(tmp, PULSE_STATE_FILE)
                return
            except (PermissionError, OSError):
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        import shutil

                        shutil.copy2(tmp, PULSE_STATE_FILE)
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception as e:
                        log.warning(f"Funding pulse state yazma uyarısı: {e}")
                    return
    except Exception as e:
        log.warning(f"Funding pulse state kaydetme hatası: {e}")


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


def _compute_median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _compute_mean(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def run_funding_pulse_check(state: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """
    Piyasa geneli funding pulse analizi çalıştırır.
    state: önceki ölçümü taşımak için kullanılabilir (load/persist pattern uyumlu)
    """
    global _LAST_SUCCESS_DATA

    if state is None:
        state = _load_pulse_state()

    enabled = getattr(SETTINGS, "FUNDING_PULSE_ENABLED", True)
    if not enabled:
        return None

    extreme_pct = getattr(SETTINGS, "FUNDING_PULSE_EXTREME_PCT", 0.05)  # yüzde
    top_n = getattr(SETTINGS, "FUNDING_PULSE_TOP_N", 5)
    min_quote_vol = getattr(SETTINGS, "FUNDING_PULSE_MIN_QUOTE_VOLUME", 0.0)
    change_eps = getattr(SETTINGS, "FUNDING_PULSE_CHANGE_EPS", 1e-6)

    try:
        data = get_all_premium_index()
    except Exception as e:
        log.warning(f"premiumIndex çağrısı başarısız: {e}")
        with _PULSE_LOCK:
            cached = _LAST_SUCCESS_DATA
        if cached is not None:
            return _build_report(cached, state, stale=True, change_eps=change_eps)
        return None

    if not data or not isinstance(data, list) or len(data) < 10:
        log.warning("premiumIndex yanıtı boş veya anormal derecede küçük, işlem iptal edildi")
        with _PULSE_LOCK:
            cached = _LAST_SUCCESS_DATA
        if cached is not None:
            return _build_report(cached, state, stale=True, change_eps=change_eps)
        return None

    # Hacim verisi (gerekliyse 24hr ticker'dan merge)
    volumes_map: Dict[str, float] = {}
    apply_volume_filter = min_quote_vol > 0

    if apply_volume_filter:
        try:
            tickers = get_24hr_tickers()
            if isinstance(tickers, list):
                for t in tickers:
                    if not isinstance(t, dict):
                        continue
                    sym = t.get("symbol")
                    if sym:
                        volumes_map[sym] = _safe_float(t.get("quoteVolume"), 0.0)
            else:
                log.warning("24hr ticker beklenmeyen tip, bu turda hacim filtresi devre dışı")
                apply_volume_filter = False
        except Exception as e:
            log.warning(f"24hr ticker hacim verisi alınamadı, bu turda hacim filtresi devre dışı: {e}")
            apply_volume_filter = False

    # Parse ve filtrele
    rows = []
    for item in data:
        try:
            symbol = item.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            if symbol in getattr(SETTINGS, "SYMBOL_BLACKLIST", ()):
                continue
            if symbol in getattr(SETTINGS, "NON_STANDARD_SYMBOLS", ()):
                continue

            rate = _safe_float(item.get("lastFundingRate"), None)
            mark_price = _safe_float(item.get("markPrice"), None)
            index_price = _safe_float(item.get("indexPrice"), None)

            if rate is None or mark_price is None or index_price is None:
                continue

            if apply_volume_filter:
                quote_volume = volumes_map.get(symbol, 0.0)
                if quote_volume < min_quote_vol:
                    continue

            spread = (mark_price - index_price) / index_price * 100.0 if index_price != 0 else 0.0

            rows.append(
                {
                    "symbol": symbol,
                    "rate": rate,  # ondalık
                    "mark_price": mark_price,
                    "index_price": index_price,
                    "spread_pct": spread,
                }
            )
        except Exception:
            continue

    if len(rows) < 10:
        log.warning("Geçerli satır sayısı çok düşük, işlem iptal edildi")
        with _PULSE_LOCK:
            cached = _LAST_SUCCESS_DATA
        if cached is not None:
            return _build_report(cached, state, stale=True, change_eps=change_eps)
        return None

    payload = {
        "rows": rows,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _PULSE_LOCK:
        _LAST_SUCCESS_DATA = payload

    return _build_report(payload, state, stale=False, change_eps=change_eps)


def _build_report(payload: Dict[str, Any], state: Dict[str, Any], stale: bool = False, change_eps: float = 1e-6) -> Optional[Dict[str, Any]]:
    rows = payload.get("rows", [])
    if not rows:
        return None

    rates = [r["rate"] for r in rows]
    spreads = [r["spread_pct"] for r in rows]

    # Karşılaştırma için ESKİ last_report'u persist'ten ÖNCE yakala
    prev_last_report = dict(state.get("last_report", {})) if isinstance(state.get("last_report"), dict) else {}

    median_funding = _compute_median(rates)
    mean_funding = _compute_mean(rates)

    extreme_pct = getattr(SETTINGS, "FUNDING_PULSE_EXTREME_PCT", 0.05)
    extreme_thresh = extreme_pct / 100.0  # ondalık

    extreme_pos = [r for r in rows if r["rate"] > extreme_thresh]
    extreme_neg = [r for r in rows if r["rate"] < -extreme_thresh]

    total = len(rows)
    extreme_pos_cnt = len(extreme_pos)
    extreme_neg_cnt = len(extreme_neg)
    extreme_pos_ratio = (extreme_pos_cnt / total * 100.0) if total > 0 else 0.0
    extreme_neg_ratio = (extreme_neg_cnt / total * 100.0) if total > 0 else 0.0

    top_n = getattr(SETTINGS, "FUNDING_PULSE_TOP_N", 5)
    # Tüm rows'tan seç (eşik aşmasa bile anlamlı)
    top_pos = sorted(rows, key=lambda x: x["rate"], reverse=True)[:top_n]
    top_neg = sorted(rows, key=lambda x: x["rate"])[:top_n]

    median_spread = _compute_median(spreads)
    mean_spread = _compute_mean(spreads)

    # Önceki ölçüm yönü
    prev_median = state.get("median_funding")
    direction = "sabit"
    if _is_valid_number(prev_median):
        prev_m = float(prev_median)
        if median_funding > prev_m + change_eps:
            direction = "artıyor"
        elif median_funding < prev_m - change_eps:
            direction = "azalıyor"
        else:
            direction = "sabit"

    report = {
        "median_funding": median_funding,
        "mean_funding": mean_funding,
        "extreme_pos_cnt": extreme_pos_cnt,
        "extreme_neg_cnt": extreme_neg_cnt,
        "extreme_pos_ratio": extreme_pos_ratio,
        "extreme_neg_ratio": extreme_neg_ratio,
        "extreme_thresh_pct": extreme_pct,
        "top_pos": top_pos,
        "top_neg": top_neg,
        "median_spread_pct": median_spread,
        "mean_spread_pct": mean_spread,
        "direction": direction,
        "total_symbols": total,
        "stale": stale,
        "ts": payload.get("ts"),
        "prev_last_report": prev_last_report,
    }

    # State güncelle
    new_state = dict(state)
    new_state["median_funding"] = median_funding
    new_state["last_report"] = {
        "median_funding": median_funding,
        "mean_funding": mean_funding,
        "direction": direction,
        "ts": report["ts"],
    }
    if not stale:
        new_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    # stale modda updated_at güncellenmez

    # State'i kaydet (atomic) - lock kapsamı genişlet
    try:
        with _PULSE_LOCK:
            pulse_state = _load_pulse_state()
            pulse_state.update(new_state)
            _save_pulse_state(pulse_state)
    except Exception as e:
        log.warning(f"Pulse state persist hatası: {e}")

    report["state"] = new_state
    return report


def fmt_funding_pulse(report: Dict[str, Any]) -> str:
    if not report:
        return ""

    stale = report.get("stale", False)
    header = "🌐 *PİYASA FUNDING NABZI*"
    if stale:
        header = "⚠️ Veri güncellenemedi, son bilinen değerler:\n🌐 *PİYASA FUNDING NABZI*"

    median_f = report.get("median_funding", 0.0)
    mean_f = report.get("mean_funding", 0.0)
    direction = report.get("direction", "sabit")

    dir_emoji = "➡️"
    if direction == "artıyor":
        dir_emoji = "📈"
    elif direction == "azalıyor":
        dir_emoji = "📉"

    extreme_pct = report.get("extreme_thresh_pct", 0.05)
    extreme_pos_cnt = report.get("extreme_pos_cnt", 0)
    extreme_neg_cnt = report.get("extreme_neg_cnt", 0)
    extreme_pos_ratio = report.get("extreme_pos_ratio", 0.0)
    extreme_neg_ratio = report.get("extreme_neg_ratio", 0.0)

    lines = []
    lines.append(header)
    lines.append(
        f"Medyan Funding: {fmt_pct(median_f * 100)}% | Ortalama: {fmt_pct(mean_f * 100)}%"
    )
    lines.append(f"Yön: {dir_emoji} {direction}")
    lines.append("")
    lines.append(f"Aşırı Pozitif (>{fmt_pct(extreme_pct)}%): {extreme_pos_cnt} sembol (%{fmt_pct(extreme_pos_ratio, 1)})")
    lines.append(f"Aşırı Negatif (<-{fmt_pct(extreme_pct)}%): {extreme_neg_cnt} sembol (%{fmt_pct(extreme_neg_ratio, 1)})")
    lines.append("")

    lines.append("🔥 En Isınmış (Long ağırlıklı):")
    top_pos = report.get("top_pos", [])
    if top_pos:
        for i, c in enumerate(top_pos, 1):
            lines.append(f"{i}. {c['symbol']} +{fmt_pct(c['rate'] * 100)}%")
    else:
        lines.append("1. -")

    lines.append("")
    lines.append("🧊 En Soğumuş (Short ağırlıklı):")
    top_neg = report.get("top_neg", [])
    if top_neg:
        for i, c in enumerate(top_neg, 1):
            lines.append(f"{i}. {c['symbol']} {fmt_pct(c['rate'] * 100)}%")
    else:
        lines.append("1. -")

    lines.append("")
    median_spread = report.get("median_spread_pct", 0.0)
    lines.append(f"📐 Mark-Index Spread Ortalaması: {fmt_pct(median_spread)}%")
    lines.append("[Not: Bu rapor piyasa genelindeki funding rate dağılımını özetler; tekil coin analizi için Funding Squeeze Radar sinyallerine bakın. Aşırı değerler tek başına alım/satım sinyali değildir.]")

    return "\n".join(lines)


def send_funding_pulse(report: Dict[str, Any]) -> None:
    if not report:
        return

    send_only_on_change = getattr(SETTINGS, "FUNDING_PULSE_SEND_ONLY_ON_CHANGE", True)
    change_eps = getattr(SETTINGS, "FUNDING_PULSE_CHANGE_EPS", 1e-6)

    if send_only_on_change and not report.get("stale", False):
        last = report.get("prev_last_report", {}) if isinstance(report.get("prev_last_report"), dict) else {}
        if last:
            same_median = abs(last.get("median_funding", 0) - report.get("median_funding", 0)) < change_eps
            same_direction = last.get("direction") == report.get("direction")
            if same_median and same_direction:
                log.info("Funding pulse: anlamlı değişiklik yok, mesaj atlanıyor")
                return

    try:
        msg = fmt_funding_pulse(report)
        send_telegram(msg)
    except Exception as e:
        log.warning(f"Funding pulse mesaj gönderimi hatası: {e}")


if __name__ == "__main__":
    log.info("Funding Pulse bağımsız test modu")
    state = _load_pulse_state()
    rep = run_funding_pulse_check(state)
    if rep:
        print(fmt_funding_pulse(rep))
    else:
        print("Rapor üretilemedi")
