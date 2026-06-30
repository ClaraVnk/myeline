# Configuration Gunicorn pour production
import multiprocessing


# Liaison
bind = "0.0.0.0:5000"

# Workers — capped at 9 to avoid saturating the DB connection pool
# (each worker holds its own pool; formula gives 5 on 2-core, 9 on 4-core+)
workers = min(multiprocessing.cpu_count() * 2 + 1, 9)
worker_class = "sync"
worker_connections = 1000
# 120 s covers the longest Mistral/RAG calls (internal HTTP timeouts are 60-120 s);
# 300 s was too long and would leave stuck workers blocking resources for 5 min.
timeout = 120
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 50
preload_app = True
daemon = False

# Logs pour Docker - sortie standard pour compatibilité avec les systèmes de logs Docker
accesslog = "-"  # stdout
errorlog = "-"  # stderr
loglevel = "info"

capture_output = True

# Format des logs d'accès détaillé
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s ' '"%(f)s" "%(a)s" %(D)s %(p)s'
)

# Optimisations
worker_tmp_dir = "/dev/shm"  # nosec B108 - Standard shared memory location
tmp_upload_dir = None

# Sécurité - géré par systemd
# user = "flask-app"
# group = "flask-app"
