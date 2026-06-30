# Self-hosting Myeline

This guide covers a complete single-user self-host install: prerequisites,
configuration, key generation, choosing an LLM, and registering your own
OAuth apps for the cloud connectors.

For an overview of features, see the [README](../README.md).

---

## 1. Prerequisites

- **Podman 4.6+** with `podman-compose` (Docker + Compose also work). Rootless
  Podman is fine.
- A host with at least **4 GB RAM** for the API-backed setup; **8 GB minimum
  (16 GB recommended)** if you run a local LLM via Ollama.
- The compose stack provides everything else as services:
  - **MariaDB 10.11** — relational data (account, sources, history…).
    PostgreSQL also works if you point `DATABASE_URL` at it, but the bundled
    stack ships MariaDB.
  - **Redis 7** — rate limiting, caching, and the background job queue.
  - **Ollama** — optional local embedding/LLM runtime.

A reverse proxy (Caddy, Nginx, Traefik…) terminating TLS in front of
`localhost:5000` is recommended for any internet-facing deployment.

---

## 2. Clone and configure

```bash
git clone https://github.com/ClaraVnk/myeline.git ~/Projects/myeline
cd ~/Projects/myeline
cp .env.example .env
```

Edit `.env`. The variables that matter for a self-host install are below;
the rest of `.env.example` relates to the hosted edition and can be left at
its defaults or empty.

### Essential variables

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing key. **Required.** |
| `SECURITY_PASSWORD_SALT` | Salt for password hashing. **Required** — never change after first use (invalidates all passwords). |
| `CLOUD_TOKEN_KEY` | Master key for encrypting sensitive DB columns. **Required.** See [§3](#3-generating-keys). |
| `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD` | MariaDB passwords. Generate strong values. |
| `MYSQL_DATABASE`, `MYSQL_USER` | Must match `DATABASE_URL`. Defaults: `myeline` / `myeline_user`. |
| `DATABASE_URL` / `SQLALCHEMY_DATABASE_URI` | Connection string, e.g. `mysql+pymysql://myeline_user:<pw>@mariadb:3306/myeline`. |
| `REDIS_URL` | Defaults to `redis://redis:6379/0` (the bundled service). |
| `EXTERNAL_URL` | Public base URL of your instance — used to build links in emails/digests. |
| `OAUTH_REDIRECT_BASE_URL` | Base URL OAuth providers redirect back to; usually equal to `EXTERNAL_URL`. |
| `LOG_LEVEL`, `LOG_TO_STDOUT` | Logging. `LOG_TO_STDOUT=true` is handy for containers. |

### LLM variables

Pick **one** of the two approaches in [§4](#4-choosing-an-llm):

| Variable | Purpose |
|---|---|
| `MISTRAL_API_KEY` | Optional platform-wide Mistral key (used if you don't set a per-account BYOK key). |
| `MISTRAL_LLM_MODEL`, `MISTRAL_AUX_MODEL` | Synthesis / auxiliary models (default `mistral-medium-latest` / `mistral-small-latest`). |
| `EMBED_PROVIDER` | `mistral` (API) or `ollama` (local). |
| `OLLAMA_URL` | Local Ollama endpoint, e.g. `http://ollama:11434`. |
| `OLLAMA_EMBED_MODEL`, `OLLAMA_LLM_MODEL` | Local models, e.g. `bge-m3` / `mistral-nemo`. |

### Email (optional)

Transactional email (account confirmation, 2FA codes, digests) goes through
[Brevo](https://www.brevo.com/). If you don't configure it:

- Set `BREVO_API_KEY=dummy` to **log** all emails (including 2FA codes) to
  the container logs instead of sending. Read codes with
  `podman-compose logs web`.
- Or set a real `BREVO_API_KEY` + `MAIL_DEFAULT_SENDER` to send for real.

---

## 3. Generating keys

Generate each secret with Python and paste the output into `.env`:

```bash
# SECRET_KEY and SECURITY_PASSWORD_SALT
python3 -c "import secrets; print(secrets.token_hex(32))"

# CLOUD_TOKEN_KEY — master encryption key
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`CLOUD_TOKEN_KEY` is a passphrase from which the app derives a **Fernet**
key (PBKDF2-HMAC-SHA256). It encrypts, at rest: cloud OAuth tokens, your
BYOK API keys, TOTP secrets, query history, saved queries, and watch-alert
keywords.

> **Back it up.** If you lose `CLOUD_TOKEN_KEY`, every encrypted column
> becomes permanently unreadable. Store it outside the repo (a password
> manager, `sops`, a Podman secret, a sealed envelope…). To rotate it later,
> use `flask rotate-cloud-token-key --old <old> --new <new>` (re-encrypts in
> place) **before** swapping the value in `.env`.

---

## 4. Choosing an LLM

### Option A — Bring your own API key (BYOK)

1. Start the stack and create your account (see [§6](#6-first-run)).
2. Go to **Settings → AI provider**, pick a provider (Mistral, OpenAI,
   Anthropic Claude, or Google Gemini) and paste your API key. It is stored
   encrypted.
3. Indexing requires an embedding-capable provider — Mistral or OpenAI cover
   both synthesis and embedding. Claude/Gemini keys can answer but not embed,
   so pair them with `EMBED_PROVIDER=mistral` (+ an embedding key) or with
   local Ollama embeddings.

Alternatively, set `MISTRAL_API_KEY` in `.env` as a default used when no
per-account key is configured.

### Option B — Fully local with Ollama (no third party)

Keep the `ollama` service from the compose file and set:

```bash
EMBED_PROVIDER=ollama
OLLAMA_URL=http://ollama:11434
OLLAMA_EMBED_MODEL=bge-m3
OLLAMA_LLM_MODEL=mistral-nemo   # or any model you pull
```

The Ollama init script pulls the configured models on first boot. Both
embedding and synthesis then run locally — nothing leaves the host. Plan for
**8 GB RAM minimum** (16 GB for `mistral-nemo`); a GPU is recommended for
acceptable latency.

Any **OpenAI-compatible** endpoint (vLLM, LM Studio, llama.cpp server) can be
used in place of Ollama by configuring the OpenAI provider against your local
base URL.

---

## 5. Bring-your-own credentials (connectors)

Each cloud connector authenticates with **your own** credentials. The
OAuth-based connectors (Google Drive, OneDrive, Dropbox, kDrive) require you
to register a provider app and put its credentials in `.env`; their redirect
URI is always:

```
https://<your-domain>/user/cloud/<provider>/callback
```

The token-based connectors (Notion, S3, WebDAV, Zotero) need no app and no
`.env` entry — credentials are entered per connection in the UI. After
editing `.env`, restart the stack.

### Google Drive — service account (no CASA)

Google Drive uses a **GCP service account** (server-to-server). Users share a
Drive folder with the service-account email; Myeline indexes it in the
background. There is no per-user OAuth flow and **no CASA security
verification** to go through.

1. [console.cloud.google.com](https://console.cloud.google.com) → create or
   select a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create Service Account** (no IAM role
   needed — it only ever sees folders users explicitly share).
4. Open the service account → **Keys → Add key → Create new key → JSON**.
5. Note the service-account email
   (`name@<project>.iam.gserviceaccount.com`) — users add it as *Viewer* on
   the folders they want indexed.
6. Paste the JSON, compacted to one line (`jq -c . key.json`), into:

```bash
GOOGLE_SERVICE_ACCOUNT_CREDENTIALS='{"type":"service_account",...}'
```

The JSON contains an RSA private key — treat it as a critical secret.

### OneDrive — Azure / Microsoft Entra app

1. [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID → App
   registrations → New registration**.
2. Redirect URI (**Web**):
   `https://<your-domain>/user/cloud/onedrive/callback`.
3. **Certificates & secrets → New client secret** → copy the *Value*.
4. **API permissions → Microsoft Graph → Delegated** → add `Files.Read` and
   `offline_access`.

```bash
ONEDRIVE_CLIENT_ID=...
ONEDRIVE_CLIENT_SECRET=...
```

### Dropbox

1. [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) →
   **Create app** → **Scoped access** → *Full Dropbox* or *App folder*.
2. **Settings → OAuth 2 → Redirect URIs**:
   `https://<your-domain>/user/cloud/dropbox/callback`.
3. **Permissions** → enable `files.metadata.read` and `files.content.read`.

```bash
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
```

### kDrive (Infomaniak)

1. [developer.infomaniak.com](https://developer.infomaniak.com) → **Create
   application**.
2. Redirect URI: `https://<your-domain>/user/cloud/kdrive/callback`.

```bash
KDRIVE_CLIENT_ID=...
KDRIVE_CLIENT_SECRET=...
```

### Notion / S3 / WebDAV / Zotero — no OAuth app

These need no app registration and no `.env` entry. Credentials are entered
**per connection** in the UI:

- **Notion**: an **internal integration token**. Create an internal
  integration at
  [notion.so/my-integrations](https://www.notion.so/my-integrations), then
  share the pages/databases you want indexed with it (*Share → Invite →
  select your integration*). Paste the token into
  **Cloud connections → Notion**.

- **S3 / S3-compatible**: access key, secret key, endpoint, region, bucket.
  Works with AWS, Scaleway, OVH, Cloudflare R2, MinIO, etc.
- **WebDAV**: server URL + username/password.
- **Zotero**: a personal API key (and your Zotero user/group ID), generated
  at [zotero.org/settings/keys](https://www.zotero.org/settings/keys).

All connector credentials and tokens are encrypted at rest with
`CLOUD_TOKEN_KEY`.

---

## 6. First run

```bash
# Build and start
podman-compose -f docker-compose.yml up -d --build

# Apply database migrations (if not run automatically by the entrypoint)
podman-compose -f docker-compose.yml exec web flask db upgrade

# Create your account
podman-compose -f docker-compose.yml exec web flask create-admin
```

Open <http://localhost:5000>, log in, and complete the mandatory TOTP 2FA
enrolment. Then add your sources and configure the AI provider under
**Settings**.

> **Local build note:** `docker-compose.yml` references a published image by
> default. To build from your local checkout, ensure the `web` / `cron` /
> `worker` services use `build: .` (uncomment it / comment the `image:` line),
> or use the development compose file `docker-compose.dev.yml` which builds
> locally out of the box.

### Health checks

```bash
curl http://localhost:5000/healthz   # liveness
curl http://localhost:5000/health    # readiness (DB, Redis, LLM…)
```

---

## 7. Persistent data & backups

| Path | Content |
|---|---|
| `./data/mysql/` | MariaDB database |
| `./data/chroma_db/` | ChromaDB vector index |
| `./data/ollama/` | Ollama model cache |
| `./uploads/` | Your uploaded documents |
| `./logs/` | Application logs |

Back up `./data` (especially `mysql/` and `chroma_db/`), `./uploads`, and —
critically — your `CLOUD_TOKEN_KEY`. A database dump without the key leaves
all encrypted columns unreadable.
