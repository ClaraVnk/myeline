import os

from app.utils.logger import get_logger


logger = get_logger("admin_services", subfolder="services")


def read_file_if_safe(filename, base_dir):
    filepath = os.path.abspath(os.path.join(os.path.abspath(base_dir), filename))
    logger.debug(f"Tentative de lecture du fichier: {filepath}")
    if not filepath.startswith(os.path.abspath(base_dir) + os.sep):
        logger.error(f"Tentative d'accès non autorisé au fichier: {filepath}")
        raise PermissionError("Fichier non autorisé")

    try:
        with open(filepath, "r", encoding="utf-8") as file:
            content = file.read()
            logger.info(f"Lecture réussie du fichier: {filepath}")
            return content
    except Exception as e:
        logger.error(f"Erreur lors de la lecture du fichier {filepath}: {e}")
        raise
