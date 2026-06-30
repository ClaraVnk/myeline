from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app.ext import db
from app.models.cloud_connection import CloudConnection, CloudDocument
from app.models.user_scraper import UserScraper, UserScraperArticle
from app.utils.logger import get_logger


logger = get_logger("user_library", subfolder="routes")

user_library_bp = Blueprint("user_library", __name__)


def _require_subscriber(fn):
    # OSS single-user build: every authenticated user has full access.
    # Kept as a pass-through so existing @_require_subscriber decorations
    # compile unchanged; @login_required still enforces authentication.
    return fn


class _DocItem:
    """Unified view over CloudDocument and UserScraperArticle."""
    __slots__ = (
        "id", "name", "doc_type", "chunk_count", "indexed_at",
        "tags", "provider_label", "scraper_name", "url",
        "mime_type", "file_size", "published_at", "_obj",
    )

    @classmethod
    def from_cloud(cls, doc, conn):
        o = cls.__new__(cls)
        o.id = doc.id
        o.name = doc.name
        o.doc_type = "cloud"
        o.chunk_count = doc.chunk_count
        o.indexed_at = doc.indexed_at
        o.tags = doc.tags
        o.provider_label = conn.provider_label if conn else ""
        o.scraper_name = None
        o.url = None
        o.mime_type = doc.mime_type
        o.file_size = doc.file_size
        o.published_at = None
        o._obj = doc
        return o

    @classmethod
    def from_scraper(cls, article, scraper):
        o = cls.__new__(cls)
        o.id = article.id
        o.name = article.title
        o.doc_type = "scraper"
        o.chunk_count = article.chunk_count
        o.indexed_at = article.indexed_at
        o.tags = article.tags
        o.provider_label = None
        o.scraper_name = scraper.name if scraper else ""
        o.url = article.url
        o.mime_type = None
        o.file_size = None
        o.published_at = article.published_at
        o._obj = article
        return o


@user_library_bp.route("/ma-bibliotheque")
@login_required
@_require_subscriber
def personal_library():
    from app.models.tag import Tag

    search_query = (request.args.get("q") or "").strip()
    provider_filter = (request.args.get("provider") or "").strip()
    tag_filter = (request.args.get("tag") or "").strip()
    source_filter = (request.args.get("source") or "").strip()  # "" | "cloud" | "scraper"

    valid_per_page = (10, 50, 100)
    per_page = request.args.get("per_page", 10, type=int)
    if per_page not in valid_per_page:
        per_page = 10
    page = request.args.get("page", 1, type=int)

    # ── Cloud docs ─────────────────────────────────────────────────────────
    connections = CloudConnection.query.filter_by(user_id=current_user.id).all()
    conn_ids = [c.id for c in connections]
    conn_map = {c.id: c for c in connections}

    cloud_q = CloudDocument.query.filter(CloudDocument.connection_id.in_(conn_ids))
    if provider_filter:
        prov_ids = [c.id for c in connections if c.provider == provider_filter]
        cloud_q = cloud_q.filter(CloudDocument.connection_id.in_(prov_ids))
    if tag_filter:
        cloud_q = cloud_q.join(CloudDocument.tags).filter(Tag.name == tag_filter)
    if search_query:
        cloud_q = cloud_q.filter(CloudDocument.name.ilike(f"%{search_query}%"))

    # ── Scraper articles ───────────────────────────────────────────────────
    # Bug L10 — l'ancien code faisait deux SELECT identiques pour
    # construire scraper_ids et scraper_map. Une seule passe suffit.
    user_scrapers = UserScraper.query.filter_by(user_id=current_user.id).all()
    scraper_ids = [s.id for s in user_scrapers]
    scraper_map = {s.id: s for s in user_scrapers}

    scraper_q = UserScraperArticle.query.filter(
        UserScraperArticle.user_id == current_user.id
    )
    if tag_filter:
        scraper_q = scraper_q.join(UserScraperArticle.tags).filter(Tag.name == tag_filter)
    if search_query:
        scraper_q = scraper_q.filter(UserScraperArticle.title.ilike(f"%{search_query}%"))
    if provider_filter:
        # provider filter only applies to cloud — suppress scraper results
        scraper_q = scraper_q.filter(False)

    # ── Stats (always full counts, ignoring filters) ────────────────────────
    total_cloud = CloudDocument.query.filter(
        CloudDocument.connection_id.in_(conn_ids)
    ).count()
    indexed_cloud = CloudDocument.query.filter(
        CloudDocument.connection_id.in_(conn_ids),
        CloudDocument.chunk_count > 0,
    ).count()
    total_scrapers = UserScraperArticle.query.filter_by(
        user_id=current_user.id
    ).count()
    indexed_scrapers = UserScraperArticle.query.filter(
        UserScraperArticle.user_id == current_user.id,
        UserScraperArticle.chunk_count > 0,
    ).count()

    # ── Pagination by source ───────────────────────────────────────────────
    if source_filter == "scraper":
        art_page = (
            scraper_q
            .options(selectinload(UserScraperArticle.tags))
            .order_by(UserScraperArticle.published_at.is_(None).asc(), UserScraperArticle.published_at.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        docs = [_DocItem.from_scraper(a, scraper_map.get(a.scraper_id)) for a in art_page.items]
        pagination = art_page

    elif source_filter == "cloud":
        cloud_page = (
            cloud_q
            .options(selectinload(CloudDocument.tags))
            .order_by(CloudDocument.name)
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        docs = [_DocItem.from_cloud(d, conn_map.get(d.connection_id)) for d in cloud_page.items]
        pagination = cloud_page

    else:
        # All: merge cloud + scrapers, sort by indexed_at desc, manual pagination
        cloud_items = (
            cloud_q.options(selectinload(CloudDocument.tags)).all()
        )
        scraper_items = (
            scraper_q.options(selectinload(UserScraperArticle.tags)).all()
        )
        merged = (
            [_DocItem.from_cloud(d, conn_map.get(d.connection_id)) for d in cloud_items]
            + [_DocItem.from_scraper(a, scraper_map.get(a.scraper_id)) for a in scraper_items]
        )
        merged.sort(key=lambda x: (x.indexed_at is None, x.indexed_at), reverse=True)
        total = len(merged)
        offset = (page - 1) * per_page
        docs = merged[offset: offset + per_page]
        pagination = _ManualPagination(total=total, page=page, per_page=per_page)

    # ── Available tags (union of cloud + scraper tags for filter UI) ────────
    all_cloud_tags = (
        CloudDocument.query
        .filter(CloudDocument.connection_id.in_(conn_ids))
        .options(selectinload(CloudDocument.tags))
        .all()
    ) if source_filter != "scraper" else []

    all_scraper_tags = (
        UserScraperArticle.query
        .filter_by(user_id=current_user.id)
        .options(selectinload(UserScraperArticle.tags))
        .all()
    ) if source_filter != "cloud" else []

    seen_tag_ids: set = set()
    available_tags = []
    for doc in list(all_cloud_tags) + list(all_scraper_tags):
        for tag in doc.tags:
            if tag.id not in seen_tag_ids:
                seen_tag_ids.add(tag.id)
                available_tags.append(tag)
    available_tags.sort(key=lambda t: t.name)

    return render_template(
        "user/library.html",
        docs=docs,
        pagination=pagination,
        per_page=per_page,
        connections=connections,
        conn_map=conn_map,
        search_query=search_query,
        provider_filter=provider_filter,
        tag_filter=tag_filter,
        source_filter=source_filter,
        available_tags=available_tags,
        total_cloud=total_cloud,
        indexed_cloud=indexed_cloud,
        total_scrapers=total_scrapers,
        indexed_scrapers=indexed_scrapers,
    )


class _ManualPagination:
    """Minimal pagination object for Python-level sliced results."""

    def __init__(self, total: int, page: int, per_page: int):
        self.total = total
        self.page = page
        self.per_page = per_page
        self.pages = max(1, -(-total // per_page))  # ceil division
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1
        self.next_num = page + 1

    def iter_pages(self, left_edge=1, right_edge=1, left_current=2, right_current=2):
        last = 0
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or (self.page - left_current - 1 < num < self.page + right_current)
                or num > self.pages - right_edge
            ):
                if last + 1 != num:
                    yield None
                yield num
                last = num


@user_library_bp.route("/ma-bibliotheque/preview/<doc_type>/<int:doc_id>")
@login_required
@_require_subscriber
def doc_preview(doc_type: str, doc_id: int):
    """Return JSON preview content for a cloud doc or scraper article."""
    if doc_type == "cloud":
        doc = db.session.get(CloudDocument, doc_id)
        conn = db.session.get(CloudConnection, doc.connection_id) if doc else None
        if not doc or not conn or conn.user_id != current_user.id:
            return jsonify({"error": "Not found"}), 404

        chunks = []
        if doc.chunk_count > 0:
            try:
                from app.services.rag_service import _get_user_collection
                col = _get_user_collection(current_user.id)
                ids = [f"clouddoc_{doc_id}_chunk_{i}" for i in range(doc.chunk_count)]
                result = col.get(ids=ids, include=["documents"])
                chunks = result.get("documents") or []
            except Exception as e:
                logger.warning("Preview ChromaDB fetch failed doc=%s: %s", doc_id, e)

        return jsonify({
            "type": "cloud",
            "name": doc.name,
            "provider": conn.provider_label,
            "mime_type": doc.mime_type,
            "file_size": doc.file_size,
            "indexed_at": doc.indexed_at.strftime("%d/%m/%Y") if doc.indexed_at else None,
            "chunk_count": doc.chunk_count,
            "chunks": chunks[:10],  # cap at 10 chunks for the panel
        })

    elif doc_type == "scraper":
        article = db.session.get(UserScraperArticle, doc_id)
        if not article or article.user_id != current_user.id:
            return jsonify({"error": "Not found"}), 404
        scraper = db.session.get(UserScraper, article.scraper_id)
        return jsonify({
            "type": "scraper",
            "title": article.title,
            "url": article.url,
            "scraper_name": scraper.name if scraper else "",
            "published_at": article.published_at.strftime("%d/%m/%Y") if article.published_at else None,
            "indexed_at": article.indexed_at.strftime("%d/%m/%Y") if article.indexed_at else None,
            "chunk_count": article.chunk_count,
            "content": (article.content or "")[:4000],  # first 4000 chars
        })

    return jsonify({"error": "Invalid type"}), 400


@user_library_bp.route("/ma-bibliotheque/cloud-doc/remove-index", methods=["POST"])
@login_required
@_require_subscriber
def remove_cloud_doc_from_index():
    from app.services.rag_service import remove_cloud_document_from_index

    data = request.get_json(silent=True) or {}
    doc_id = data.get("doc_id")
    if not doc_id:
        return jsonify({"success": False, "message": "doc_id manquant."}), 400

    doc = db.session.get(CloudDocument, doc_id)
    conn = db.session.get(CloudConnection, doc.connection_id) if doc else None
    if not doc or not conn or conn.user_id != current_user.id:
        return jsonify({"success": False, "message": "Document introuvable."}), 404

    if doc.chunk_count > 0:
        try:
            remove_cloud_document_from_index(current_user.id, doc.id, doc.chunk_count)
        except Exception as e:
            logger.error("remove_cloud_doc_from_index %s: %s", doc_id, e)

    # Reset sync_cursor so the next sync is a full sync and re-indexes this file.
    # OAuth providers (Google Drive, OneDrive…) only expose changed files in delta
    # mode; without this reset they would never re-process an unmodified file.
    conn.sync_cursor = None
    db.session.delete(doc)
    db.session.commit()
    return jsonify({"success": True})


@user_library_bp.route("/ma-bibliotheque/chat/<doc_type>/<int:doc_id>")
@login_required
@_require_subscriber
def doc_chat(doc_type: str, doc_id: int):
    """Render a chat page scoped to a single document.

    `doc_type` is one of "user", "cloud", "org" — mapped to the
    document_scope token search() expects ("user_doc:42" etc.). The
    template renders the same RAG search UI but with the multi-doc
    selectors hidden and document_scope wired into every request.

    Pro+ only — Standard users see the chat button hidden in the
    template but get an explicit 402 here if they navigate directly.
    """
    from flask import flash, redirect, url_for

    from app.models.user_document import UserDocument
    from app.utils.deployment_mode import is_on_prem
    is_pro_plus = (
        is_on_prem()
        or current_user.is_admin
        or current_user.is_pro_subscriber
    )
    if not is_pro_plus:
        flash("Le chat sur un document unique est réservé au plan Pro et supérieurs.", "warning")
        return redirect(url_for("user_library.personal_library"))

    if doc_type == "cloud":
        doc = db.session.get(CloudDocument, doc_id)
        conn = db.session.get(CloudConnection, doc.connection_id) if doc else None
        if not doc or not conn or conn.user_id != current_user.id:
            return ("Not found", 404)
        title = doc.name
        scope_token = f"cloud_doc:{doc_id}"
    elif doc_type == "user":
        doc = db.session.get(UserDocument, doc_id)
        if not doc or doc.user_id != current_user.id:
            return ("Not found", 404)
        title = doc.display_title
        scope_token = f"user_doc:{doc_id}"
    else:
        return ("Invalid doc_type", 400)

    return render_template(
        "user/rag_search.html",
        document_context={
            "scope_token": scope_token,
            "title": title,
            "doc_type": doc_type,
            "doc_id": doc_id,
        },
        initial_query="",
        initial_scope="personal",
        tags=[],
        stats={"indexed_sections": 0},
    )
