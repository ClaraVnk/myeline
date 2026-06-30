# Email — Architecture, Kill-Switches, and Send Inventory

This document describes how Myeline sends transactional and bulk emails,
how to keep dev / staging from leaking real messages, and where each
caller lives in the codebase.

## Provider

All transactional emails go through **Brevo** (formerly Sendinblue) via the
HTTP API at `https://api.brevo.com/v3/smtp/email`. There is no SMTP path —
the codebase has a single sender helper, `send_email_via_brevo_api()` in
[`app/utils/mailer.py`](../app/utils/mailer.py).

**Outbound `From:` identity** comes from two env vars:

| Var | Default | Purpose |
|---|---|---|
| `MAIL_DEFAULT_SENDER` | `hello@myeline.io` | RFC-5322 `From:` address |
| `MAIL_SENDER_NAME` | `Myeline` | display name |

The sending domain (`myeline.io` in production) **must** publish three DNS
records so DMARC-aware MTAs accept the messages:

| Record | Name | Value |
|---|---|---|
| SPF | `myeline.io.` | `"v=spf1 include:spf.brevo.com ~all"` |
| DKIM | `mail._domainkey.myeline.io.` (selector provided by Brevo) | `"k=rsa; p=…"` |
| DMARC | `_dmarc.myeline.io.` | `"v=DMARC1; p=none; rua=mailto:postmaster@myeline.io; aspf=r; adkim=r; pct=100"` |

Start DMARC at `p=none` for at least two weeks of observation, then move
to `p=quarantine`, then `p=reject`. Sudden volume on a fresh domain (e.g.
1000+ messages in an hour) gets soft-bounced even with valid records — the
domain reputation has to warm up.

## Kill-Switches

Every email goes through the same gate in `send_email_via_brevo_api()`.
Three switches in order of precedence:

| Switch | Effect | When to use |
|---|---|---|
| `MAIL_DRY_RUN=true` | **Always** logs the email instead of sending, regardless of `FLASK_ENV`. | Default on every dev box. Set in your `.env`. |
| `BREVO_API_KEY=dummy` | Always logs. | Unit / integration test fixtures. |
| `FLASK_ENV != production` (and `MAIL_FORCE_SEND` unset) | Logs by default. | Implicit safety net for any non-prod container. |
| `MAIL_FORCE_SEND=true` | Override the above so non-prod environments **do** deliver. | A staging environment that has to validate end-to-end delivery. |

Production: `FLASK_ENV=production` + `MAIL_DRY_RUN=false` (or unset) is the
only configuration that actually hits Brevo.

> **Why this exists.** The previous logic only logged when `FLASK_ENV !=
> production` AND a (different-named) opt-in flag was set. A dev box that
> ran `FLASK_ENV=production` for prod-like smoke tests therefore sent
> through Brevo on every iteration. Combined with cron retries and
> repeated Enterprise provisioning, this is how ~1400 real emails left the
> tenant in a single morning of testing.

## Send-Path Inventory

Every call site of `send_email_via_brevo_api()`:

| Caller | Trigger | Volume per trigger | Loop risk | Notes |
|---|---|---|---|---|
| `routes/auth.py:137` | Registration (sovereign mode only) | 1 / signup | Low | Confirmation email. Try/except — failure doesn't block flow. |
| `routes/auth.py:374` | Password reset | 1 / request | Low | Single send. |
| `routes/auth.py:456` | Resend confirmation | 1 / request | Low | Rate-limited 3/min. No dedup — repeat clicks repeat the email. |
| `routes/user.py:135` | Contact form | 1 / submission | Low | Routed to `hello@myeline.io`. |
| `routes/user_2fa.py:67` | 2FA email OTP | 1 / setup attempt | **Medium** | Called on `/2fa/verify` GET (line 218) — reloading the page resends. No GET-side rate limit. |
| `routes/user_tools.py:161` | Account deletion confirmation | 1 / deletion | Low | |
| `routes/user_tickets.py:29,51` | Support ticket open / reply | 1 / event | Low | Notifies `SUPPORT_NOTIFY_EMAIL` only (silent no-op without it). |
| `routes/payment.py:236` | Stripe checkout success | 1 / payment | Low | |
| `routes/stripe_webhook.py:146,196,223,299,335,349` | Stripe events (welcome, failure, cancel, enterprise lifecycle) | 1 / event | Low | All wrapped in try/except. |
| `cron/remind_unconfirmed_users.py:58,69` | Daily reminders + deletion warnings | **All matching unconfirmed users** | **Very high** | **No `last_sent_at` field.** Re-running the cron same day re-mails everyone in the window. 10-worker parallel send. |
| `cron/send_recap_digests.py:104` | Hourly digest cron | **All confirmed/active users with a frequency** | **High** | Has `last_digest_sent_at` per user (checked line 127, updated line 191) — duplicate-protected within the frequency window. Vulnerable to clock skew / NULL backfill. |
| `cron/check_watch_alerts.py:34` | Watch-alert match | **All active alerts that match** | **Medium** | Per-alert `last_notified_at` provides idempotency within the alert frequency. |
| `tasks.py:121` (`broadcast_task`) | Admin "Send broadcast" UI | **0 to entire active subscriber base** | **Extreme** | No idempotency token. Re-clicking submit re-enqueues. 10-worker parallel send. **Highest single-action blast radius in the system.** |
| `services/provisioning_service.py` (`provision-init`) | Enterprise stack init | 0 | n/a | Doesn't email — only provisions the admin user. Lifecycle emails are in `stripe_webhook.py` 299/335/349. |
| `utils/central_reporter.py` | Tenant → central ticket forward | n/a | n/a | HTTP only, not email. |

## 1400-Email Post-Mortem (2026-05-05)

Most plausible chain, given the available signals:

1. The dev box was running with `FLASK_ENV=production` to validate the
   real auth + provisioning flow.
2. The **previous** kill-switch (`MAIL_DEV_MODE`) required the same
   non-prod precondition and was therefore a no-op.
3. Repeated `delete_org_stack` + `provision_org_stack` cycles fired the
   Stripe-webhook welcome/lifecycle emails (all six 1-per-event paths in
   `stripe_webhook.py`), and any ad-hoc cron run of
   `remind_unconfirmed_users` or `send_recap_digests` against the prod
   user set fanned out to the matching cohort.
4. With `last_sent_at` tracking missing on `remind_unconfirmed_users`,
   the same recipients were eligible across iterations.

The new `MAIL_DRY_RUN` switch (set in the local `.env`) closes this hole.
Production is unaffected.

## Outstanding Hardening

These are *known* gaps, not yet fixed:

- **`remind_unconfirmed_users`**: add a `last_reminder_sent_at` column on
  `User` (or a small `email_send_log` table) to make re-runs idempotent.
- **`broadcast_task`**: require an idempotency token on submit + cap
  recipient lists (e.g. `> 500 → require explicit confirmation step`).
- **`send_recap_digests`**: make `last_digest_sent_at` an indexed column
  if it isn't already; backfill `NULL` users at first cron run rather
  than treating `NULL` as "send immediately".
- **2FA email retries**: throttle GET on `/2fa/verify` so reloads don't
  resend the OTP — currently any reload re-issues a code.

## Operational Checklist When Sending Real Email From Local

1. `MAIL_DRY_RUN=true` in the local `.env` (default).
2. To temporarily test real delivery from this box:
   ```bash
   MAIL_DRY_RUN=false MAIL_FORCE_SEND=true podman-compose ... up -d
   ```
   Send **one** test message, verify it arrived, then flip back.
3. Never run cron jobs that fan out to the user table without verifying
   `MAIL_DRY_RUN=true` first.
