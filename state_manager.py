import json
import os
import shutil
import threading
import time
from datetime import datetime
from typing import Dict, List

from config import SETTINGS
from logger import get_logger

log = get_logger("state")


def _ensure_dir(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"JSON yükleme hatası ({path}): {e}. Varsayılan değer kullanılacak.")
        return default


def _save(path, data):
    _ensure_dir(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        
        # Windows işletim sisteminde geçici dosya kilitlenme (WinError 5/32) hatasına karşı yeniden deneme
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except (PermissionError, OSError):
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        shutil.copy2(tmp, path)
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception as e:
                        log.warning(f"JSON yazma uyarısı ({path}): {e}")
    except Exception as e:
        log.warning(f"JSON kaydetme hatası ({path}): {e}")


class StateManager:
    """Sembol başına tek açık pozisyon takip eder, kapanan işlemleri geçmişe
    yazar (state/trade_history.json) ve genel performans istatistiklerini
    (state/stats.json) tutar. Aynı bar için sinyali tekrar tekrar Telegram'a
    yollamayı önler.

    Diske JSON olarak yazılır; bot yeniden başlasa da pozisyonlar/istatistikler
    kaybolmaz.

    Thread-safe: tüm public metotlar RLock ile korunur."""

    def __init__(self):
        self._lock = threading.RLock()
        with self._lock:
            self.positions: Dict[str, dict] = _load(SETTINGS.STATE_FILE, {})
            self.alerted: Dict[str, str] = _load(SETTINGS.ALERTED_FILE, {})
            self.trade_history: List[dict] = _load(SETTINGS.TRADE_HISTORY_FILE, [])
            self.signal_log: List[dict] = _load(SETTINGS.SIGNAL_LOG_FILE, [])
            # Migrate legacy records missing the ``type`` field to ``trade_signal``
            migration_needed = False
            for rec in self.signal_log:
                if "type" not in rec:
                    rec["type"] = "trade_signal"
                    migration_needed = True
            if migration_needed:
                try:
                    _save(SETTINGS.SIGNAL_LOG_FILE, self.signal_log)
                    log.info("Signal log migration: added missing type='trade_signal' to legacy records")
                except Exception as e:
                    log.warning(f"Signal log migration failed: {e}")
            self.stats: dict = _load(
                SETTINGS.STATS_FILE,
                {
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "breakevens": 0,
                    "total_r_multiple": 0.0,
                },
            )
            # Eğer geçmiş varsa ama stats dosyası yoksa otomatik yeniden hesapla
            if self.trade_history and self.stats.get("total_trades", 0) == 0:
                self._recalculate_stats()
            # (Analiz) total_r_realized, total_r_multiple ile birebir aynı
            # değerle güncelleniyordu (ayrı bir metrik değildi) → kaldırıldı.
            # Eski stats.json dosyalarındaki kalıntı anahtar temizlenir.
            self.stats.pop("total_r_realized", None)

        if self.positions:
            log.info(f"📂 {len(self.positions)} açık pozisyon diskten yüklendi")
        if self.stats.get("total_trades", 0) > 0:
            st = self.stats
            wr = (st['wins'] / st['total_trades'] * 100) if st['total_trades'] > 0 else 0
            log.info(
                f"🏆 Geçmiş Performans: {st['total_trades']} İşlem │ "
                f"{st['wins']} WIN ({wr:.1f}%) │ {st['losses']} LOSS │ {st['breakevens']} BE │ "
                f"Toplam: {st['total_r_multiple']:+.2f}R"
            )

    def _recalculate_stats(self):
        """Geçmiş işlem listesinden genel performans istatistiklerini hesaplar."""
        total = len(self.trade_history)
        wins = sum(1 for t in self.trade_history if t.get("outcome") == "WIN" or t.get("win", False))
        be = sum(1 for t in self.trade_history if t.get("outcome") == "BREAKEVEN" or t.get("reason") == "BE")
        losses = sum(1 for t in self.trade_history if t.get("outcome") == "LOSS" or (not t.get("win", False) and t.get("reason") != "BE"))
        total_r = sum(float(t.get("r_multiple", 0.0)) for t in self.trade_history)

        self.stats = {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "breakevens": be,
            "total_r_multiple": round(total_r, 2),
        }

    @staticmethod
    def _extract_indicators(setup):
        """SweepSetup nesnesinden öğrenme sistemi için tüm gösterge değerlerini çıkarır."""
        if setup is None:
            return {}
        indicator_fields = [
            "adx_value", "di_plus", "di_minus", "supertrend_trend",
            "pmax_trend", "pmax_cross", "rsi_div", "market_regime",
            "confluence_score", "grade", "probability",
            "poc_price", "vah_price", "val_price",
            "linreg_osc", "swing_vwap", "swing_vwap_dir",
            "breakout_signal", "tsi_value", "tsi_signal",
            "multi_div_bull", "multi_div_bear",
            "whale_bull_absorption", "whale_bear_absorption", "whale_delta_dominance",
            "fvg_bull_near", "fvg_bear_near", "ob_bull_near", "ob_bear_near",
            "ema_cross", "ema_position", "engulfing",
            "lux_liq_sweep", "bos_signal", "stacked_ob_fvg",
            "ttm_squeeze", "ichimoku_trend", "cvd_trend",
            "cmf_value", "stoch_rsi",
            "ob_imbalance_ratio", "ob_favorable",
            "htf_counter_trend",
        ]
        indicators = {}
        for field in indicator_fields:
            val = getattr(setup, field, None)
            if val is not None:
                # bool/int/float/str dışı tipleri stringe çevir
                if isinstance(val, (bool, int, float, str)):
                    indicators[field] = val
                else:
                    indicators[field] = str(val)
        return indicators

    def _log_signal_context(self, symbol, plan, score, bar_time, setup):
        """Sinyal anındaki tüm gösterge değerlerini signal_log.json'a kaydeder."""
        indicators = self._extract_indicators(setup)
        # (Analiz) "type" alanı şema tutarlılığı içindir: log_whale_signal
        # "whale_alert" yazarken trade kayıtları da açıkça etiketlenir;
        # downstream analiz else-varsayımıyla karıştırma riski kalkar.
        record = {
            "type": "trade_signal",
            "symbol": symbol,
            "bar_time": str(bar_time),
            "engine": getattr(setup, "engine", "sweep"),
            "direction": "LONG" if plan.direction == 1 else "SHORT",
            "setup_type": getattr(setup, "setup_type", "Liq Sweep") if setup else "Liq Sweep",
            "score": round(score, 2),
            "grade": getattr(setup, "grade", "B") if setup else "B",
            "market_regime": getattr(setup, "market_regime", "TRANSITION") if setup else "TRANSITION",
            "entry": plan.entry,
            "sl": plan.sl,
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "tp3": plan.tp3,
            "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "indicators": indicators,
        }
        self.signal_log.append(record)

    def _persist(self):
        # Zaten lock altında çağrılır (RLock olduğu için sorun yok)
        _save(SETTINGS.STATE_FILE, self.positions)
        _save(SETTINGS.ALERTED_FILE, self.alerted)
        _save(SETTINGS.TRADE_HISTORY_FILE, self.trade_history)
        _save(SETTINGS.STATS_FILE, self.stats)
        _save(SETTINGS.SIGNAL_LOG_FILE, self.signal_log)

    def log_whale_signal(self, whale_info: dict):
        """Whale alert sinyalini signal_log.json'a kaydeder ve diske yazar.

        main.py tarafından her whale tespitinde çağrılır. StateManager
        dışından doğrudan signal_log.append() yapmak yerine bu metot
        kullanılmalıdır; çünkü:
          - Lock altında çalışır (thread-safe)
          - Append sonrası diske persist eder (bot çökerse veri kaybolmaz)

        Args:
            whale_info: check_whale_alert()'in döndürdüğü dict
        """
        with self._lock:
            record = {
                "symbol": whale_info.get("symbol", ""),
                "bar_time": whale_info.get("bar_time", ""),
                "type": "whale_alert",
                "direction": "LONG" if whale_info.get("direction", 0) == 1 else "SHORT",
                "buy_ratio": round(float(whale_info.get("buy_ratio", 0)), 4),
                "deviation": round(float(whale_info.get("deviation", 0)), 4),
                "deviation_15m": (
                    round(float(whale_info["deviation_15m"]), 4)
                    if whale_info.get("deviation_15m") is not None
                    else None
                ),
                "buy_volume_usd": round(float(whale_info.get("buy_volume_usd", 0)), 2),
                "sell_volume_usd": round(float(whale_info.get("sell_volume_usd", 0)), 2),
                "vol_spike": round(float(whale_info.get("vol_spike", 0)), 2),
                "quality_label": whale_info.get("quality_label", ""),
                "tf_confirmed": whale_info.get("tf_confirmed", True),
                "mark_price": whale_info.get("mark_price", 0),
                "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.signal_log.append(record)
            _save(SETTINGS.SIGNAL_LOG_FILE, self.signal_log)

    def has_open(self, symbol):
        with self._lock:
            return symbol in self.positions

    def already_alerted(self, symbol, bar_time):
        with self._lock:
            return self.alerted.get(symbol) == str(bar_time)

    def mark_alerted(self, symbol, bar_time):
        with self._lock:
            self.alerted[symbol] = str(bar_time)
            self._persist()

    def open_position(self, symbol, plan, score, bar_time, setup=None):
        with self._lock:
            setup_type = getattr(setup, "setup_type", "Liq Sweep") if setup else "Liq Sweep"
            grade = getattr(setup, "grade", "B") if setup else "B"
            indicators = self._extract_indicators(setup)

            self.positions[symbol] = {
                "direction": plan.direction,
                "entry": plan.entry,
                "sl": plan.sl,
                "tp1": plan.tp1,
                "tp2": plan.tp2,
                "tp3": plan.tp3,
                "score": score,
                "engine": getattr(setup, "engine", "sweep"),
                "opened_at": str(bar_time),
                "setup_type": setup_type,
                "grade": grade,
                "market_regime": getattr(setup, "market_regime", "TRANSITION") if setup else "TRANSITION",
                "indicators": indicators,
                "tp1_reached": False,
                "tp2_reached": False,
                "tp3_reached": False,
                "be_active": False,
                "extreme_price": plan.entry,
                "risk_distance": abs(plan.entry - plan.sl),
                "trailing_active": False,
            }
            # Sinyal bağlam logunu kaydet
            self._log_signal_context(symbol, plan, score, bar_time, setup)
            self._persist()
            side = "LONG" if plan.direction == 1 else "SHORT"
            log.debug(f"Pozisyon açıldı: {symbol} {side} @ {plan.entry:.4f} ({setup_type})")

    def update_on_bar(self, symbol, high, low, close=None):
        """Son kapanmış mumun high/low'una göre SL/TP/BE kontrolü yapar.
        Bildirilecek olayların listesini döner.

        close: opsiyonel mum kapanış fiyatı — time-stop değerlendirmesi için
        (main.py 1m/5m takipte son mumun close'unu geçirir).
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return []
            events = []
            d = pos["direction"]
            sl_hit = (low <= pos["sl"]) if d == 1 else (high >= pos["sl"])
            tp1_hit = (high >= pos["tp1"]) if d == 1 else (low <= pos["tp1"])
            tp2_hit = (high >= pos["tp2"]) if d == 1 else (low <= pos["tp2"])
            tp3_hit = (high >= pos["tp3"]) if d == 1 else (low <= pos["tp3"])

            if tp1_hit and not pos["tp1_reached"] and not sl_hit:
                pos["tp1_reached"] = True
                events.append({"type": "tp1", "symbol": symbol, "price": pos["tp1"]})
                if SETTINGS.USE_BREAK_EVEN and not pos["be_active"]:
                    # (Analiz) SL girişe değil entry ± BE_LEVEL_R×risk'e çekilir:
                    # TP1'e değip girişte kesilen 10+ trade deseni kırılır.
                    risk = pos.get("risk_distance", 0) or 0.0
                    lock = pos["entry"] + SETTINGS.BE_LEVEL_R * risk if d == 1 else pos["entry"] - SETTINGS.BE_LEVEL_R * risk
                    # Kilit seviyesi mevcut SL'den iyiyse uygula
                    if (d == 1 and lock > pos["sl"]) or (d == -1 and lock < pos["sl"]):
                        pos["sl"] = lock
                        pos["be_active"] = True
                        events.append({"type": "breakeven", "symbol": symbol, "price": lock})
            if tp2_hit and not pos["tp2_reached"] and not sl_hit:
                pos["tp2_reached"] = True
                events.append({"type": "tp2", "symbol": symbol, "price": pos["tp2"]})
                # TP2'de SL'yi TP1 seviyesine yükselt (kârı kilitle)
                new_sl_tp2 = pos["tp1"]
                if (d == 1 and new_sl_tp2 > pos["sl"]) or (d == -1 and new_sl_tp2 < pos["sl"]):
                    pos["sl"] = new_sl_tp2
                    events.append({"type": "sl_escalation", "symbol": symbol, "price": new_sl_tp2, "level": "TP1"})
            if tp3_hit and not pos["tp3_reached"] and not sl_hit:
                pos["tp3_reached"] = True
                events.append({"type": "tp3", "symbol": symbol, "price": pos["tp3"]})

            # --- Trailing Stop Logic ---
            if getattr(SETTINGS, "USE_TRAILING_STOP", False) and not sl_hit:
                r_dist = pos.get("risk_distance", 0)
                if r_dist > 0:
                    activation_dist = r_dist * getattr(SETTINGS, "TRAILING_STOP_ACTIVATION_R", 1.0)
                    trail_dist = r_dist * getattr(SETTINGS, "TRAILING_STOP_DISTANCE_R", 1.0)

                    if d == 1:  # LONG
                        if high > pos.get("extreme_price", pos["entry"]):
                            pos["extreme_price"] = high

                        if pos["extreme_price"] >= pos["entry"] + activation_dist:
                            if not pos.get("trailing_active", False):
                                pos["trailing_active"] = True
                                events.append({"type": "trailing_activated", "symbol": symbol, "price": pos["extreme_price"]})

                            new_sl = pos["extreme_price"] - trail_dist
                            if new_sl > pos["sl"]:
                                pos["sl"] = new_sl

                    else:  # SHORT
                        if low < pos.get("extreme_price", pos["entry"]):
                            pos["extreme_price"] = low

                        if pos["extreme_price"] <= pos["entry"] - activation_dist:
                            if not pos.get("trailing_active", False):
                                pos["trailing_active"] = True
                                events.append({"type": "trailing_activated", "symbol": symbol, "price": pos["extreme_price"]})

                            new_sl = pos["extreme_price"] + trail_dist
                            if new_sl < pos["sl"]:
                                pos["sl"] = new_sl

            # --- Pozisyon Kapanış Kontrolü (SL / TP3 / Time-Stop) ---
            exit_price = None
            reason = None
            if sl_hit:
                exit_price = pos["sl"]
                reason = "BE" if pos["be_active"] else "SL"
            elif tp3_hit:
                exit_price = pos["tp3"]
                reason = "TP3"
            elif close is not None:
                # (Analiz) Time-stop: THETA 525 bar, AIA 497 bar gibi ölü
                # pozisyonlar sermayeyi bağlayıp SL yiyordu.
                if self._hold_bars_exceeded(pos):
                    risk = pos.get("risk_distance", 0) or 0.0
                    r_now = (((close - pos["entry"]) * d) / risk) if risk > 0 else 0.0
                    if r_now < SETTINGS.TIME_STOP_MIN_R:
                        exit_price = float(close)
                        reason = "TIME"

            if exit_price is not None:
                r_multiple = self._r_multiple(pos, exit_price)

                # Sonuç sınıflandırması: gerçek R'den (eski 0/−1 yaklaşımı değil).
                # (Analiz) Eşik artık config'ten (BE_OUTCOME_THRESHOLD): BE/trailing
                # kilitli çıkışlar küçük pozitif R ile kapanabilir; bunların WIN
                # mi BREAKEVEN mı sayılacağı oradan ayarlanır.
                if abs(r_multiple) < SETTINGS.BE_OUTCOME_THRESHOLD and reason != "TP3":
                    win = False
                    outcome = "BREAKEVEN"
                    self.stats["breakevens"] = self.stats.get("breakevens", 0) + 1
                elif r_multiple > 0:
                    win = True
                    outcome = "WIN"
                    self.stats["wins"] = self.stats.get("wins", 0) + 1
                else:
                    win = False
                    outcome = "LOSS"
                    self.stats["losses"] = self.stats.get("losses", 0) + 1

                self.stats["total_trades"] = self.stats.get("total_trades", 0) + 1
                self.stats["total_r_multiple"] = round(self.stats.get("total_r_multiple", 0.0) + r_multiple, 2)

                # Detaylı geçmiş kaydı (gösterge verileri dahil)
                trade_record = {
                    "symbol": symbol,
                    "direction": "LONG" if d == 1 else "SHORT",
                    "entry": pos["entry"],
                    "close_price": exit_price,
                    "score": pos.get("score", 0),
                    "setup_type": pos.get("setup_type", "Liq Sweep"),
                    "grade": pos.get("grade", "B"),
                    "market_regime": pos.get("market_regime", "TRANSITION"),
                    "opened_at": pos.get("opened_at", ""),
                    "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": reason,
                    "outcome": outcome,
                    "win": win,
                    "r_multiple": r_multiple,
                    "r_realized": r_multiple,
                    "tp1_reached": pos.get("tp1_reached", False),
                    "tp2_reached": pos.get("tp2_reached", False),
                    "tp3_reached": pos.get("tp3_reached", False),
                    "indicators": pos.get("indicators", {}),
                }
                self.trade_history.append(trade_record)

                events.append({
                    "type": "close",
                    "symbol": symbol,
                    "win": win,
                    "r_multiple": r_multiple,
                    "reason": reason,
                    "outcome": outcome,
                })
                del self.positions[symbol]

            self._persist()
            return events

    def get_open_positions_summary(self):
        """Terminale yazdırmak için açık pozisyonların özetini döner."""
        with self._lock:
            return dict(self.positions)

    def get_performance_summary(self):
        """Genel başarım / performans istatistiklerini döner."""
        with self._lock:
            st = self.stats
            total = st.get("total_trades", 0)
            wins = st.get("wins", 0)
            losses = st.get("losses", 0)
            be = st.get("breakevens", 0)
            total_r = st.get("total_r_multiple", 0.0)

            win_rate = (wins / total * 100.0) if total > 0 else 0.0
            decisive_total = wins + losses
            win_rate_decisive = (wins / decisive_total * 100.0) if decisive_total > 0 else 0.0

            return {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "breakevens": be,
                "win_rate": round(win_rate, 1),
                "win_rate_decisive": round(win_rate_decisive, 1),
                "total_r_multiple": round(total_r, 2),
                "open_positions": len(self.positions),
            }

    _BAR_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900,
                    "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

    @staticmethod
    def _r_multiple(pos, exit_price):
        """Gerçek R-multiple: çıkış fiyatından hesaplanır (eski 0/−1 yaklaşımı
        değil). BE_LEVEL_R ve trailing kilitleri artık sonuca yansır."""
        risk = float(pos.get("risk_distance", 0) or 0.0)
        if risk <= 0:
            return 0.0
        return round(((exit_price - pos["entry"]) * pos["direction"]) / risk, 4)

    def _hold_bars_exceeded(self, pos):
        """Pozisyon MAX_HOLD_BARS'tan uzun süredir mi açık? (opened_at → now)"""
        try:
            opened = datetime.fromisoformat(str(pos.get("opened_at", "")))
        except (ValueError, TypeError):
            return False
        bar_sec = self._BAR_SECONDS.get(SETTINGS.INTERVAL, 300)
        elapsed_bars = (datetime.now() - opened).total_seconds() / bar_sec
        return elapsed_bars >= SETTINGS.MAX_HOLD_BARS
