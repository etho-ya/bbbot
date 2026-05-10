import logging
from logging.handlers import RotatingFileHandler
import os
from app.core.config import settings

def setup_logger(name, log_file, level=None):
    if level is None:
        level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    from pythonjsonlogger import jsonlogger
    
    # Text formatter for console
    console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # JSON formatter for file
    json_formatter = jsonlogger.JsonFormatter('%(asctime)s %(name)s %(levelname)s %(message)s')
    
    # File handler (100MB x 30 = 3GB)
    file_handler = RotatingFileHandler(log_file, maxBytes=100*1024*1024, backupCount=30)
    file_handler.setFormatter(json_formatter)
    
    # Console handler for real-time debugging
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

logger = setup_logger("bot", "logs/bot.log")