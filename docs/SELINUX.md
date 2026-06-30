# SELinux on Rocky / RHEL hosts

This project assumes SELinux is **Enforcing** with the `targeted`
policy. Bind-mounts in `docker-compose.yml` use the `:z` suffix so
Podman relabels them with the shared `container_file_t` type.

## Known compromise

The `socket-proxy` service runs with `security_opt: [label=disable]`
(see `docker-compose.yml`). This **disables type enforcement for that
single container** because the Podman socket file's type
(`container_runtime_t`) is not assignable to the HAProxy process type
that the `tecnativa/docker-socket-proxy` image uses. Without the opt-out,
HAProxy can't connect to the socket and every API call returns 503.

The rest of the system stays Enforcing — only the proxy container is
exempted.

## Bug C-M1 (audit round 3) — recommended hardening

The `label=disable` blanket exemption is broader than necessary. A
proper fix is to write a dedicated SELinux policy that:

1. Creates a custom type for the proxy process (e.g. `socket_proxy_t`)
   that is allowed to connect to `container_runtime_t` sockets.
2. Restores the policy on container start.
3. Drops the `label=disable` opt-out from the compose file.

### Why this is deferred

Writing a custom SELinux policy module requires:
- `policycoreutils-devel` on the host
- `audit2allow` analysis of the actual transition the proxy needs
- Module compile + install + persistence across host reboots

This is outside the scope of a typical security sprint and demands
dedicated SELinux expertise. The current `label=disable` keeps the
attack surface bounded to a container that:
- Is on a network-isolated `provisioner-net` (audit round 3 C1)
- Exposes only whitelisted Podman API endpoints (HAProxy ACLs)
- Has no shell, no compilers, no curl

Risk accepted at this time.

## Common SELinux ops

```bash
# Check current mode
getenforce

# Recent AVC denials
sudo ausearch -m AVC -ts recent

# Human-readable explanation
sudo journalctl -t setroubleshoot

# Suggested allow rules (don't apply blindly)
sudo audit2allow -a

# Restore default labels on a path
sudo restorecon -Rv /path/to/dir

# Boolean flip (e.g. allow container access to home)
sudo setsebool -P container_use_cephfs on
```

## Project conventions

- **Never** propose `setenforce 0` as a fix — banned by CLAUDE.md §23.
- Bind-mounts use `:z` (shared) or `:Z` (private) — see compose files.
- Project ports above 5000 are not in the default SELinux port type
  list; if HAProxy / Podman need to listen on an exotic port, use
  `semanage port -a -t http_port_t -p tcp <port>`.
