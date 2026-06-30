import threading
from datetime import datetime, timezone

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app.ext import db, limiter
from app.models.sharing import ScraperShare
from app.models.user import User
from app.models.user_scraper import UserScraper, UserScraperArticle
from app.services.user_scraper_service import (
    USER_SCRAPER_MAX_SOURCES,
    _try_rss,
    delete_scraper_from_index,
    detect_source,
    get_scraper_limit,
    scrape_source,
)
from app.utils.logger import get_logger


logger = get_logger("user_scrapers", subfolder="routes")

user_scrapers_bp = Blueprint("user_scrapers", __name__)


def _require_subscriber(fn):
    # Pivot Free/Pro : les sources personnalisées sont accessibles au tier Free
    # (compte confirmé), CAPÉES (max 50 sources, 100 articles/source) ; Pro =
    # illimité. La différenciation se fait sur le quota dans add_scraper, pas
    # sur l'accès. Bloque seulement les comptes non confirmés / inactifs.
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_subscriber:
            flash("Veuillez confirmer votre compte pour gérer vos sources.", "warning")
            return redirect(url_for("home.welcome_page"))
        return fn(*args, **kwargs)

    return wrapper


@user_scrapers_bp.route("/mes-sources")
@login_required
@_require_subscriber
def scraper_list():
    from app.models.tag import Tag

    tag_filter = (request.args.get("tag") or "").strip()

    base_query = UserScraper.query.filter_by(user_id=current_user.id)
    if tag_filter:
        base_query = (
            base_query
            .join(UserScraper.articles)
            .join(UserScraperArticle.tags)
            .filter(Tag.name == tag_filter)
            .distinct()
        )
    # Collect available tags across all user scraper articles — single query with eager tags
    scraper_ids_subq = db.select(UserScraper.id).filter_by(user_id=current_user.id).scalar_subquery()
    articles_for_tags = (
        UserScraperArticle.query
        .filter(UserScraperArticle.scraper_id.in_(scraper_ids_subq))
        .options(selectinload(UserScraperArticle.tags))
        .all()
    )
    seen_tag_ids: set = set()
    available_tags = []
    for art in articles_for_tags:
        for tag in art.tags:
            if tag.id not in seen_tag_ids:
                seen_tag_ids.add(tag.id)
                available_tags.append(tag)
    available_tags.sort(key=lambda t: t.name)

    max_sources = get_scraper_limit(current_user)

    page = request.args.get("page", 1, type=int)
    valid_per_page = (10, 50, 100)
    per_page = request.args.get("per_page", 10, type=int)
    if per_page not in valid_per_page:
        per_page = 10

    pagination_scrapers = base_query.order_by(UserScraper.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return render_template(
        "user/scrapers.html",
        scrapers=pagination_scrapers.items,
        pagination=pagination_scrapers,
        per_page=per_page,
        is_org_member=False,
        available_tags=available_tags,
        tag_filter=tag_filter,
        max_sources=max_sources,
        quota_used=UserScraper.query.filter_by(user_id=current_user.id).count(),
    )


@user_scrapers_bp.route("/mes-sources/discover-feed")
@login_required
@limiter.limit("30 per hour", key_func=lambda: str(current_user.id))
def discover_feed():
    """Try to find an RSS/Atom feed URL for the given URL (used for live preview)."""
    raw_url = (request.args.get("url") or "").strip()
    if not raw_url:
        return jsonify({"found": False}), 400
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
    feed_url = _try_rss(raw_url)
    if feed_url:
        return jsonify({"found": True, "feed_url": feed_url})
    return jsonify({"found": False})


@user_scrapers_bp.route("/mes-sources/add", methods=["POST"])
@login_required
@_require_subscriber
@limiter.limit("10 per hour", key_func=lambda: str(current_user.id))
def add_scraper():
    from datetime import datetime
    name = (request.form.get("name") or "").strip()[:255]
    raw_url = (request.form.get("url") or "").strip()
    since_str = (request.form.get("scrape_since") or "").strip()

    if not name or not raw_url:
        return jsonify({"success": False, "message": "Nom et URL requis."}), 400

    scrape_since = None
    if since_str:
        try:
            scrape_since = datetime.strptime(since_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return jsonify({"success": False, "message": "Format de date invalide (AAAA-MM-JJ)."}), 400

    try:
        resolved_url, scraper_type = detect_source(raw_url)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    # Bug H14 — race condition sur le quota : deux POST parallèles
    # passaient le check count() et insertaient tous les deux, dépassant
    # la limite. Fix : SELECT … FOR UPDATE sur la ligne user pour
    # sérialiser les ajouts du même user, puis re-vérifier count après
    # le lock. SQLite no-op pour `with_for_update`, mais sans threads
    # concurrents en tests c'est sans incidence.
    max_sources = get_scraper_limit(current_user)
    db.session.query(User).filter_by(id=current_user.id).with_for_update().first()
    quota_used = UserScraper.query.filter_by(user_id=current_user.id).count()
    if max_sources is not None and quota_used >= max_sources:
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": f"Quota atteint ({max_sources} sources max).",
        }), 400

    # Check for duplicate URL for this user (sous le même lock)
    if UserScraper.query.filter_by(user_id=current_user.id, url=resolved_url).first():
        db.session.rollback()
        return jsonify({"success": False, "message": "Cette source est déjà dans votre liste."}), 400

    scraper = UserScraper(
        user_id=current_user.id, name=name, url=resolved_url,
        scraper_type=scraper_type, scrape_since=scrape_since,
    )
    db.session.add(scraper)
    db.session.commit()

    # Trigger first scrape in background
    app_ctx = None
    try:
        from flask import current_app
        app_ctx = current_app._get_current_object()
    except RuntimeError:
        pass

    scraper_id = scraper.id
    if app_ctx:
        def _run():
            # Bug M14 — defense-in-depth : le teardown du app_context fait
            # déjà db.session.remove(), mais on assure le rollback explicite
            # en cas d'exception pour éviter qu'une transaction sale ne soit
            # commitée par mégarde au teardown.
            with app_ctx.app_context():
                try:
                    scrape_source(scraper_id)
                except Exception:
                    db.session.rollback()
                    logger.exception(f"Initial scrape failed for scraper {scraper_id}")
                finally:
                    db.session.remove()

        threading.Thread(target=_run, daemon=True).start()

    type_label = "flux RSS" if scraper_type == "rss" else "page web"
    logger.info(f"User {current_user.id} added scraper {scraper_id} [{scraper_type}]: {resolved_url}")
    return jsonify({
        "success": True,
        "message": f"Source ajoutée ({type_label}). Indexation en cours.",
        "scraper_id": scraper.id,
        "scraper_type": scraper_type,
    })


@user_scrapers_bp.route("/mes-sources/delete", methods=["POST"])
@login_required
@_require_subscriber
def delete_scraper():
    data = request.get_json(silent=True) or {}
    scraper_id = data.get("scraper_id")
    if not scraper_id:
        return jsonify({"success": False, "message": "scraper_id manquant."}), 400

    scraper = db.session.get(UserScraper, scraper_id)
    if not scraper or scraper.user_id != current_user.id:
        return jsonify({"success": False, "message": "Source introuvable."}), 404

    user_id = current_user.id
    delete_scraper_from_index(scraper_id, user_id)

    # Cascade handles UserScraperArticle rows
    db.session.delete(scraper)
    db.session.commit()

    logger.info(f"User {user_id} deleted scraper {scraper_id}")
    return jsonify({"success": True, "message": "Source supprimée."})


@user_scrapers_bp.route("/mes-sources/refresh", methods=["POST"])
@login_required
@_require_subscriber
@limiter.limit("20 per hour", key_func=lambda: str(current_user.id))
def refresh_scraper():
    data = request.get_json(silent=True) or {}
    scraper_id = data.get("scraper_id")
    if not scraper_id:
        return jsonify({"success": False, "message": "scraper_id manquant."}), 400

    scraper = db.session.get(UserScraper, scraper_id)
    if not scraper or scraper.user_id != current_user.id:
        return jsonify({"success": False, "message": "Source introuvable."}), 404

    from flask import current_app
    app_ctx = current_app._get_current_object()

    def _run():
        # Bug M14 — rollback + remove explicites en defense-in-depth.
        with app_ctx.app_context():
            try:
                n, err = scrape_source(scraper_id)
                logger.info(f"Manual refresh scraper {scraper_id}: {n} new articles, err={err}")
            except Exception:
                db.session.rollback()
                logger.exception(f"Manual refresh failed for scraper {scraper_id}")
            finally:
                db.session.remove()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "message": "Actualisation lancée en arrière-plan."})


@user_scrapers_bp.route("/mes-sources/share", methods=["POST"])
@login_required
@_require_subscriber
def share_scraper():
    data = request.get_json(silent=True) or {}
    scraper_id = data.get("scraper_id")
    email = (data.get("email") or "").strip().lower()

    if not scraper_id or not email:
        return jsonify({"success": False, "message": "scraper_id et email requis."}), 400

    scraper = db.session.get(UserScraper, scraper_id)
    if not scraper or scraper.user_id != current_user.id:
        return jsonify({"success": False, "message": "Source introuvable."}), 404

    target = User.query.filter_by(email=email).first()
    if not target:
        return jsonify({"success": False, "message": "Aucun utilisateur trouvé avec cet email."}), 404

    if target.id == current_user.id:
        return jsonify({"success": False, "message": "Vous ne pouvez pas partager avec vous-même."}), 400

    if ScraperShare.query.filter_by(scraper_id=scraper_id, shared_with_id=target.id).first():
        return jsonify({"success": False, "message": "Déjà partagé avec cet abonné."}), 400

    share = ScraperShare(scraper_id=scraper_id, owner_id=current_user.id, shared_with_id=target.id)
    db.session.add(share)
    db.session.commit()

    logger.info(f"User {current_user.id} shared scraper {scraper_id} with user {target.id}")
    return jsonify({"success": True, "message": "Source partagée.", "share_id": share.id, "email": email})


@user_scrapers_bp.route("/mes-sources/articles", methods=["GET"])
@login_required
@_require_subscriber
def scraper_articles():
    scraper_id = request.args.get("scraper_id", type=int)
    if not scraper_id:
        return jsonify({"success": False, "message": "scraper_id manquant."}), 400

    scraper = db.session.get(UserScraper, scraper_id)
    if not scraper or scraper.user_id != current_user.id:
        return jsonify({"success": False, "message": "Source introuvable."}), 404

    articles = (
        UserScraperArticle.query
        .filter_by(scraper_id=scraper_id)
        .options(selectinload(UserScraperArticle.tags))
        .order_by(UserScraperArticle.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify({
        "success": True,
        "articles": [
            {
                "id": a.id,
                "title": a.title,
                "url": a.url,
                "published_at": a.published_at.strftime("%d/%m/%Y") if a.published_at else "",
                "indexed_at": a.indexed_at.strftime("%d/%m/%Y %H:%M") if a.indexed_at else "",
                "chunk_count": a.chunk_count,
                "tags": [{"id": t.id, "name": t.name} for t in a.tags],
            }
            for a in articles
        ],
    })


@user_scrapers_bp.route("/mes-sources/delete-article", methods=["POST"])
@login_required
@_require_subscriber
def delete_scraper_article():
    data = request.get_json(silent=True) or {}
    article_id = data.get("article_id")
    if not article_id:
        return jsonify({"success": False, "message": "article_id manquant."}), 400

    article = db.session.get(UserScraperArticle, article_id)
    if not article or article.user_id != current_user.id:
        return jsonify({"success": False, "message": "Article introuvable."}), 404

    # Remove ChromaDB chunks
    try:
        from app.services.rag_service import _get_user_collection
        collection = _get_user_collection(current_user.id)
        ids_to_delete = [f"userscraper_{article.id}_chunk_{i}" for i in range(article.chunk_count)]
        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
    except Exception:
        logger.exception(f"ChromaDB delete failed for article {article_id}")

    scraper_id = article.scraper_id
    # Bug N3 (audit round 2) — l'ancien code faisait deux commits
    # consécutifs (delete article puis recalcul article_count). Entre
    # les deux, une autre transaction pouvait s'intercaler → count
    # incohérent durable. Tout en une seule transaction.
    db.session.delete(article)
    scraper = db.session.get(UserScraper, scraper_id)
    if scraper:
        # SQL flush implicite avant le COUNT pour qu'il reflète le delete.
        db.session.flush()
        scraper.article_count = UserScraperArticle.query.filter_by(
            scraper_id=scraper_id,
        ).count()
    db.session.commit()

    logger.info(f"User {current_user.id} deleted article {article_id} from scraper {scraper_id}")
    return jsonify({"success": True})


@user_scrapers_bp.route("/mes-sources/update-since", methods=["POST"])
@login_required
@_require_subscriber
def update_scraper_since():
    data = request.get_json(silent=True) or {}
    scraper_id = data.get("scraper_id")
    since_str = (data.get("scrape_since") or "").strip()

    scraper = db.session.get(UserScraper, scraper_id)
    if not scraper or scraper.user_id != current_user.id:
        return jsonify({"success": False, "message": "Source introuvable."}), 404

    if since_str:
        try:
            scraper.scrape_since = datetime.strptime(since_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return jsonify({"success": False, "message": "Format de date invalide (AAAA-MM-JJ)."}), 400
    else:
        scraper.scrape_since = None

    db.session.commit()
    logger.info("User %s set scraper %s since=%s", current_user.id, scraper_id, since_str or "None")
    return jsonify({"success": True})


@user_scrapers_bp.route("/mes-sources/update-frequency", methods=["POST"])
@login_required
@_require_subscriber
def update_scraper_frequency():
    data = request.get_json(silent=True) or {}
    scraper_id = data.get("scraper_id")
    frequency = (data.get("frequency") or "daily").strip()
    if frequency not in ("daily", "weekly", "monthly"):
        return jsonify({"success": False, "message": "Fréquence invalide."}), 400

    scraper = db.session.get(UserScraper, scraper_id)
    if not scraper or scraper.user_id != current_user.id:
        return jsonify({"success": False, "message": "Source introuvable."}), 404

    scraper.scrape_frequency = frequency
    db.session.commit()
    logger.info(f"User {current_user.id} set scraper {scraper_id} frequency to {frequency}")
    return jsonify({"success": True})


@user_scrapers_bp.route("/mes-sources/unshare", methods=["POST"])
@login_required
@_require_subscriber
def unshare_scraper():
    data = request.get_json(silent=True) or {}
    share_id = data.get("share_id")
    if not share_id:
        return jsonify({"success": False, "message": "share_id manquant."}), 400

    share = db.session.get(ScraperShare, share_id)
    if not share or share.owner_id != current_user.id:
        return jsonify({"success": False, "message": "Partage introuvable."}), 404

    db.session.delete(share)
    db.session.commit()
    return jsonify({"success": True, "message": "Partage retiré."})


# ---------------------------------------------------------------------------
# Scrape progress (polled by the dashboard banner — same pattern as
# /user/cloud/sync-status for cloud connectors)
# ---------------------------------------------------------------------------

@user_scrapers_bp.route("/mes-sources/scrape-status", methods=["GET"])
@login_required
def scrape_status():
    """Return JSON describing in-flight scraper runs for the current user.

    Polled every ~10s by the banner partial in base.html. Returns an empty
    list when nothing is running so the banner removes itself.

    Response shape::

        {"scrapes": [
            {"id": 12, "name": "Le Monde RSS", "scraper_type": "rss",
             "processed": 14, "total": 47, "started_at": "...",
             "elapsed_s": 92, "eta_s": 218}
        ]}
    """
    from datetime import datetime, timezone

    rows = (
        db.session.query(UserScraper)
        .filter(UserScraper.user_id == current_user.id,
                UserScraper.is_scraping.is_(True))
        .all()
    )

    payload = []
    now = datetime.now(timezone.utc)
    for s in rows:
        processed = s.scrape_processed or 0
        total = s.scrape_total
        started = s.scrape_started_at
        if started and started.tzinfo is None:
            started_aware = started.replace(tzinfo=timezone.utc)
        else:
            started_aware = started
        elapsed_s = int((now - started_aware).total_seconds()) if started_aware else None

        # ETA: derive from observed throughput, same heuristic as cloud sync.
        eta_s = None
        if processed >= 3 and total and elapsed_s and elapsed_s > 0:
            rate = processed / elapsed_s
            if rate > 0:
                remaining = max(0, total - processed)
                eta_s = int(remaining / rate)

        payload.append({
            "id": s.id,
            "name": s.name,
            "scraper_type": s.scraper_type,
            "processed": processed,
            "total": total,
            "started_at": started_aware.isoformat() if started_aware else None,
            "elapsed_s": elapsed_s,
            "eta_s": eta_s,
        })

    return jsonify({"scrapes": payload})
