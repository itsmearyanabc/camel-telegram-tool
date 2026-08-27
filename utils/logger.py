import logging
import os
import sys
from logging.handlers import RotatingFileHandler

def setup_logger():
    logger = logging.getLogger("ARMEDIAS")
    logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

    # Prevent duplicate handlers
    if not logger.handlers:
        # Rotating file log. Set LOG_TO_FILE=0 to keep the disk completely clean
        # and rely on journald (journalctl -u <service>) instead — the console
        # handler below still emits everything.
        if os.environ.get("LOG_TO_FILE", "1") != "0":
            log_dir = (os.environ.get("STATE_DIR") or ".").rstrip("/\\") + "/logs"
            os.makedirs(log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(
                os.path.join(log_dir, "bot.log"),
                maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
            file_handler.setFormatter(
                logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s'))
            logger.addHandler(file_handler)

        # Console Handler — force UTF-8 stream to avoid Windows cp1252 crashes
        try:
            utf8_stream = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)
        except Exception:
            utf8_stream = sys.stdout
        console_handler = logging.StreamHandler(stream=utf8_stream)
        console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(console_handler)

    return logger

logger = setup_logger()
