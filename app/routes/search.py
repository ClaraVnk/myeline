from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user

from app.ext import db, limiter
from app.utils.logger import get_logger


logger = get_logger("search", subfolder="routes")

search_bp = Blueprint("search", __name__)


# A4-M2: the global MAX_CONTENT_LENGTH (16 MB) is sized for uploads, not
# for a 300-char public search query. Cap the body here so an anonymous
# caller can't burn parser CPU + DB writes via a megabyte-sized JSON tree.
_MAX_SEARCH_BODY_BYTES = 8 * 1024
# A4-M2: cap how many source cards we persist into SharedRagResult. The
# retrieval pipeline already returns ~5-15 sources; refuse to write
# tens of thousands of rows if the RAG layer ever returns a runaway list.
_MAX_SHARED_SOURCES = 50


def _search_key():
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return f"ip:{request.remote_addr}"


def _is_unlimited():
    return current_user.is_authenticated and (
        current_user.is_admin or current_user.is_subscriber
    )


@search_bp.route("/search", methods=["POST"])
@limiter.limit("1 per minute", key_func=_search_key, exempt_when=_is_unlimited)
def public_search():
    # Pivot Free/Pro : un abonnement Pro en `past_due` retombe simplement en
    # Free (accès de base, recherche en BYOK) — plus de blocage paiement ici.
    if request.content_length and request.content_length > _MAX_SEARCH_BODY_BYTES:
        return jsonify(error="Requête trop volumineuse."), 413

    # A4-M4: bound JSON nesting depth — /search is a public POST and
    # Python's json.loads only stops at the interpreter's recursion
    # limit (~1000), well above what any legitimate caller needs.
    from app.utils.json_safe import parse_json_bounded
    raw = request.get_data(cache=False)
    if not raw:
        data = {}
    else:
        try:
            data = parse_json_bounded(raw, max_depth=8)
        except Exception:
            return jsonify(error="Format de requête invalide."), 400
    if not isinstance(data, dict):
        return jsonify(error="Format de requête invalide."), 400
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify(error="Question vide."), 400
    if len(query) > 300:
        return jsonify(error="Question trop longue (300 caractères max)."), 400

    try:
        from app.models.shared_rag_result import SharedRagResult
        from app.services.rag_service import search
        result = search(query)

        sources = result.get("sources", [])
        if len(sources) > _MAX_SHARED_SOURCES:
            sources = sources[:_MAX_SHARED_SOURCES]
        confidence = (
            round(sum(s.get("relevance", 0) for s in sources) / len(sources))
            if sources else 0
        )

        shared = SharedRagResult.create(
            query=query,
            answer=result.get("answer", ""),
            sources=sources,
            confidence=confidence,
        )
        db.session.commit()
        result["share_token"] = shared.token

        if current_user.is_authenticated:
            logger.info("Search query user_id=%s", current_user.id)
        else:
            logger.info("Search query ip=%s", request.remote_addr)
        return jsonify(result)
    except Exception as e:
        db.session.rollback()
        logger.error("Public search error: %s", e)
        return jsonify(error="Erreur interne. Veuillez réessayer."), 500


@search_bp.route("/r/<token>")
def shared_result(token):
    from app.models.shared_rag_result import SharedRagResult
    result = db.session.execute(
        db.select(SharedRagResult).where(SharedRagResult.token == token)
    ).scalar_one_or_none()
    if result is None:
        from flask import abort
        abort(404)
    return render_template("search/shared_result.html", result=result)
