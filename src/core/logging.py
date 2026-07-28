import os
import logging
from logging.handlers import TimedRotatingFileHandler


_LOG_DIR = os.getenv("LOG_DIR", "/var/log/florence")


def init_logging():
    log_dir = _LOG_DIR
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(module)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "florence.log"),
        when="midnight",
        interval=1,
        backupCount=90,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logging.info("Logging initialized - log dir: %s", log_dir)
