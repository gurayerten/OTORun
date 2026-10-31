"""
Merkezi logging modülü.
- Konsol: renkli, seviyeli, zaman damgalı
- Dosya: state/bot.log dosyasına kayıt (rotate ile)
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler


_LOG_DIR = "state"
_LOG_FILE = os.path.join(_LOG_DIR, "bot.log")
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3

# Windows'ta terminale unicode karakter basarken oluşan UnicodeEncodeError hatasını önlemek için stdout/stderr'i reconfigure et
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _ColorFormatter(logging.Formatter):
    """Terminalde renk destekli formatter."""
    COLORS = {
        logging.DEBUG: "\033[90m",      # gri
        logging.INFO: "\033[0m",        # varsayılan
        logging.WARNING: "\033[33m",    # sarı
        logging.ERROR: "\033[31m",      # kırmızı
        logging.CRITICAL: "\033[1;31m", # kalın kırmızı
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        record.msg = f"{color}{record.msg}{self.RESET}"
        return super().format(record)


class SafeRotatingFileHandler(RotatingFileHandler):
    """Windows'ta os.rename hatasını (WinError 32 / PermissionError) önleyen güvenli Handler."""

    def rotate(self, source, dest):
        if not os.path.exists(source):
            return
        import time
        import shutil
        for attempt in range(5):
            try:
                if os.path.exists(dest):
                    os.remove(dest)
                os.rename(source, dest)
                return
            except (PermissionError, OSError):
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    try:
                        shutil.copy2(source, dest)
                        with open(source, "w", encoding="utf-8") as f:
                            f.truncate(0)
                    except Exception:
                        pass

    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError):
            # Dosya o anda başka işlemce kilitliyse rollover'ı bir sonraki tura ertele
            pass


def get_logger(name: str = "bot") -> logging.Logger:
    """Projenin her yerinden çağrılabilir tek noktadan logger döner."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # zaten yapılandırılmış

    logger.setLevel(logging.DEBUG)

    # --- Konsol handler ---
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_ColorFormatter("%(asctime)s │ %(message)s", datefmt="%H:%M:%S"))

    # --- Dosya handler ---
    os.makedirs(_LOG_DIR, exist_ok=True)
    fh = SafeRotatingFileHandler(_LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger
