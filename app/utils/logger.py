import gzip
import logging
import os
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv


def set_level_logger(env):
    """Set logging level based on environment.

    Args:
        env (str): Environment name ('development' or other)

    Returns:
        int: Logging level constant (DEBUG for development, INFO otherwise)
    """
    if env == "development":
        return logging.DEBUG
    else:
        return logging.INFO


# Affichage en console si dev
def console_handler(formatter):
    """Create and configure a console logging handler.

    Args:
        formatter (logging.Formatter): Formatter to apply to the handler

    Returns:
        logging.StreamHandler: Configured console handler
    """
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    return handler


class NullHandler(logging.Handler):
    def emit(self, record):
        pass


class GzipRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that compresses rotated files with gzip."""

    def rotation_filename(self, default_name):
        return default_name + ".gz"

    def rotate(self, source, dest):
        with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(source)


def get_logger(script_name, subfolder=None):
    """Initialise un logger avec rotation automatique.

    Args:
        script_name (str): Nom du fichier de log sans extension
        subfolder (str, optional): Dossier optionnel dans logs/ (ex: 'scrapers', 'cron', etc.)

    Returns:
        logging.Logger: Logger configuré et fonctionnel (jamais None)
    """

    load_dotenv()
    env = os.getenv("FLASK_ENV", "production")
    log_to_stdout = os.getenv("LOG_TO_STDOUT", "false").lower() == "true"
    logger = logging.getLogger(script_name)
    log_level = set_level_logger(env)
    logger.setLevel(log_level)
    
    # Empêcher la propagation vers le logger parent pour éviter les doublons
    logger.propagate = False

    # Vérification pour ne pas dupliquer les handlers
    if not logger.handlers:
        try:
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            formatter.converter = time.localtime

            # Si LOG_TO_STDOUT est activé (Docker), on log uniquement sur stdout
            if log_to_stdout:
                logger.addHandler(console_handler(formatter))
            else:
                # Sinon, on utilise le système de fichiers avec rotation
                base_dir = Path(__file__).resolve().parent.parent.parent
                logs_dir = base_dir / "logs"
                if subfolder:
                    logs_dir = logs_dir / subfolder

                logs_dir.mkdir(parents=True, exist_ok=True)
                log_file = logs_dir / f"{script_name}.log"

                file_handler = GzipRotatingFileHandler(
                    log_file,
                    maxBytes=1_000_000,
                    backupCount=3,
                )
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

                # En développement, on ajoute aussi la console
                if env == "development":
                    logger.addHandler(console_handler(formatter))
        except Exception as e:
            # Fallback: use stderr so errors are never silently dropped
            fallback = logging.StreamHandler()
            fallback.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            logger.addHandler(fallback)
            logger.error(
                f"[LOGGER] Impossible de configurer le logger '{script_name}': {e}"
            )

    return logger
