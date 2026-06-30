import logging
import os
import threading

import click
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_babel import gettext as _  # noqa: F401 — exposes _ to app context
from flask_login import LoginManager, current_user
from flask_wtf import CSRFProtect
from markupsafe import escape
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config as BaseConfig
from app.ext import babel, db, migrate, oauth
from app.utils.flask_limiter import limiter


load_dotenv()


def _thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    logging.getLogger("app.threads").error(
        "Unhandled exception in thread %s",
        args.thread.name if args.thread else "unknown",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


threading.excepthook = _thread_excepthook

login_manager = LoginManager()
login_manager.login_view = "auth.login"
csrf = CSRFProtect()


class Config(BaseConfig):
    pass


def create_app(config_class=Config):
    flask_app = Flask(__name__)
    flask_app.config.from_object(config_class)

    # Configure ProxyFix to trust reverse proxy headers.
    # Round 5 A4-H3: PROXY_HOPS env (default 1) drives x_for / x_proto /
    # x_host uniformly. A misconfigured chain (e.g. Pangolin → Nginx →
    # Gunicorn = 2 hops but PROXY_HOPS=1) makes ProxyFix pick the wrong
    # X-Forwarded-For entry and collapses every client onto the
    # immediate-upstream IP — neutering per-IP rate limits on
    # /login, /register, /forgot-password. Operators must set
    # PROXY_HOPS to the exact number of trusted proxies between client
    # and Gunicorn. Clamped to [0, 10] to refuse insane values.
    _proxy_hops = max(0, min(10, int(os.environ.get("PROXY_HOPS", "1"))))
    flask_app.wsgi_app = ProxyFix(
        flask_app.wsgi_app,
        x_for=_proxy_hops,
        x_proto=_proxy_hops,
        x_host=_proxy_hops,
    )

    # Override database config for testing if needed
    if os.environ.get("TESTING", "false").lower() == "true":
        flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    # Fail fast on missing production secrets
    if not os.environ.get("TESTING"):
        config_class.validate_production()

    db.init_app(flask_app)

    def _get_locale():
        # OSS edition ships in English only.
        return "en"

    import os as _os
    _translations_dir = _os.path.join(_os.path.dirname(flask_app.root_path), "translations")
    flask_app.config["BABEL_TRANSLATION_DIRECTORIES"] = _translations_dir
    babel.init_app(flask_app, locale_selector=_get_locale)
    flask_app.jinja_env.globals["get_locale"] = _get_locale
    from app.utils.time_utils import utcnow as _utcnow
    flask_app.jinja_env.globals["now"] = _utcnow

    # ── OAuth social login (Authlib) ─────────────────────────────────────────
    oauth.init_app(flask_app)

    if flask_app.config.get("GOOGLE_LOGIN_CLIENT_ID"):
        oauth.register(
            name="google",
            client_id=flask_app.config["GOOGLE_LOGIN_CLIENT_ID"],
            client_secret=flask_app.config["GOOGLE_LOGIN_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    if flask_app.config.get("MICROSOFT_LOGIN_CLIENT_ID"):
        oauth.register(
            name="microsoft",
            client_id=flask_app.config["MICROSOFT_LOGIN_CLIENT_ID"],
            client_secret=flask_app.config["MICROSOFT_LOGIN_CLIENT_SECRET"],
            server_metadata_url=(
                "https://login.microsoftonline.com/common/v2.0"
                "/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": "openid email profile"},
        )

    if flask_app.config.get("APPLE_LOGIN_CLIENT_ID"):
        from app.routes.auth_social import _apple_client_secret

        # Bug H5 — secret de boot avec TTL 30j en filet de sécurité ;
        # social_callback regenerate juste avant chaque token exchange.
        oauth.register(
            name="apple",
            client_id=flask_app.config["APPLE_LOGIN_CLIENT_ID"],
            client_secret=_apple_client_secret(ttl_seconds=86400 * 30),
            api_base_url="https://appleid.apple.com",
            access_token_url="https://appleid.apple.com/auth/token",
            authorize_url="https://appleid.apple.com/auth/authorize",
            jwks_uri="https://appleid.apple.com/auth/keys",
            client_kwargs={
                "scope": "openid email name",
                "response_mode": "form_post",
            },
        )

    def email_format(value: str) -> str:
        if not value:
            return ""
        lines = value.splitlines()
        html_parts = []
        in_list = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                if not in_list:
                    html_parts.append("<ul>")
                    in_list = True
                content = stripped[2:]
                html_parts.append(f"<li>{escape(content)}</li>")
            else:
                if in_list:
                    html_parts.append("</ul>")
                    in_list = False
                if stripped:
                    html_parts.append(f"{escape(stripped)}<br>")
                else:
                    html_parts.append("<br>")
        if in_list:
            html_parts.append("</ul>")
        return "".join(html_parts)

    flask_app.jinja_env.filters["email_format"] = email_format

    # SECURITY (audit C-3): every URL coming from stored user content
    # (RSS scraper article URLs, cloud doc links, source citations on
    # the public /r/<token> page) is rendered into a `<a href="...">`.
    # `escHtml`-equivalent (Jinja autoescape) only handles HTML-special
    # chars — it does NOT block `javascript:` / `data:` / `vbscript:`
    # URLs that the browser then runs as inline JS on click. The
    # `safe_url` filter rewrites those to "#" so the link is rendered
    # but inert. Use it everywhere stored content lands in href.
    def _safe_url(raw):
        if not raw or not isinstance(raw, str):
            return "#"
        # Strip whitespace + control chars before the scheme — some
        # browsers tolerate "java\tscript:..." and execute it.
        trimmed = "".join(c for c in raw if c.isprintable()).strip()
        lowered = trimmed.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            return raw
        if lowered.startswith("mailto:"):
            return raw
        # Same-origin relative paths and anchors — fine.
        if trimmed.startswith("/") and not trimmed.startswith("//"):
            return raw
        if trimmed.startswith("#"):
            return raw
        return "#"

    flask_app.jinja_env.filters["safe_url"] = _safe_url

    from app.models import User
    from app.models.cloud_connection import CloudConnection, CloudDocument  # noqa: F401
    from app.models.oauth_account import OAuthAccount  # noqa: F401

    migrate.init_app(flask_app, db)
    login_manager.init_app(flask_app)

    with flask_app.app_context():
        try:
            db.session.query(CloudConnection).filter_by(is_syncing=True).update({"is_syncing": False})
            db.session.commit()
        except Exception:
            db.session.rollback()

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return db.session.get(User, int(user_id))
        except (ValueError, TypeError):
            return None

    csrf.init_app(flask_app)

    # Set password hashing method to pbkdf2_sha256 for compatibility
    flask_app.config["SECURITY_PASSWORD_HASH"] = "pbkdf2_sha256"  # nosec B105
    _salt = os.getenv("SECURITY_PASSWORD_SALT")
    if not _salt:
        if os.getenv("FLASK_ENV") == "production":
            raise ValueError(
                "SECURITY_PASSWORD_SALT must be set in production. Add it to your .env file."
            )
        import warnings
        warnings.warn(
            "SECURITY_PASSWORD_SALT not set — using insecure default. Set it in .env for production.",
            stacklevel=2,
        )
        _salt = "dev-only-insecure-salt"
    flask_app.config["SECURITY_PASSWORD_SALT"] = _salt  # nosec B105

    # Disable rate limiting entirely in testing
    if not os.environ.get("TESTING"):
        limiter.init_app(flask_app)

        # Configure rate limiting from environment
        if limiter.enabled and hasattr(flask_app.config, "RATELIMIT_DEFAULT"):
            limiter.limit(flask_app.config.RATELIMIT_DEFAULT)(flask_app)

    from flask import Blueprint

    class CSRFExemptBlueprint(Blueprint):
        def add_url_rule(self, rule, endpoint=None, view_func=None, **options):
            if view_func and getattr(view_func, "csrf_exempt", False):
                view_func.csrf_exempt = True
            super().add_url_rule(rule, endpoint, view_func, **options)

    # Import and register blueprints
    from app.routes.admin import admin_cron, admin_ops  # noqa: F401 — registers routes on admin_bp
    from app.routes.admin.admin import admin_bp
    from app.routes.admin.admin_tools import admin_tools_bp
    from app.routes.admin.manage_users import manage_users_bp
    from app.routes.auth import auth_bp
    from app.routes.auth_social import auth_social_bp
    from app.routes.base import base_bp
    from app.routes.health import health_bp
    from app.routes.home import home_bp
    from app.routes.search import search_bp
    from app.routes.user import user_bp
    from app.routes.user_2fa import user_2fa_bp
    from app.routes.user_cloud import user_cloud_bp
    from app.routes.user_library import user_library_bp
    from app.routes.user_rag import user_rag_bp
    from app.routes.user_scrapers import user_scrapers_bp
    from app.routes.user_tags import user_tags_bp
    from app.routes.user_tickets import user_tickets_bp
    from app.routes.user_tools import user_tools_bp

    sovereign = flask_app.config.get("SOVEREIGN_MODE", False)

    flask_app.register_blueprint(admin_bp, url_prefix="/admin")
    flask_app.register_blueprint(manage_users_bp, url_prefix="/admin/users")
    flask_app.register_blueprint(admin_tools_bp, url_prefix="/admin/admin_tools")
    flask_app.register_blueprint(user_bp, url_prefix="/user")
    flask_app.register_blueprint(user_library_bp, url_prefix="/user")
    flask_app.register_blueprint(user_rag_bp, url_prefix="/user")
    flask_app.register_blueprint(user_scrapers_bp, url_prefix="/user")
    flask_app.register_blueprint(user_tools_bp, url_prefix="/user")
    flask_app.register_blueprint(user_cloud_bp, url_prefix="/user")
    flask_app.register_blueprint(user_2fa_bp, url_prefix="/user")
    flask_app.register_blueprint(user_tags_bp, url_prefix="/user")
    flask_app.register_blueprint(user_tickets_bp, url_prefix="/user")
    flask_app.register_blueprint(auth_social_bp)
    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(base_bp)
    flask_app.register_blueprint(home_bp)
    flask_app.register_blueprint(health_bp)
    flask_app.register_blueprint(search_bp)

    @flask_app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("user.dashboard"))
        if sovereign:
            # No public pricing landing on-premise — go straight to login.
            return redirect(url_for("auth.login"))
        return redirect(url_for("home.welcome_page"))

    @flask_app.route("/robots.txt")
    def robots_txt():
        # Servir le robots.txt depuis le filesystem statique mais à la
        # racine du site (les crawlers ne regardent pas /static/robots.txt).
        return flask_app.send_static_file("robots.txt")

    @flask_app.cli.command("create-admin")
    def create_admin():
        """Crée le premier compte administrateur."""
        from app.ext import db
        from app.models.user import User

        email = input("Email admin : ").strip().lower()
        password = input("Mot de passe : ")

        if User.query.filter_by(email=email).first():
            print("❌ Un compte avec cet email existe déjà.")
            return

        admin = User(
            email=email,
            is_admin=True,
            is_active=True,
            is_confirmed=True,
            abonnement_type="pro",
        )
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        print(f"✅ Administrateur {email} créé.")

    @flask_app.cli.command("update-admin")
    def update_admin():
        """Met à jour l’email et le mot de passe de l’administrateur."""
        from app.ext import db
        from app.models.user import User

        email = input("Nouvel email admin : ")
        password = input("Nouveau mot de passe : ")

        admin = User.query.filter_by(is_admin=True).first()
        if admin:
            admin.email = email
            admin.set_password(password)
            db.session.commit()
            print("✅ Administrateur mis à jour.")
        else:
            print("❌ Aucun administrateur trouvé.")

    @flask_app.cli.command("delete-user")
    def delete_user():
        """Supprimer un utilisateur par email"""
        from app.models.user import User

        email = input("Email de l'utilisateur à supprimer : ").strip()
        user = User.query.filter_by(email=email).first()
        if user:
            db.session.delete(user)
            db.session.commit()
            print(f"✅ Utilisateur {email} supprimé.")
        else:
            print("❌ Utilisateur introuvable.")

    @flask_app.cli.command("reindex-doc-dates")
    def reindex_doc_dates():
        """Backfill doc_date / doc_ts metadata on existing ChromaDB chunks.

        Chunks indexed before the temporal-filter feature don't carry a
        date in their metadata, so they're silently excluded once a
        date filter is active. This walks every CloudDocument,
        UserDocument and Newsletter row, recomputes the metadata via
        the same _doc_date_meta() helper used at indexing time, and
        updates the matching Chroma chunk metadata in place — no
        re-embedding, just a metadata patch.
        """
        from app.models.cloud_connection import CloudDocument
        from app.models.newsletter import Newsletter
        from app.models.user_document import UserDocument
        from app.services.rag_service import _doc_date_meta, _get_collection, _get_user_collection

        total_user = total_cloud = total_news = 0

        # User documents — personal collection per user.
        for d in UserDocument.query.filter(UserDocument.chunk_count > 0).all():
            meta = _doc_date_meta(d.uploaded_at)
            if not meta:
                continue
            ids = [f"userdoc_{d.id}_chunk_{i}" for i in range(d.chunk_count)]
            try:
                col = _get_user_collection(d.user_id)
                col.update(ids=ids, metadatas=[meta] * len(ids))
                total_user += len(ids)
            except Exception as exc:
                print(f"  user_doc {d.id} skipped: {exc}")

        # Cloud documents — personal collection per user.
        for d in CloudDocument.query.filter(CloudDocument.chunk_count > 0).all():
            meta = _doc_date_meta(d.remote_modified_at)
            if not meta:
                continue
            ids = [f"clouddoc_{d.id}_chunk_{i}" for i in range(d.chunk_count)]
            try:
                col = _get_user_collection(d.user_id)
                col.update(ids=ids, metadatas=[meta] * len(ids))
                total_cloud += len(ids)
            except Exception as exc:
                print(f"  cloud_doc {d.id} skipped: {exc}")

        # Newsletters — shared collection, one chunk per section.
        for nl in Newsletter.query.filter_by(status="sent").all():
            meta = _doc_date_meta(nl.date)
            if not meta:
                continue
            ids = [f"section_{s.id}" for s in nl.sections]
            if not ids:
                continue
            try:
                col = _get_collection()
                col.update(ids=ids, metadatas=[meta] * len(ids))
                total_news += len(ids)
            except Exception as exc:
                print(f"  newsletter {nl.id} skipped: {exc}")

        print(
            f"✅ Backfill complete — user_doc:{total_user} chunks, "
            f"cloud_doc:{total_cloud}, newsletter:{total_news}"
        )

    @click.option("--force", is_flag=True, default=False,
                  help="Acquire the reindex lock even if marked busy (last resort — "
                       "use only when sure the held lock is stale).")
    @flask_app.cli.command("reindex-embeddings")
    def reindex_embeddings(force: bool):
        """Re-embed every chunk in every ChromaDB collection with the active embed provider.

        Required after switching EMBED_PROVIDER from ollama to mistral (or
        vice-versa) — the vector spaces are not comparable, so existing
        vectors must be regenerated before queries return meaningful results.

        Walks UserDocument, CloudDocument (per-user and per-org) and
        Newsletter rows, re-runs chunking + _embed() and overwrites the
        ChromaDB embeddings in place (same chunk ids, metadata preserved).

        Long-running: typical Pro corpus (~1000 docs) re-embeds in 2-5 min
        against Mistral API. Run during a maintenance window — the search
        results are degraded for the duration (mixed vector spaces).

        Audit round 4 — A3-C2. The CLI now acquires
        ``_EMBED_REINDEX_LOCK_KEY`` before mutating Chroma so it can't
        race against the RQ ``reindex_embeddings_task`` triggered from
        the admin UI. The lock is released in a finally block on every
        exit path so a Ctrl-C or crash never permanently jams future
        switches.
        """
        import os as _os
        import sys as _sys

        from app.models.cloud_connection import CloudDocument
        from app.models.newsletter import Newsletter
        from app.models.user_document import UserDocument
        from app.services.rag_service import (
            _EMBED_REINDEX_LOCK_KEY,
            EmbedDimensionMismatch,
            _active_embed_model,
            _embed,
            _get_collection,
            _get_user_collection,
            assert_collection_compat_for_reindex,
        )
        from app.utils.text_extraction import chunk_text as _chunk_text

        redis_url = _os.getenv("REDIS_URL")
        lock_client = None
        lock_owned = False
        if redis_url:
            try:
                from redis import Redis
                lock_client = Redis.from_url(redis_url, socket_connect_timeout=2)
                if force:
                    lock_client.set(_EMBED_REINDEX_LOCK_KEY, "cli-forced", ex=3600)
                    lock_owned = True
                    print("⚠ --force: lock acquired regardless of prior holder.")
                else:
                    # 1h TTL — covers the slowest realistic reindex; expires
                    # automatically if the CLI is SIGKILLed before finally.
                    lock_owned = bool(lock_client.set(
                        _EMBED_REINDEX_LOCK_KEY, "cli", nx=True, ex=3600,
                    ))
                if not lock_owned:
                    print(
                        "❌ Refusing to run: another reindex is in progress "
                        "(RQ task or another CLI). Re-run with --force only "
                        "if you're sure the lock is stale."
                    )
                    _sys.exit(1)
            except SystemExit:
                raise
            except Exception as exc:
                # Redis-down: proceed without the lock but warn loudly.
                # Locally-run reindex in a sovereign install without Redis
                # is a legitimate (rare) use case.
                print(f"⚠ Could not contact Redis for the reindex lock ({exc}); "
                      f"running without serialisation.")
                lock_client = None
                lock_owned = False
        else:
            print("⚠ REDIS_URL not set; running without serialisation.")

        active = _active_embed_model()
        print(f"▶ Reindexing all chunks with active embed model: {active}")

        total = 0
        # A3-C3 — refuse to write into a collection whose first vector has
        # a different dim than the active model would emit. Cached per
        # collection so we probe each one only once.
        _checked: set = set()
        _skipped: set = set()

        def _check_or_skip(col, label):
            cid = id(col)
            if cid in _checked:
                return True
            if cid in _skipped:
                return False
            try:
                assert_collection_compat_for_reindex(col)
                _checked.add(cid)
                return True
            except EmbedDimensionMismatch as mismatch:
                print(
                    f"  ❌ dim mismatch on {label} ({getattr(col, 'name', '?')}): "
                    f"existing={mismatch.existing} requested={mismatch.requested}\n"
                    f"     Delete the collection manually, then rerun."
                )
                _skipped.add(cid)
                return False

        try:
            # User documents (personal collection)
            for d in UserDocument.query.filter(
                UserDocument.chunk_count > 0,
            ).yield_per(50):
                try:
                    chunks = _chunk_text(d.content or "")
                    if not chunks:
                        continue
                    col = _get_user_collection(d.user_id)
                    if not _check_or_skip(col, "user_doc"):
                        continue
                    embeddings = _embed(chunks, doc_title=d.title or "")
                    ids = [f"userdoc_{d.id}_chunk_{i}" for i in range(len(chunks))]
                    col.update(ids=ids[:d.chunk_count], embeddings=embeddings[:d.chunk_count])
                    total += min(len(chunks), d.chunk_count)
                except Exception as exc:
                    print(f"  user_doc {d.id} skipped: {exc}")

            # Cloud documents — split user vs org
            for d in CloudDocument.query.filter(CloudDocument.chunk_count > 0).yield_per(50):
                try:
                    chunks = _chunk_text(d.content or "")
                    if not chunks:
                        continue
                    col = _get_user_collection(d.user_id)
                    if not _check_or_skip(col, "cloud_doc"):
                        continue
                    embeddings = _embed(chunks, doc_title=d.name or "")
                    ids = [f"clouddoc_{d.id}_chunk_{i}" for i in range(len(chunks))]
                    col.update(ids=ids[:d.chunk_count], embeddings=embeddings[:d.chunk_count])
                    total += min(len(chunks), d.chunk_count)
                except Exception as exc:
                    print(f"  cloud_doc {d.id} skipped: {exc}")

            # Newsletters — shared collection
            for nl in Newsletter.query.yield_per(50):
                sections = [s for s in nl.sections if s.content]
                if not sections:
                    continue
                try:
                    col = _get_collection()
                    if not _check_or_skip(col, "newsletter"):
                        continue
                    texts = [s.content for s in sections]
                    embeddings = _embed(texts, doc_title=nl.title or "")
                    ids = [f"section_{s.id}" for s in sections]
                    col.update(ids=ids, embeddings=embeddings)
                    total += len(sections)
                except Exception as exc:
                    print(f"  newsletter {nl.id} skipped: {exc}")

            print(f"✅ Reindex complete — {total} chunks re-embedded with {active}")
        finally:
            if lock_owned and lock_client is not None:
                try:
                    lock_client.delete(_EMBED_REINDEX_LOCK_KEY)
                except Exception:
                    pass

    # ── CSP nonce — per-request token for inline <script>/<style> ──────────
    # Generated once per request, exposed to templates via {{ csp_nonce() }},
    # and embedded into the script-src / style-src CSP directive in place of
    # the previous 'unsafe-inline' (audit M3, sprint 2E). Closes the residual
    # XSS amplification path: even if an injection lands in stored content,
    # the browser refuses to execute it without the matching nonce.
    @flask_app.before_request
    def _csp_nonce_setup():
        import secrets as _s

        from flask import g
        g.csp_nonce = _s.token_urlsafe(16)

    @flask_app.context_processor
    def _inject_csp_nonce():
        from flask import g

        def csp_nonce() -> str:
            # `g.csp_nonce` is set in the before_request hook above.
            # Fallback to empty string in test contexts that hit a template
            # outside a request — the browser will simply refuse the script,
            # which is louder than a silent crash.
            return getattr(g, "csp_nonce", "")
        return {"csp_nonce": csp_nonce}

    # ── Prometheus HTTP metrics middleware ──────────────────────────────────
    # Two hooks: before_request stamps the start time, after_request records
    # the duration + status code. Endpoint is the Flask rule name (e.g.
    # "auth.login") rather than the raw path so the cardinality stays
    # bounded — /admin/organizations/<int:org_id> doesn't blow up the label
    # set with one entry per org id.
    @flask_app.before_request
    def _metrics_start_timer():
        import time as _time

        from flask import g as _g
        _g._req_start = _time.monotonic()

    @flask_app.after_request
    def _metrics_record_request(response):
        try:
            import time as _time

            from flask import g as _g
            from flask import request as _req

            from app.utils.metrics import http_request_duration_seconds, http_requests_total

            endpoint = _req.endpoint or "unknown"
            method = _req.method
            status = str(response.status_code)
            http_requests_total.labels(method=method, endpoint=endpoint, status=status).inc()
            start = getattr(_g, "_req_start", None)
            if start is not None:
                http_request_duration_seconds.labels(
                    method=method, endpoint=endpoint,
                ).observe(_time.monotonic() - start)
        except Exception:
            # Never let metrics observation kill the request.
            pass
        return response

    @flask_app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Modern hardening headers per audit M2 — close the cross-window /
        # FLoC / camera / microphone surface that the legacy header set
        # left wide open by default.
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), interest-cohort=(), "
            "browsing-topics=(), payment=(self)"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        # CSP nonces (audit M3 / sprint 2E): inline <script> tags carry
        # nonce="{{ csp_nonce() }}" — see template sweep. The nonce comes
        # from g.csp_nonce (set in the before_request hook).
        #
        # NOTE on style-src: we KEEP 'unsafe-inline' on style-src because
        # the templates rely heavily on HTML `style="..."` attributes (the
        # logo size, badge colors, layout helpers — hundreds of them).
        # CSP nonces on style-src refuse those attributes (they can't
        # carry a nonce), which would explode the navbar to native size.
        # The audit's XSS-amplifier concern was script execution, not
        # style: a style-only injection can deface but cannot exfiltrate
        # cookies or run code. Keeping 'unsafe-inline' on style-src is
        # the standard pragmatic trade-off documented in CSP3.
        from flask import g
        nonce = getattr(g, "csp_nonce", "")
        script_nonce = f"'nonce-{nonce}'" if nonce else "'unsafe-inline'"

        if flask_app.config.get("SOVEREIGN_MODE", False):
            # Sovereign / on-premise: no Stripe, no Google reCAPTCHA, no
            # Analytics. Every asset is bundled locally since A4-L2
            # (debt), so the CSP can drop jsdelivr + fonts.googleapis +
            # fonts.gstatic entirely.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' {script_nonce}; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self' data:; "
                "img-src 'self' data:; "
                "frame-src 'self'; "
                "connect-src 'self';"
            )
        else:
            from app.utils.deployment_mode import is_classic, is_on_prem
            if is_on_prem():
                # On-prem: every asset is bundled locally. CSP is strict —
                # no external origin needed. Self-only.
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; "
                    f"script-src 'self' {script_nonce}; "
                    "style-src 'self' 'unsafe-inline'; "
                    "font-src 'self' data:; "
                    "img-src 'self' data:; "
                    "connect-src 'self';"
                )
            else:
                # Classic SaaS: CDN for Bootstrap/Fonts/Bootstrap Icons,
                # Stripe scripts, reCAPTCHA, GTM analytics.
                # Bug X13 (audit round 3) — gstatic.com ajouté à connect-src
                # (reCAPTCHA fait des XHR vers gstatic).
                # Bug X14 — img-src restreint : 'self' data: + 3 origines
                # connues, au lieu de `https:` global. Réduit la fenêtre
                # d'exfiltration via `<img src=...>` en cas d'XSS futur.
                # A4-L2 (debt) — jsdelivr / fonts.googleapis /
                # fonts.gstatic dropped: every third-party CSS / JS is
                # self-hosted under /static/vendor/. Stripe + Google
                # reCAPTCHA + GTM remain (they're runtime services, not
                # static assets).
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; "
                    f"script-src 'self' {script_nonce} https://www.google.com https://www.gstatic.com https://www.googletagmanager.com; "
                    "style-src 'self' 'unsafe-inline'; "
                    "font-src 'self' data:; "
                    "img-src 'self' data: https://www.google.com https://www.gstatic.com; "
                    "frame-src https://www.google.com; "
                    "connect-src 'self' https://www.google.com https://www.gstatic.com;"
                )
        return response

    @flask_app.context_processor
    def inject_admin_ticket_count():
        if current_user.is_authenticated and current_user.is_admin:
            from app.models.support_ticket import SupportTicket
            count = (
                SupportTicket.query.filter(SupportTicket.status.in_(("open", "in_progress"))).count()
            )
            return {"admin_open_ticket_count": count}
        return {"admin_open_ticket_count": 0}

    @flask_app.context_processor
    def inject_deployment_mode():
        """Expose deployment-mode helpers + EXTERNAL_URL to every template.

        ``sovereign_mode`` stays for backward-compat with templates that
        haven't been migrated; new code should use ``deployment_mode`` and
        the ``has_*`` predicates directly.

        external_url is used by transactional/digest email templates to
        build absolute links (logo, CTAs) since cron-sent emails have no
        request context to fall back on.
        """
        from app.utils.deployment_mode import (
            get_deployment_mode,
            has_stripe,
            is_byok,
            is_classic,
            is_hybrid,
            is_on_prem,
            is_sovereign,
        )
        mode = get_deployment_mode()
        return {
            "deployment_mode": mode,
            "is_classic": is_classic(),
            "is_sovereign": is_sovereign(),
            "is_hybrid": is_hybrid(),
            "is_on_prem": is_on_prem(),
            "has_stripe": has_stripe(),
            "is_byok": is_byok(),
            # Back-compat — drop after templates migrate.
            "sovereign_mode": is_sovereign(),
            "external_url": flask_app.config.get("EXTERNAL_URL", "https://myeline.io"),
        }

    @flask_app.before_request
    def force_password_reset_when_required():
        """Tout user avec must_reset_password=True ne peut rien faire
        d'autre que reset son password (et logout). S'applique AUSSI aux
        admins — important pour les admins tenant Enterprise qui ont
        reçu un password initial par email en clair (cf. bug 22).
        """
        if not current_user.is_authenticated:
            return
        if not getattr(current_user, "must_reset_password", False):
            return
        endpoint = request.endpoint
        if endpoint is None:
            return
        allowed = {
            "auth.logout",
            "auth.first_password_reset",
            "auth.first_password_reset_post",
            "static",
        }
        if endpoint in allowed or endpoint.startswith("static"):
            return
        return redirect(url_for("auth.first_password_reset"))

    @flask_app.before_request
    def restrict_unvalidated_users():
        if not current_user.is_authenticated:
            return

        if current_user.is_admin:
            return

        endpoint = request.endpoint
        if endpoint is None:
            return

        # Org invite routes must be accessible regardless of account state so that
        # an existing Myeline user (unconfirmed, no-2FA, or non-subscriber) can
        # accept an organisation invitation without being redirected to payment.
        if endpoint in ("org_auth.accept_invite", "org_auth.setup_from_invite"):
            return

        if not current_user.is_confirmed:
            allowed = {
                "auth.logout", "user.account_settings",
                "user_2fa.setup_2fa", "user_2fa.disable_2fa",
                "user_2fa.verify_2fa", "user_2fa.setup_sms",
                "static",
            }
            if endpoint not in allowed and not endpoint.startswith("static"):
                flash("Veuillez confirmer votre adresse email pour accéder au contenu.", "warning")
                return redirect(url_for("user.account_settings"))
            return

        # 2FA mandatory — block access until configured. The
        # is_2fa_bypassed() helper carves out OAuth-review accounts
        # (Google / Microsoft / Dropbox app validation) that can't
        # answer a TOTP challenge. See app/utils/twofa_bypass.py.
        if not current_user.has_2fa:
            from app.utils.twofa_bypass import is_2fa_bypassed
            if is_2fa_bypassed(current_user):
                return  # OAuth-review account: skip 2FA enforcement

            allowed_no_2fa = {
                "auth.logout",
                "user_2fa.setup_2fa", "user_2fa.disable_2fa", "user_2fa.verify_2fa",
                "static",
            }
            if endpoint not in allowed_no_2fa and not endpoint.startswith("static"):
                return redirect(url_for("user_2fa.setup_2fa"))

        # OSS self-host has no billing gate — every authenticated, confirmed,
        # 2FA-enrolled user has full access.

    # ── Global error handlers ────────────────────────────────────────────────

    @flask_app.errorhandler(404)
    def not_found(e):
        if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
            return jsonify(error="Not found"), 404
        return render_template("errors/404.html"), 404

    @flask_app.errorhandler(500)
    def internal_error(e):
        db.session.rollback()
        if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
            return jsonify(error="Internal server error"), 500
        return render_template("errors/500.html"), 500

    @flask_app.errorhandler(429)
    def too_many_requests(e):
        return jsonify(error="Trop de requêtes. Veuillez réessayer plus tard."), 429

    # Register CLI commands for data retention
    from app import commands

    commands.init_app(flask_app)

    # Register additional cleanup commands
    from app import commands_cleanup

    commands_cleanup.init_app(flask_app)

    # Register CLOUD_TOKEN_KEY rotation CLI
    from app import commands_rotate

    commands_rotate.init_app(flask_app)

    # Register `flask run-cron <task>` — used by the cron sidecar.
    from app import commands_cron

    commands_cron.init_app(flask_app)

    return flask_app


# Create app instance for WSGI servers
app = create_app()
