# Security notes

Operational security posture and known, accepted trade-offs for Myeline
(`veille-bibliographique`). Most hardening lives in the code, the Dockerfile and
the compose files; this document records the deliberate exceptions so they are
not mistaken for oversights.

## Image / supply chain

- Base images (`python:3.13-slim`, `mariadb:10.11`, `redis:7-alpine`) are pinned
  by `@sha256` digest in the Dockerfile and compose files. A monthly rebuild
  cron refreshes them to pick up upstream security patches.
- `supercronic` and `rclone` are downloaded at build time and verified against a
  pinned SHA256 before use — bump the version and the hash together.

## Container runtime

- All services run with `no-new-privileges` and `cap_drop: ALL`. `mariadb` and
  `redis` add back only the capabilities their entrypoints need to drop from
  root (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID` / `SETGID`,
  `SETUID`). The application tier (`web`/`cron`/`worker`) runs with a read-only
  root filesystem; all writes go to the mounted `uploads`/`data`/`logs` volumes
  or to tmpfs (`/tmp`, `/dev/shm`).
- The `web` container port is bound to `127.0.0.1` only — TLS is terminated by
  the Pangolin tunnel / Caddy / the operator's reverse proxy, which reaches the
  app over the internal network.

## Deferred / accepted trade-offs

### socket-proxy `EXEC=1` (enterprise provisioning)

`docker-compose.yml` runs a `tecnativa/docker-socket-proxy` with `EXEC=1` and
`POST=1` so the central instance can seed and manage per-tenant enterprise
stacks. This is the most powerful capability in the system: an RCE in `web` or
`cron` could reach the proxy and exec into other containers.

It is **not** removed because the enterprise provisioning flow depends on it
(`app/services/provisioning_service.py`). Mitigations in place: the proxy is
only reachable over the internal `provisioner-net`, the socket is mounted
read-only into the proxy alone, and the proxy enforces an HAProxy ACL
whitelist. **Action:** monitor/audit `/exec` calls through the proxy; revisit
removing `EXEC` if the provisioning seed can be reworked to avoid container
exec.

### Legacy token KDF (raw SHA-256)

`app/utils/crypto.py` keeps a decrypt-only fallback (`_fernet_legacy`, raw
SHA-256 of the key) for credentials encrypted before the PBKDF2 migration. New
writes always use PBKDF2 (260k iterations); the legacy path only reads old
ciphertext and logs a warning prompting a re-save.

**Action:** remove `_fernet_legacy` once `flask rotate-cloud-token-key` has been
confirmed complete in production (no rows still readable only via the legacy
KDF). Until then it stays to avoid bricking pre-migration secrets.

## Reporting

Security issues: **hello@myeline.io**.
