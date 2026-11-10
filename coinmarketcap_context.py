"""Low-frequency global spot-market context from CoinMarketCap."""
import json
import os
import shutil
import time
from typing import Any, Dict, Optional

import requests

from config import SETTINGS
from logger import get_logger
from telegram_notify import send_telegram

log = get_logger("coinmarketcap")
CMC_URL = "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest"
CMC_LISTINGS_URL = "https://pro-api.coinmarketcap.com/v3/cryptocurrency/listings/latest"
STATE_FILE = os.path.join("state", "coinmarketcap_context.json")
DISCOVERY_STATE_FILE = os.path.join("state", "coinmarketcap_discovery.json")


def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
    try:
        os.replace(temp, STATE_FILE)
    except OSError:
        shutil.copy2(temp, STATE_FILE)
        os.remove(temp)


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def run_cmc_context_check(state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not getattr(SETTINGS, "CMC_ENABLED", True):
        return None
    api_key = os.environ.get("CMC_API_KEY", "").strip()
    if not api_key:
        log.info("CoinMarketCap devre dışı: CMC_API_KEY tanımlı değil")
        return None
    previous = dict(state if state is not None else _load_state())
    interval_seconds = max(15, int(getattr(SETTINGS, "CMC_INTERVAL_MIN", 360))) * 60
    saved_at = _number(previous.get("saved_at"))
    if saved_at is not None and time.time() - saved_at < interval_seconds:
        return None
    try:
        response = requests.get(
            CMC_URL,
            headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"},
            params={"convert": "USD"},
            timeout=max(1, int(getattr(SETTINGS, "CMC_REQUEST_TIMEOUT", 12))),
        )
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status", {}) if isinstance(payload, dict) else {}
        if int(status.get("error_code", 0) or 0) != 0:
            # Never include response bodies: some upstream errors can echo request details.
            raise ValueError("CMC returned an API error")
        data = payload.get("data")
        quote = data.get("quote", {}).get("USD", {}) if isinstance(data, dict) else {}
        if not isinstance(quote, dict):
            raise ValueError("CMC response is missing global USD metrics")
        metrics = {
            "market_cap": _number(quote.get("total_market_cap")),
            "market_cap_change_24h": _number(quote.get("total_market_cap_yesterday_percentage_change")),
            "volume_24h": _number(quote.get("total_volume_24h")),
            "volume_change_24h": _number(quote.get("total_volume_24h_yesterday_percentage_change")),
            "btc_dominance": _number(data.get("btc_dominance")),
            "eth_dominance": _number(data.get("eth_dominance")),
            "updated_at": quote.get("last_updated") or data.get("last_updated"),
        }
        if metrics["market_cap"] is None or metrics["btc_dominance"] is None:
            raise ValueError("CMC global metrics are incomplete")
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        log.warning("CoinMarketCap global piyasa verisi alınamadı (%s)", type(exc).__name__)
        return None

    old_cap = _number(previous.get("market_cap"))
    old_btc = _number(previous.get("btc_dominance"))
    cap_move = ((metrics["market_cap"] / old_cap) - 1) * 100 if old_cap else None
    btc_move = metrics["btc_dominance"] - old_btc if old_btc is not None else None
    cap_threshold = max(0.1, float(getattr(SETTINGS, "CMC_MARKET_CAP_ALERT_PCT", 3.0)))
    btc_threshold = max(0.05, float(getattr(SETTINGS, "CMC_BTC_DOMINANCE_ALERT_PCT", 0.75)))
    report = {
        **metrics,
        "change_since_last_pct": cap_move,
        "btc_dominance_change_since_last_pct": btc_move,
        "notable_change": (cap_move is not None and abs(cap_move) >= cap_threshold)
        or (btc_move is not None and abs(btc_move) >= btc_threshold),
        "first_snapshot": old_cap is None,
    }
    _save_state({**metrics, "saved_at": int(time.time())})
    if report["first_snapshot"] or report["notable_change"]:
        return report
    log.info("CoinMarketCap global metriklerinde bildirim eşiğini aşan değişim yok")
    return None


def _money(value: Optional[float]) -> str:
    if value is None:
        return "veri yok"
    if value >= 1e12:
        return f"${value / 1e12:,.2f} trilyon"
    if value >= 1e9:
        return f"${value / 1e9:,.2f} milyar"
    return f"${value:,.0f}"


def fmt_cmc_context(report: Dict[str, Any]) -> str:
    lines = ["🌐 GLOBAL KRİPTO PİYASA ÖZETİ", "",
             f"Toplam piyasa değeri: {_money(report.get('market_cap'))}"]
    if report.get("market_cap_change_24h") is not None:
        lines.append(f"24s piyasa değeri değişimi: {report['market_cap_change_24h']:+.2f}%")
    lines.append(f"24s spot hacim: {_money(report.get('volume_24h'))}")
    if report.get("volume_change_24h") is not None:
        lines.append(f"24s hacim değişimi: {report['volume_change_24h']:+.2f}%")
    lines.append(f"BTC dominansı: {report['btc_dominance']:.2f}%")
    if report.get("eth_dominance") is not None:
        lines.append(f"ETH dominansı: {report['eth_dominance']:.2f}%")
    if report.get("change_since_last_pct") is not None:
        lines.append(f"Önceki ölçüme göre piyasa değeri: {report['change_since_last_pct']:+.2f}%")
    if report.get("btc_dominance_change_since_last_pct") is not None:
        lines.append(f"BTC dominansı değişimi: {report['btc_dominance_change_since_last_pct']:+.2f} puan")
    updated = report.get("updated_at")
    if updated:
        lines.extend(["", f"Veri zamanı: {updated}"])
    lines.append("Kaynak: CoinMarketCap Global Metrics. Spot piyasa bağlamıdır; tek başına işlem sinyali değildir.")
    return "\n".join(lines)


def send_cmc_context(report: Dict[str, Any]) -> None:
    send_telegram(fmt_cmc_context(report))


def _get_usdt_perpetual_bases() -> set:
    """Read the Binance futures contract universe for safe ticker intersection."""
    from exchange import get_exchange_info

    info = get_exchange_info()
    return {
        str(item.get("baseAsset", "")).upper()
        for item in info.get("symbols", [])
        if item.get("quoteAsset") == "USDT"
        and item.get("contractType") == "PERPETUAL"
        and item.get("status") == "TRADING"
        and item.get("baseAsset")
    }


def run_cmc_discovery_check() -> Optional[Dict[str, Any]]:
    """Find liquid CMC-ranked assets that also have a Binance USDT perpetual."""
    if not getattr(SETTINGS, "CMC_ENABLED", True) or not getattr(SETTINGS, "CMC_DISCOVERY_ENABLED", True):
        return None
    api_key = os.environ.get("CMC_API_KEY", "").strip()
    if not api_key:
        return None
    previous = _load_json_state(DISCOVERY_STATE_FILE)
    interval = max(15, int(getattr(SETTINGS, "CMC_INTERVAL_MIN", 360))) * 60
    if time.time() - (_number(previous.get("saved_at")) or 0) < interval:
        return None
    try:
        response = requests.get(
            CMC_LISTINGS_URL,
            headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"},
            params={
                "start": 1,
                "limit": min(500, max(100, int(getattr(SETTINGS, "CMC_DISCOVERY_LIMIT", 500)))),
                "convert": "USD",
                "sort": "market_cap",
                "sort_dir": "desc",
                "aux": "cmc_rank,num_market_pairs",
            },
            timeout=max(1, int(getattr(SETTINGS, "CMC_REQUEST_TIMEOUT", 12))),
        )
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status", {}) if isinstance(payload, dict) else {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if int(status.get("error_code", 0) or 0) != 0 or not isinstance(data, list):
            raise ValueError("CMC listings response is invalid")
        contract_bases = _get_usdt_perpetual_bases()
        # Exclude symbols duplicated within the fetched CMC universe to reduce false matches.
        symbol_counts: Dict[str, int] = {}
        for asset in data:
            if isinstance(asset, dict):
                symbol = str(asset.get("symbol", "")).upper()
                if symbol:
                    symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        min_volume = max(0, float(getattr(SETTINGS, "CMC_DISCOVERY_MIN_VOLUME_USD", 5_000_000)))
        candidates = []
        for asset in data:
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("symbol", "")).upper()
            quotes = asset.get("quote")
            quote = quotes[0] if isinstance(quotes, list) and quotes else {}
            change = _number(quote.get("percent_change_24h")) if isinstance(quote, dict) else None
            volume = _number(quote.get("volume_24h")) if isinstance(quote, dict) else None
            rank = asset.get("cmc_rank")
            if (symbol not in contract_bases or symbol_counts.get(symbol) != 1
                    or change is None or volume is None or volume < min_volume):
                continue
            candidates.append({
                "symbol": symbol,
                "name": str(asset.get("name", symbol)),
                "rank": int(rank) if isinstance(rank, (int, float)) else None,
                "change_24h": change,
                "volume_24h": volume,
                "market_cap": _number(quote.get("market_cap")) if isinstance(quote, dict) else None,
                "cmc_id": asset.get("id"),
            })
        candidates.sort(key=lambda row: row["change_24h"], reverse=True)
        movers = candidates[:5] + sorted(candidates, key=lambda row: row["change_24h"])[:5]
        # De-duplicate if the same asset appears in both tails on a small universe.
        movers_by_id = {row["cmc_id"]: row for row in movers}
        movers = list(movers_by_id.values())
    except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError) as exc:
        log.warning("CoinMarketCap keşif verisi alınamadı (%s)", type(exc).__name__)
        return None

    prior_movers = previous.get("movers", [])
    prior_by_id = {str(item.get("cmc_id")): item for item in prior_movers if isinstance(item, dict)}
    threshold = max(1.0, float(getattr(SETTINGS, "CMC_DISCOVERY_CHANGE_THRESHOLD_PCT", 5.0)))
    notable = not prior_by_id
    for row in movers:
        old = prior_by_id.get(str(row["cmc_id"]))
        if old is None or abs(row["change_24h"] - float(old.get("change_24h", 0))) >= threshold:
            notable = True
    _save_json_state(DISCOVERY_STATE_FILE, {"saved_at": int(time.time()), "movers": movers})
    if not notable or not movers:
        return None
    return {"movers": movers, "first_snapshot": not prior_by_id}


def _load_json_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
    try:
        os.replace(temp, path)
    except OSError:
        shutil.copy2(temp, path)
        os.remove(temp)


def fmt_cmc_discovery(report: Dict[str, Any]) -> str:
    gainers = [row for row in report["movers"] if row["change_24h"] >= 0]
    losers = [row for row in report["movers"] if row["change_24h"] < 0]
    lines = ["🔎 CMC SPOT HAREKET RADARI", "",
             "İlk 500 piyasa değeri içinden, Binance USDT perpetual'da da bulunan varlıklar:"]
    for title, rows in (("Yükselenler", gainers), ("Düşenler", losers)):
        if rows:
            lines.extend(["", f"{title}:"])
            for row in rows:
                lines.append(f"• {row['symbol']} (CMC #{row['rank'] or '—'}): {row['change_24h']:+.2f}% | spot hacim {_money(row['volume_24h'])}")
    lines.extend(["", "Kaynak: CoinMarketCap spot piyasası. Binance USDT perpetual eşleşmesi bulundu; bu liste tek başına işlem sinyali değildir."])
    return "\n".join(lines)


def send_cmc_discovery(report: Dict[str, Any]) -> None:
    send_telegram(fmt_cmc_discovery(report))
