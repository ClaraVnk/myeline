# Backup & Restore

## What gets backed up

The daily `backup_databases` cron snapshots three things into
`data/backups/<YYYY-MM-DD>/`:

| Artefact | File | How |
|---|---|---|
| Central MariaDB | `central_db.sql` | `mysqldump --single-transaction --routines --triggers` |
| Central ChromaDB | `chroma_db.tar.gz` | `tarfile.open(...)` over `data/chroma_db/` |
| Each tenant's volumes | `tenant_<slug>/{db,uploads,data,logs}.tar.gz` | One-shot Alpine container tars the named Podman volume read-only |

Tenants are archived as **raw volume contents** rather than SQL dumps
because the central DB does not store tenant DB credentials. Restoring
a tenant therefore means re-creating the named volumes and untarring
the archives back in, *not* loading SQL.

## Schedule

Cron entry: `30 2 * * *` (02:30 UTC daily). Configurable from the admin
cron dashboard (`/admin/cron`). Run on demand with:

```bash
podman exec myeline_web_1 flask cron backup_databases
```

## Retention

Backup directories older than `BACKUP_RETENTION_DAYS` (default 30) are
deleted at the end of each successful run. Override with:

```
BACKUP_RETENTION_DAYS=90
```

## Restore — central MariaDB

```bash
# 1. Stop the central web/worker so they don't write during restore.
podman-compose -f docker-compose.dev.yml stop web worker

# 2. Drop & recreate the schema (or restore into a fresh DB).
podman exec -i myeline_mariadb_1 mariadb -umyeline_user -pmyeline_password myeline \
  -e "DROP DATABASE myeline; CREATE DATABASE myeline;"

# 3. Pipe the dump back in.
cat data/backups/2026-05-05/central_db.sql | \
  podman exec -i myeline_mariadb_1 mariadb -umyeline_user -pmyeline_password myeline

# 4. Restart.
podman-compose -f docker-compose.dev.yml start web worker
```

## Restore — central ChromaDB

```bash
# Stop the web container that holds the persistent client open.
podman-compose -f docker-compose.dev.yml stop web worker

# Replace the directory (back up the current one first if you want a
# rollback option).
rm -rf data/chroma_db
mkdir -p data/chroma_db
tar xzf data/backups/2026-05-05/chroma_db.tar.gz -C data/

podman-compose -f docker-compose.dev.yml start web worker
```

## Restore — a tenant stack

```bash
SLUG=acme

# 1. Stop the tenant containers.
for c in mariadb_$SLUG redis_$SLUG web_$SLUG cron_$SLUG worker_$SLUG; do
  podman stop "$c" 2>/dev/null || true
done

# 2. Recreate the empty volumes (force=true wipes whatever's there).
for kind in db uploads data logs; do
  podman volume rm -f enterprise_${SLUG}_${kind}
  podman volume create enterprise_${SLUG}_${kind}
done

# 3. Untar each archive into the matching volume via a throwaway container.
for kind in db uploads data logs; do
  podman run --rm \
    -v "enterprise_${SLUG}_${kind}:/dest" \
    -v "$(pwd)/data/backups/2026-05-05/tenant_${SLUG}:/src:ro" \
    docker.io/library/alpine:latest \
    sh -c "cd /dest && tar xzf /src/${kind}.tar.gz"
done

# 4. Start the tenant stack again — provisioning_service can re-create
#    the containers; the volumes are now populated.
```

## Off-host sync to Google Drive (or any rclone remote)

Set `BACKUP_REMOTE` in the environment of the `cron` service to the
rclone remote name + path. After the local backup completes, the cron
runs `rclone copy data/backups/<date>/ <BACKUP_REMOTE>/<date>/` so the
nightly snapshot is replicated off the host.

### One-time setup (Google Drive — 5 TB on Google One is plenty)

```bash
# On the host (NOT inside a container) — interactive OAuth dance.
rclone config

# Pick: n (new remote)
# Name: gdrive
# Storage: 18 (Google Drive)
# client_id: <leave empty for default> (or your own GCP OAuth client for higher quotas)
# client_secret: <leave empty>
# scope: 1 (full access)
# Auto-config: y → opens a browser, click Allow
# Configure as a team drive: n
# Result is saved to ~/.config/rclone/rclone.conf

# Verify the remote works:
rclone lsd gdrive:
```

Then point Myeline at it:

```bash
# In .env on the prod host:
BACKUP_REMOTE=gdrive:myeline-backups
RCLONE_CONFIG_PATH=/home/<user>/.config/rclone/rclone.conf
```

The compose service mounts the config file read-only inside the cron
container. Test with a manual run:

```bash
podman exec myeline_cron_1 flask run-cron backup_databases
# Watch the logs — you should see "Off-host backup uploaded → gdrive:…"
```

### Other backends

rclone supports 70+ providers. Same pattern: configure the remote
once on the host, set `BACKUP_REMOTE` to the remote name + path.

| Backend | Example BACKUP_REMOTE |
|---|---|
| AWS S3 | `s3-paris:myeline-prod-backups` |
| Scaleway Object Storage | `scaleway:myeline-backups/prod` |
| OVH Object Storage | `ovh:myeline-backups` |
| OneDrive | `onedrive:myeline-backups` |
| Backblaze B2 | `b2:myeline-backups` |
| MinIO (self-hosted) | `minio-internal:myeline` |

For a fully air-gapped sovereign deployment, point `rclone` at an
internal MinIO bucket on a separate physical host so the backup
survives a full primary-host loss without going to a cloud provider.

## What's not backed up (yet)

- **Encryption of the off-host artefact.** rclone uploads as-is.
  For sensitive deployments, layer rclone's `crypt` backend on top:
  configure two remotes (`gdrive-raw` then `gdrive` as crypt over
  it) and use the crypt one as `BACKUP_REMOTE`. Files become
  unreadable to anyone without the encryption password — including
  Google.
- **Redis state.** Treated as ephemeral cache + queue; losing it on
  restore is fine — queued jobs replay or expire.
- **Off-host audit log archive.** `archive_audit_log` covers that;
  configure with `AUDIT_S3_BUCKET` (independent of `BACKUP_REMOTE`).
