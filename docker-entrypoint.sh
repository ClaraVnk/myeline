#!/bin/sh
set -e

# Wait for the database to accept connections before migrating.
# Only runs when DATABASE_URL points to MySQL/MariaDB.
if echo "${DATABASE_URL:-}" | grep -q "mysql"; then
  echo "[entrypoint] Attente de la base de données..."
  MAX=30
  N=0
  until python - <<'PYEOF' 2>/dev/null
import os, sys, re, pymysql
m = re.search(r'://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/([^?]+)', os.environ.get('DATABASE_URL', ''))
if not m: sys.exit(0)
u, p, h, port, db = m.groups()
try:
    c = pymysql.connect(host=h, port=int(port or 3306), user=u, password=p, database=db, connect_timeout=3)
    c.close()
except Exception:
    sys.exit(1)
PYEOF
  do
    N=$((N + 1))
    if [ "$N" -ge "$MAX" ]; then
      echo "[entrypoint] ERREUR : DB indisponible après $((MAX * 2))s. Abandon."
      exit 1
    fi
    echo "[entrypoint] DB pas encore prête ($N/$MAX), nouvel essai dans 2s..."
    sleep 2
  done
  echo "[entrypoint] Base de données prête."
fi

# If arguments are passed (e.g. "rq worker ..."), run them directly — skip
# migrations and scraper init which are web-only concerns.
if [ $# -gt 0 ]; then
  echo "[entrypoint] Démarrage : $*"
  exec "$@"
fi

echo "[entrypoint] Application des migrations..."
flask db upgrade

# Note : l'ancien bloc d'init scrapers (workers/scrapper_web.py +
# workers/bundle_mail.py) a été retiré — ces fichiers ont disparu lors
# du rebrand vers Synapse (commit 92f4c64). Les scrapers actuels
# tournent via cron `check_user_scrapers` (cf. scripts/crontab),
# pas au boot du container.

echo "[entrypoint] Démarrage de l'application..."
exec gunicorn --config app/gunicorn_config.py app.wsgi:application
