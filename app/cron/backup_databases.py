"""Cron: backup_databases
Schedule: daily at 02:30
Purpose: snapshot the MariaDB and the ChromaDB to data/backups/<date>/.

Restore protocol — see docs/BACKUP.md.
"""
import os
import shutil
import subprocess  # noqa: S404 — invoked with fixed arg lists, not user input
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from app.utils.logger import get_logger
from app.utils.time_utils import utcnow


CRON_SCHEDULE = "30 2 * * *"
CRON_DESCRIPTION = "Daily backup of DB + ChromaDB"

logger = get_logger("backup_databases", subfolder="cron")

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKUP_DIR = _ROOT / "data" / "backups"
_CHROMA_DIR = _ROOT / "data" / "chroma_db"
_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))


def _dump_central_mariadb(target: Path) -> bool:
    """mysqldump the central DB into target. Reads creds from env."""
    db_url = os.getenv("DATABASE_URL", "")
    # Format: mysql+pymysql://user:pass@host:port/dbname
    try:
        from urllib.parse import urlparse
        parsed = urlparse(db_url.replace("mysql+pymysql://", "mysql://"))
        user = parsed.username
        password = parsed.password
        host = parsed.hostname
        port = str(parsed.port or 3306)
        database = (parsed.path or "/").lstrip("/")
        if not all([user, password, host, database]):
            logger.warning("Central DB URL missing fields — skipping dump")
            return False
    except Exception as exc:
        logger.error("Cannot parse DATABASE_URL: %s", exc)
        return False

    try:
        with target.open("wb") as f:
            # Use `mariadb-dump` (pas `mysqldump`) :
            # - `mysqldump` client v9+ (MySQL 8.4+) exige TLS par défaut et
            #   plante avec "TLS/SSL error: SSL is required, but the server
            #   does not support it" contre MariaDB 10.11 image officielle
            #   (le serveur ne propose pas TLS sans config explicite).
            #   Résultat : dump à 0 byte, backups silencieusement vides.
            # - `mariadb-dump` est le binaire natif MariaDB, pas de default
            #   TLS, drop-in replacement compatible mysqldump.
            # On force aussi --ssl=0 explicitement par ceinture+bretelles.
            cmd = [
                "mariadb-dump",
                "-h", host, "-P", port,
                "-u", user,
                "--ssl=0",
                "--single-transaction", "--routines", "--triggers",
                database,
            ]
            # Bug H9 — l'ancien code passait f"-p{password}" en argv. Le
            # password apparaissait dans `ps auxf`, /proc/<pid>/cmdline,
            # tout journal de processus. N'importe quel process local
            # lisait le password root DB pendant la fenêtre du backup.
            # Fix : passer le password via la variable d'env MYSQL_PWD
            # (convention native mysqldump/mariadb-dump, pas dans argv).
            dump_env = {**os.environ, "MYSQL_PWD": password}
            r = subprocess.run(  # noqa: S603
                cmd, stdout=f, stderr=subprocess.PIPE,
                timeout=600, env=dump_env,
            )
        if r.returncode != 0:
            logger.error("mariadb-dump failed (rc=%s): %s", r.returncode, r.stderr.decode()[:400])
            # Bug H1 (audit round 2) — sans cet unlink, un dump partiel
            # (souvent vide ou tronqué) restait sur disque et était
            # ramassé par `_push_offhost` ; le backup off-host devenait
            # un fichier corrompu présenté comme "backup of record".
            target.unlink(missing_ok=True)
            return False
        return True
    except FileNotFoundError:
        logger.error(
            "mariadb-dump binary not found — install the MariaDB client in "
            "the backup environment to enable DB dumps",
        )
        target.unlink(missing_ok=True)
        return False
    except Exception as exc:
        logger.error("Central dump error: %s", exc)
        target.unlink(missing_ok=True)
        return False


def _backup_chroma(target: Path) -> bool:
    """tar.gz the ChromaDB persistent dir."""
    if not _CHROMA_DIR.exists():
        logger.info("No ChromaDB dir at %s — skipping", _CHROMA_DIR)
        return True
    try:
        with tarfile.open(target, "w:gz") as tar:
            tar.add(_CHROMA_DIR, arcname="chroma_db")
        return True
    except Exception as exc:
        logger.error("ChromaDB tar failed: %s", exc)
        return False


def _rotate_old_backups() -> int:
    """Delete backup directories older than _RETENTION_DAYS. Returns count."""
    if not _BACKUP_DIR.exists():
        return 0
    cutoff = utcnow() - timedelta(days=_RETENTION_DAYS)
    removed = 0
    for entry in _BACKUP_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            # Directory names are YYYY-MM-DD — parse and compare.
            d = datetime.strptime(entry.name, "%Y-%m-%d")
            if d.replace(tzinfo=cutoff.tzinfo) < cutoff:
                shutil.rmtree(entry)
                removed += 1
                logger.info("Rotated old backup %s", entry.name)
        except ValueError:
            continue  # Not a date-named dir, leave alone
    return removed


def run() -> dict:
    """Main entry — runs the DB + ChromaDB backup passes + rotation."""
    today = utcnow().date().isoformat()
    out_dir = _BACKUP_DIR / today
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {"date": today, "central_db": False, "chroma": False}

    summary["central_db"] = _dump_central_mariadb(out_dir / "central_db.sql")
    summary["chroma"] = _backup_chroma(out_dir / "chroma_db.tar.gz")

    rotated = _rotate_old_backups()
    summary["rotated"] = rotated

    # Bug H2 (audit round 2) — chiffrement optionnel avant push off-host.
    # Sans, central_db.sql (emails, audit_log) atterrit
    # en clair sur le remote rclone (souvent un Drive perso). Si
    # BACKUP_ENCRYPTION_PASSPHRASE est configuré, on chiffre tous les
    # fichiers du dossier via gpg symmetric AES256 et on supprime les
    # versions en clair. Le restore = gpg --decrypt avec la même passphrase.
    summary["encrypted"] = _encrypt_backup_dir(out_dir)

    # Off-host sync — copy today's directory to the configured rclone
    # remote (Google Drive, S3, etc.). No-op when BACKUP_REMOTE isn't
    # set, so dev / small deployments keep backups local-only.
    summary["offhost"] = _push_offhost(out_dir, today)

    logger.info(
        "Backup complete date=%s central=%s chroma=%s rotated=%d offhost=%s",
        today, summary["central_db"], summary["chroma"],
        rotated, summary["offhost"],
    )
    return summary


def _encrypt_backup_dir(out_dir: Path) -> str:
    """Bug H2 — chiffre tous les fichiers du dossier avec gpg symmetric.

    Activé via BACKUP_ENCRYPTION_PASSPHRASE. La passphrase passe par
    stdin (jamais argv → invisible dans `ps`). Cipher AES256, format
    `.gpg` à côté du fichier original (qui est ensuite supprimé).
    Retourne :
      - "skipped" : pas de passphrase configurée
      - "ok" : tout chiffré, originaux supprimés
      - "error: ..." : échec
    """
    passphrase = os.getenv("BACKUP_ENCRYPTION_PASSPHRASE", "").strip()
    if not passphrase:
        logger.warning(
            "BACKUP_ENCRYPTION_PASSPHRASE non configuré — backup poussé EN CLAIR "
            "sur le remote rclone (PII, audit_log). "
            "Configurer un passphrase pour Art. 32 RGPD.",
        )
        return "skipped"
    try:
        for entry in out_dir.rglob("*"):
            if entry.is_dir() or entry.suffix == ".gpg":
                continue
            encrypted = entry.with_suffix(entry.suffix + ".gpg")
            cmd = [
                "gpg", "--batch", "--yes", "--symmetric",
                "--cipher-algo", "AES256",
                "--passphrase-fd", "0",
                "-o", str(encrypted),
                str(entry),
            ]
            r = subprocess.run(  # noqa: S603 — fixed args
                cmd, input=passphrase.encode(),
                capture_output=True, timeout=600,
            )
            if r.returncode != 0:
                logger.error(
                    "gpg encryption failed for %s: %s",
                    entry, r.stderr.decode(errors="replace")[:400],
                )
                return f"error: gpg rc={r.returncode}"
            # Supprimer l'original UNIQUEMENT après succès du chiffrement.
            entry.unlink()
        return "ok"
    except FileNotFoundError:
        logger.error("gpg binary missing — backup non chiffré sur disque/remote")
        return "error: gpg binary missing"
    except Exception as exc:
        logger.error("Backup encryption error: %s", exc)
        return f"error: {exc}"


def _push_offhost(out_dir: Path, today: str) -> str:
    """rclone copy out_dir → <BACKUP_REMOTE>/<today>/.

    Returns one of: "skipped" (no remote configured), "ok",
    or an error string. The remote name + path comes from env so
    the same image can target Google Drive, S3, OneDrive, etc.
    without code changes.

    Configure rclone once on the host (`rclone config`), then
    bind-mount the resulting config file into the cron container at
    /home/appuser/.config/rclone/rclone.conf — see docs/BACKUP.md.
    """
    remote = os.getenv("BACKUP_REMOTE", "").strip()
    if not remote:
        return "skipped"
    target = f"{remote.rstrip('/')}/{today}/"
    try:
        # --transfers limits parallel uploads; --tpslimit caps Drive
        # API rate so a multi-GB run doesn't trip Google's quota.
        cmd = [
            "rclone", "copy", str(out_dir), target,
            "--transfers", "4",
            "--tpslimit", "10",
            "--retries", "3",
            "--low-level-retries", "10",
            "--log-level", "INFO",
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=3600)  # noqa: S603 — fixed args
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")[-400:]
            logger.error("rclone copy failed: %s", err)
            return f"error: {err.splitlines()[-1] if err else 'unknown'}"
        logger.info("Off-host backup uploaded → %s", target)
        return "ok"
    except FileNotFoundError:
        return "error: rclone binary missing"
    except Exception as exc:
        logger.error("Off-host backup error: %s", exc)
        return f"error: {exc}"
