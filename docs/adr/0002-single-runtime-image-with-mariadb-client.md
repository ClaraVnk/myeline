# ADR 0002 — Single runtime image carrying `mariadb-client`

**Status:** Accepted
**Date:** 2026-05-18
**Round:** Audit round 3 — bug C-L3

## Context

The runtime Docker image (`Dockerfile`) installs `mariadb-client`
(specifically `mariadb-dump`) on top of `python:3.13-slim`. This same
image is used for three compose services:

- `web` — handles HTTP requests
- `cron` — runs `supercronic` against `scripts/crontab`
- `worker` — runs `rq worker`

`mariadb-dump` is only ever invoked by `cron` (via
`app/cron/backup_databases.py`). Bundling it in the same image used
by `web` enlarges that container's attack surface: if `web` is
RCE'd, the attacker has `mariadb-dump` already on PATH and can dump
the central DB without bringing their own binary.

## Decision

Keep a single runtime image with `mariadb-client` for now.

## Considered alternatives

### (A) Split into `myeline-web` and `myeline-cron` images

Two Dockerfiles, two CI build jobs, two image tags, two
`requirements.lock` files (potentially), two SHA digests to pin in
compose. The image without `mariadb-client` is ~30 MB lighter; the
gain is real but binary.

**Why not now:** the CI build pipeline (cf. `.github/workflows/ci.yml`)
is currently single-image with cached layers and `--require-hashes`
locked deps. Splitting doubles the build matrix and the registry
storage cost (Scaleway + ghcr), and forces every Sprint deploying a
new app version to push two tags in lock-step. The CI ergonomics cost
exceeds the security gain at the current threat model.

### (B) Install `mariadb-client` at runtime in cron only

Have `cron`'s entrypoint `apt-get install mariadb-client` if absent.
Adds 10–20 s to cron container start, requires the cron image to keep
apt working (so can't switch to a `distroless` base later), and
breaks `--require-hashes` reproducibility.

**Why not:** worse trade-off.

### (C) Remove `mariadb-client` from the image and exec into the `mariadb` container

`app/cron/backup_databases.py:_dump_via_podman_exec` already implements
this fallback (used when `mariadb-dump` is absent on PATH). Removing
the host-side binary would make this the only path.

**Why not:** the podman-exec path requires the `socket-proxy` reachable
from `cron`, which is the case today, but it's a more complex code
path (race with mariadb container restart, harder to debug when it
breaks). The host-side dump remains the fast happy path; keeping it
in the image is a reliability call as much as a performance one.

## Residual risk accepted

- RCE in `web` → attacker has `mariadb-dump` on PATH and can exfil
  the central DB to anywhere they have egress. **Mitigations
  already in place:**
  - `web` has no access to the host Podman socket since round-1 / C5
  - `web` has no DB credentials in env that aren't already in app
    process memory (so `mariadb-dump` doesn't unlock anything that
    a full app-context RCE didn't already give)
  - DB user is scoped to the application DB only — root creds aren't
    in `web`'s env (audit round 2 verified)

## Revisit when

- The CI pipeline gets fully restructured (multi-image build is OK)
- The host moves to a distroless base (forces image split anyway)
- A new compliance requirement explicitly bans dump binaries on
  application containers

## Related

- Audit round 3 / C-L3 — original finding
- Audit round 2 / H9 — `mariadb-dump` password via env (already fixed)
- `app/cron/backup_databases.py` — both code paths (host-side and
  podman-exec) coexist already
