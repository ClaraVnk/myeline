# syntax=docker/dockerfile:1.6
#
# Multi-stage build (Trivy hardening, audit 2026-05-08):
#
# ┌─────────────────────────────────────────────────────────────────────┐
# │ Stage 1 (builder): full toolchain — gcc, g++, *-dev headers, uv     │
# │   • compiles wheels for cryptography, lxml, pillow, argon2-cffi…    │
# │   • produces a `/install` site-packages tree we copy in stage 2     │
# │                                                                     │
# │ Stage 2 (runtime): minimal Debian slim — runtime *.so only          │
# │   • zero gcc / g++ / linux-libc-dev (drops 48 HIGH false-positives) │
# │   • zero -dev headers (drops a dozen more HIGH per Trivy)           │
# │   • supercronic, rclone, curl, mysqldump kept (used at runtime)     │
# └─────────────────────────────────────────────────────────────────────┘
#
# Python 3.13 — current stable. 3.9 reached EOL Oct 2025; staying on it
# means no more security fixes upstream. The `requirements.lock` file
# in this repo is compiled against 3.13 with hashes, so the build is
# bit-for-bit reproducible (any tampered wheel triggers a hash mismatch
# at install time, before the code ever runs).
#
# Why python:3.13-slim (Debian Trixie) and not Rocky/UBI?
# Despite the host running Rocky 10, the container OS is deliberately
# Debian for *portability across customer deployments*: a sovereign
# install can happen on a Debian / Ubuntu / Rocky / Alma host, and the
# container should run identically on all of them. Debian-slim images
# are also an order of magnitude smaller than UBI / Rocky-minimal once
# Python 3.13 is added (Rocky 10 ships 3.12 by default). The host's
# SELinux / firewalld / patch cadence stays Rocky; the container's
# patches come from Debian-security and are pulled in via the monthly
# rebuild cron (see scripts/crontab and docs/SECURITY.md).

# ─── Stage 1: builder ──────────────────────────────────────────────────
FROM python:3.13-slim@sha256:af5bd286051a06b38587d30a8638958f4a2f38381aa80fe859c740af3411bd4d AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Compilers + dev headers — stay in stage 1, never reach the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libjpeg-dev \
    libpng-dev \
    libwebp-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt requirements.lock ./

# uv + locked deps with hash check. --target makes pip install into a
# specific dir we'll COPY --from=builder later.
RUN pip install --no-cache-dir uv && \
    uv pip install --target=/install --no-cache --require-hashes -r requirements.lock

# ─── Stage 2: runtime ──────────────────────────────────────────────────
FROM python:3.13-slim@sha256:af5bd286051a06b38587d30a8638958f4a2f38381aa80fe859c740af3411bd4d AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    FLASK_ENV=production \
    LOG_TO_STDOUT=true \
    PYTHONPATH=/usr/local/lib/python3.13/site-packages

# Runtime *.so only — the matching -dev packages stay in stage 1.
# mariadb-client provides `mariadb-dump` (used by backup_databases cron).
# IMPORTANT : on installe mariadb-client et PAS default-mysql-client :
# le binaire `mysqldump` du paquet MySQL v9+ (MySQL 8.4+) exige TLS par
# défaut, ce qui plante contre l'image MariaDB officielle 10.11 qui ne
# propose pas TLS — symptôme : dumps à 0 byte, backups silencieusement
# vides. `mariadb-dump` est le binaire natif MariaDB, drop-in
# replacement, sans cette régression.
# curl is kept for rclone install + supercronic download below; it is
# also used by the in-cluster healthcheck script.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    libjpeg62-turbo \
    libwebp7 \
    curl \
    mariadb-client \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Supercronic — a cron implementation designed for containers
# (logs to stdout, no PID-1 quirks, validates the crontab at boot).
# v0.2.45 is the first release compiled with Go 1.24, fixing the 10
# stdlib CVEs Trivy flagged on v0.2.32 (incl. CVE-2025-68121 CRITICAL).
ENV SUPERCRONIC_VERSION=v0.2.45
# Pinned SHA256 of supercronic-linux-amd64 for SUPERCRONIC_VERSION above.
# Bump both together; the build fails loudly if the download doesn't match.
ENV SUPERCRONIC_SHA256=bb6da5af8d5547c9a5cbb4cf58d9f5541f0433df2188bfe4f1a54b04ad253db6
RUN curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
        -o /usr/local/bin/supercronic \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic \
    && /usr/local/bin/supercronic -test /dev/null || (rm -f /usr/local/bin/supercronic && exit 1)

# rclone — used by the backup_databases cron to optionally sync the
# nightly backup directory off-host (Google Drive, S3, OneDrive, etc.).
# Direct binary download (instead of the upstream `curl | bash`) because
# we need the build to FAIL LOUDLY if rclone is missing — the previous
# `|| true` fallback silently shipped images without rclone, which broke
# off-host sync without any signal at build time.
#
# IMPORTANT : ARG (build-time only), pas ENV. Une variable d'env nommée
# `RCLONE_VERSION` qui persisterait dans l'image runtime serait
# interprétée par le binaire rclone comme un override du flag
# `--version` (qui est un bool), provoquant à chaque appel :
#   strconv.ParseBool: parsing "v1.71.2": invalid syntax
# ARG ne persiste pas dans l'image finale, c'est l'isolation qu'on veut.
ARG RCLONE_VERSION=v1.71.2
# Pinned SHA256 of the rclone release zip, per architecture. From
# https://downloads.rclone.org/${RCLONE_VERSION}/SHA256SUMS — bump with the
# version. The zip is verified before extraction; mismatch fails the build.
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
         amd64) RCLONE_SHA256=ab9fa5877cee91c64fdfd61a27028a458cf618b39259e5c371dc2ec34a12e415 ;; \
         arm64) RCLONE_SHA256=e2e2efc7ed143026352d60216ef0d46d3fa4fe9d647eff1bd929e6fea498e6f1 ;; \
         *) echo "unsupported architecture: $ARCH" && exit 1 ;; \
       esac \
    && curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${ARCH}.zip" \
        -o /tmp/rclone.zip \
    && echo "${RCLONE_SHA256}  /tmp/rclone.zip" | sha256sum -c - \
    && apt-get update && apt-get install -y --no-install-recommends unzip \
    && unzip -j /tmp/rclone.zip "rclone-${RCLONE_VERSION}-linux-${ARCH}/rclone" -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/rclone \
    && /usr/local/bin/rclone version | head -1 \
    && rm -f /tmp/rclone.zip \
    && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Copy the pre-built site-packages tree from stage 1.
COPY --from=builder /install /usr/local/lib/python3.13/site-packages

# Console scripts that pip put under /install/bin (flask, gunicorn, rq…)
COPY --from=builder /install/bin/ /usr/local/bin/

WORKDIR /app

# Copy application code
COPY . .

# Create non-root user and set up directories
RUN adduser --disabled-password --gecos '' appuser && \
    mkdir -p uploads/students_cards logs && \
    chown -R appuser:appuser /app && \
    chmod -R 755 /app/uploads

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
USER appuser

# Expose port
EXPOSE 5000

# Health check — hit /healthz (liveness uniquement, 200 garanti dès que
# Flask répond) plutôt que /health qui fait des roundtrips DB/Redis/Ollama
# et peut renvoyer 503 sur des ralentissements transitoires de
# dépendances. --max-time 5 borne curl pour éviter qu'il hang si Flask
# est unresponsive. start-period 20s : laisser le temps à gunicorn de
# démarrer et aux migrations Alembic de tourner au premier boot.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS -o /dev/null --max-time 5 http://localhost:5000/healthz || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
