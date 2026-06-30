from flask import Blueprint, current_app, jsonify, redirect
from flask import request as flask_request
from flask import url_for
from flask_login import current_user, login_required

from app.ext import limiter
from app.queue import get_queue
from app.utils.flask_limiter import conditional_limit
from app.utils.logger import get_logger


# Bug N10 (audit round 2) — les routes GET de listing (history, saved,
# alerts) renvoyaient l'historique de queries déchiffré sans cap, à
# concurrence libre. Un attaquant hijack session pouvait exfiltrer
# l'historique entier en ~quelques secondes. 30/min/user couvre les
# usages légitimes (refresh UI tab) sans payer une charge mémoire forte.
def _listing_key():
    return f"rag_list:{current_user.id}" if current_user.is_authenticated else "rag_list:anon"


logger = get_logger("user_rag", subfolder="routes")

user_rag_bp = Blueprint("user_rag", __name__)


@user_rag_bp.route("/recherche")
@login_required
def rag_search_page():
    return redirect(url_for("user.dashboard"))


def _is_pro() -> bool:
    return current_user.is_authenticated and (current_user.is_admin or current_user.is_pro_subscriber)


def _is_pro_or_above() -> bool:
    """True for Pro / Team / Enterprise / admins. The three RAG features
    that are pro-gated (multi-turn, strict toggle, single-doc chat) all
    use this predicate.

    Sovereign / on-premise deployments bill out-of-band — there's no
    Stripe subscription and tier is not a meaningful axis. Treat every
    confirmed user as Pro+ in that mode so the gated UI / endpoints
    work for the customer that paid for the install.
    """
    if not current_user.is_authenticated:
        return False
    # On-prem deployments (sovereign + hybrid) don't have Stripe plans —
    # everyone gets full Pro access (the licence covers the operator).
    from app.utils.deployment_mode import is_on_prem
    if is_on_prem():
        return True
    return bool(current_user.is_admin or current_user.is_pro_subscriber)


# Per-plan RAG rate caps (pivot Free/Pro/Enterprise). The DAILY cap is what
# protects margin on Pro/Enterprise (Mistral included, ~0.005 €/request); Free
# is BYOK so the user pays their own AI cost — its limits are generous and only
# act as light anti-abuse. The per-minute cap absorbs burst usage:
#   Free       60/day  (BYOK — their cost; generous, anti-abuse only)
#   Pro       120/day  (Mistral included)
#   Enterprise 300/day (Mistral included, dedicated stack)
_RAG_LIMITS_FREE =       "10 per minute; 60 per day"
_RAG_LIMITS_PRO =        "15 per minute; 120 per day"
_RAG_LIMITS_ADMIN =      "1000 per minute"


def _rag_rate_limit() -> str:
    """Return the per-request rate-limit string based on the user's plan.

    Called by flask-limiter on every hit; switches the active limits based on
    the authenticated user's tier without needing multiple decorators or an
    `exempt_when` (which would let abusive Pro users uncap entirely).

    Pivot Free/Pro/Enterprise : enterprise d'abord (org enterprise ou
    abonnement enterprise), puis Pro (org team / premium_pro), sinon Free.
    """
    if not current_user.is_authenticated:
        return _RAG_LIMITS_FREE
    if current_user.is_admin:
        return _RAG_LIMITS_ADMIN
    if current_user.is_pro_subscriber:
        return _RAG_LIMITS_PRO
    return _RAG_LIMITS_FREE


@user_rag_bp.route("/recherche/query", methods=["POST"])
@login_required
@limiter.limit(_rag_rate_limit, key_func=lambda: str(current_user.id))
def rag_query():
    data = flask_request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    tag_filter = (data.get("tag") or "").strip() or None
    date_from = (data.get("date_from") or "").strip() or None
    date_to = (data.get("date_to") or "").strip() or None
    # Default = strict mode. The toggle is opt-in and only loosens the
    # source-only constraint when the client sends it explicitly.
    strict_mode = bool(data.get("strict_mode", True))
    # Multi-turn: optional conversation_id. When set, the task fetches the
    # prior turns to feed continuity into the prompt and appends both the
    # user message and the assistant answer as new turns.
    conversation_id = data.get("conversation_id")
    try:
        conversation_id = int(conversation_id) if conversation_id else None
    except (TypeError, ValueError):
        conversation_id = None
    # Single-document chat (#7): "user_doc:42" / "cloud_doc:42" / "org_doc:42"
    # restricts retrieval to that one document. Validated in search() — the
    # route just checks the shape so a malformed value can't reach Chroma.
    document_scope = (data.get("document_scope") or "").strip() or None
    if document_scope:
        import re as _re_ds
        if not _re_ds.match(r"^(user_doc|cloud_doc|org_doc):\d+$", document_scope):
            return jsonify({"error": "document_scope invalide"}), 400

    # ── Pro+ feature gating ───────────────────────────────────────────────
    # Multi-turn, strict toggle, and single-document chat are reserved to
    # Pro / Team / Enterprise. Standard users get the basic search:
    # silent downgrade rather than a hard error so the search still works.
    if not _is_pro_or_above():
        if conversation_id is not None:
            return jsonify({
                "error": "La fonctionnalité de conversation multi-tours est réservée au plan Pro et supérieurs."
            }), 402  # Payment Required — clean signal for the UI
        if document_scope:
            return jsonify({
                "error": "Le chat sur un document unique est réservé au plan Pro et supérieurs."
            }), 402
        # Strict toggle: silently force strict=True. The user can still
        # ask without the relaxed flag — the answer just stays strict.
        if not strict_mode:
            strict_mode = True

    if not query:
        return jsonify({"error": "Question vide"}), 400
    if len(query) > 400:
        return jsonify({"error": "Question trop longue (400 caractères max)"}), 400

    # Validate date inputs server-side (UI sends YYYY-MM-DD; reject anything else
    # so a malformed string can't reach the search() ISO parser unfiltered).
    import re as _re_d
    if date_from and not _re_d.match(r"^\d{4}-\d{2}-\d{2}$", date_from):
        return jsonify({"error": "date_from invalide (YYYY-MM-DD attendu)"}), 400
    if date_to and not _re_d.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
        return jsonify({"error": "date_to invalide (YYYY-MM-DD attendu)"}), 400

    # Resolve first-person pronouns to the user's real name so the LLM and
    # vector search can match documents that reference the user by name.
    user_full_name = " ".join(
        p for p in [current_user.firstname, current_user.lastname] if p
    ).strip()
    if user_full_name:
        import re as _re

        # Replace "je" (standalone word) with the user's full name.
        # Prepend an identity note so the LLM interprets all first-person
        # references correctly without fragile grammar rewrites.
        if _re.search(r"\bje\b|\bmes\b|\bmon\b|\bma\b|\bme\b", query, flags=_re.IGNORECASE):
            query = f"[L'utilisateur s'appelle {user_full_name}] {query}"

    scope = (data.get("scope") or "personal").strip()
    if scope not in ("shared", "personal", "scrapers", "mine", "shared_docs", "both", "all", "org"):
        scope = "personal"

    org_id = None
    if scope == "org":
        scope = "personal"

    queue_name = "myeline-pro" if _is_pro() else "myeline"
    try:
        q = get_queue(current_app._get_current_object(), queue_name)
        from app.tasks import rag_query_task
        job = q.enqueue(
            rag_query_task,
            args=(current_user.id, query, tag_filter, scope, org_id),
            kwargs={
                "date_from": date_from, "date_to": date_to,
                "strict_mode": strict_mode,
                "conversation_id": conversation_id,
                "document_scope": document_scope,
            },
            job_timeout=120,
            result_ttl=300,
        )
        return jsonify({"job_id": job.id})
    except Exception:
        logger.error("Erreur file d'attente RAG user_id=%s", current_user.id, exc_info=True)
        return jsonify({"error": "Service temporairement indisponible."}), 500


@user_rag_bp.route("/recherche/status/<job_id>", methods=["GET"])
@login_required
def rag_status(job_id):
    from redis import Redis
    from rq.job import Job

    redis_conn = Redis.from_url(current_app.config["REDIS_URL"])
    try:
        job = Job.fetch(job_id, connection=redis_conn)
    except Exception:
        return jsonify({"status": "not_found"}), 404

    # Validate ownership: job.args[0] is user_id (see rag_query enqueue call).
    # Return 404 (not 403) to avoid leaking whether the job exists.
    if not job.args or job.args[0] != current_user.id:
        return jsonify({"status": "not_found"}), 404

    status = job.get_status()
    if status == "finished":
        result = job.result
        if isinstance(result, dict) and "error" in result:
            return jsonify({"status": "failed", "error": result["error"]})
        return jsonify({"status": "finished", "result": result})
    elif status == "failed":
        return jsonify({"status": "failed", "error": "La requête a échoué."}), 500
    else:
        # `stage` is set by rag_query_task as it walks the pipeline (embed →
        # retrieve → rerank → synth) so the UI can show a humane milestone
        # instead of a silent spinner. Stage is best-effort: missing key just
        # means the job hasn't reached the first checkpoint yet.
        stage = (job.meta or {}).get("stage")
        payload = {"status": str(status)}
        if stage:
            payload["stage"] = stage
        return jsonify(payload)


# ── RAG Feedback ──────────────────────────────────────────────────────────────

@user_rag_bp.route("/recherche/feedback", methods=["POST"])
@login_required
def rag_feedback():
    from app.ext import db
    from app.models.rag_feedback import RagFeedback

    data = flask_request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    rating = (data.get("rating") or "").strip()
    comment = (data.get("comment") or "").strip() or None
    answer_excerpt = (data.get("answer_excerpt") or "").strip()[:1000] or None
    sources_count = data.get("sources_count")
    scope = (data.get("scope") or "").strip() or None

    if not query or rating not in ("thumbs_up", "thumbs_down"):
        return jsonify({"error": "Paramètres invalides"}), 400

    fb = RagFeedback(
        user_id=current_user.id,
        query=query[:400],
        answer_excerpt=answer_excerpt,
        rating=rating,
        comment=comment,
        sources_count=sources_count,
        scope=scope,
    )
    db.session.add(fb)
    db.session.commit()
    return jsonify({"ok": True})


# ── Query history ─────────────────────────────────────────────────────────────

@user_rag_bp.route("/recherche/history", methods=["GET"])
@login_required
@conditional_limit("30 per minute", key_func=_listing_key)  # Bug N10
def rag_history():
    from app.ext import db
    from app.models.query_history import QueryHistory

    if not _is_pro_or_above():
        return jsonify({"error": "Cette fonctionnalité est réservée au plan Pro et supérieurs."}), 402

    # NOTE: QueryHistory.query is the encrypted column, not the
    # Flask-SQLAlchemy Query accessor — go through the session.
    rows = (
        db.session.query(QueryHistory)
        .filter_by(user_id=current_user.id)
        .order_by(QueryHistory.executed_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([
        {
            "id": r.id,
            "query": r.query,
            "scope": r.scope,
            "sources_count": r.sources_count,
            "executed_at": r.executed_at.isoformat(),
        }
        for r in rows
    ])


# ── Saved queries ─────────────────────────────────────────────────────────────

@user_rag_bp.route("/recherche/saved", methods=["GET"])
@conditional_limit("30 per minute", key_func=_listing_key)  # Bug N10
@login_required
def rag_saved_list():
    from app.ext import db
    from app.models.query_history import SavedQuery

    if not _is_pro_or_above():
        return jsonify({"error": "Cette fonctionnalité est réservée au plan Pro et supérieurs."}), 402

    # See rag_history above — SavedQuery.query is the encrypted column.
    rows = (
        db.session.query(SavedQuery)
        .filter_by(user_id=current_user.id)
        .order_by(SavedQuery.created_at.desc())
        .all()
    )
    return jsonify([
        {
            "id": r.id,
            "name": r.name,
            "query": r.query,
            "scope": r.scope,
            "tag_filter": r.tag_filter,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ])


@user_rag_bp.route("/recherche/saved", methods=["POST"])
@login_required
def rag_save_query():
    from app.ext import db
    from app.models.query_history import SavedQuery

    if not _is_pro_or_above():
        return jsonify({"error": "Cette fonctionnalité est réservée au plan Pro et supérieurs."}), 402

    data = flask_request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    name = (data.get("name") or query[:60]).strip()
    scope = (data.get("scope") or "personal").strip()
    tag_filter = (data.get("tag_filter") or "").strip() or None

    if not query:
        return jsonify({"error": "Requête vide"}), 400
    # Bug M18 — scope n'était pas validé : n'importe quelle string était
    # persistée et provoquait des comportements aberrants au load (le
    # rag_query lui ne consomme que personal/org/shared).
    if scope not in ("personal", "org", "shared"):
        return jsonify({"error": f"Scope invalide : {scope!r}"}), 400
    if len(name) > 255:
        name = name[:255]

    sq = SavedQuery(
        user_id=current_user.id,
        name=name,
        query=query[:400],
        scope=scope,
        tag_filter=tag_filter,
    )
    db.session.add(sq)
    db.session.commit()
    return jsonify({"ok": True, "id": sq.id, "name": sq.name})


@user_rag_bp.route("/recherche/saved/<int:sq_id>", methods=["DELETE"])
@login_required
def rag_delete_saved(sq_id):
    from app.ext import db
    from app.models.query_history import SavedQuery

    sq = db.session.query(SavedQuery).filter_by(id=sq_id, user_id=current_user.id).first_or_404()
    db.session.delete(sq)
    db.session.commit()
    return jsonify({"ok": True})


# ── Watch alerts ──────────────────────────────────────────────────────────────

@user_rag_bp.route("/recherche/alerts", methods=["GET"])
@conditional_limit("30 per minute", key_func=_listing_key)  # Bug N10
@login_required
def rag_alerts_list():
    from app.models.watch_alert import WatchAlert

    rows = (
        WatchAlert.query
        .filter_by(user_id=current_user.id)
        .order_by(WatchAlert.created_at.desc())
        .all()
    )
    return jsonify([
        {
            "id": r.id,
            "keyword": r.keyword,
            "scope": r.scope,
            "frequency": r.frequency,
            "is_active": r.is_active,
            "last_notified_at": r.last_notified_at.isoformat() if r.last_notified_at else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ])


@user_rag_bp.route("/recherche/alerts", methods=["POST"])
@login_required
def rag_create_alert():
    from app.ext import db
    from app.models.watch_alert import WatchAlert

    data = flask_request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    scope = (data.get("scope") or "all").strip()
    frequency = (data.get("frequency") or "daily").strip()

    if not keyword or len(keyword) > 255:
        return jsonify({"error": "Mot-clé invalide"}), 400
    if scope not in ("shared", "personal", "scrapers", "all"):
        scope = "all"
    if frequency not in ("daily", "weekly"):
        frequency = "daily"

    # Per-plan cap on active alerts. Standard has no access at all,
    # Pro = 20, Team = 50, Enterprise = 200.
    if not _is_pro_or_above():
        return jsonify({
            "error": "Les alertes par mots-clés sont réservées au plan Pro et supérieurs."
        }), 402
    if current_user.is_admin:
        max_alerts = 1000
    else:
        max_alerts = 20  # Pro
    count = WatchAlert.query.filter_by(user_id=current_user.id, is_active=True).count()
    if count >= max_alerts:
        return jsonify({"error": f"Limite de {max_alerts} alertes actives atteinte"}), 400

    alert = WatchAlert(
        user_id=current_user.id,
        keyword=keyword,
        scope=scope,
        frequency=frequency,
    )
    db.session.add(alert)
    db.session.commit()
    return jsonify({"ok": True, "id": alert.id})


@user_rag_bp.route("/recherche/alerts/<int:alert_id>", methods=["DELETE"])
@login_required
def rag_delete_alert(alert_id):
    from app.ext import db
    from app.models.watch_alert import WatchAlert

    alert = WatchAlert.query.filter_by(id=alert_id, user_id=current_user.id).first_or_404()
    db.session.delete(alert)
    db.session.commit()
    return jsonify({"ok": True})


@user_rag_bp.route("/recherche/alerts/<int:alert_id>/toggle", methods=["POST"])
@login_required
def rag_toggle_alert(alert_id):
    from app.ext import db
    from app.models.watch_alert import WatchAlert

    alert = WatchAlert.query.filter_by(id=alert_id, user_id=current_user.id).first_or_404()
    alert.is_active = not alert.is_active
    db.session.commit()
    return jsonify({"ok": True, "is_active": alert.is_active})


# ── Conversations (multi-turn RAG) ───────────────────────────────────────────
# Each conversation is a thread of alternating user/assistant turns.
# The query endpoint above creates one implicitly on the first turn and
# appends to it on every subsequent call when conversation_id is set.

@user_rag_bp.route("/recherche/conversations", methods=["GET"])
@login_required
def rag_list_conversations():
    from app.models.conversation import Conversation

    rows = (
        Conversation.query
        .filter_by(user_id=current_user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(50)
        .all()
    )
    return jsonify({
        "conversations": [
            {
                "id": c.id,
                "title": c.title,
                "scope": c.scope,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in rows
        ]
    })


@user_rag_bp.route("/recherche/conversations/<int:conv_id>", methods=["GET"])
@login_required
def rag_get_conversation(conv_id):
    import json as _json

    from app.models.conversation import Conversation

    conv = Conversation.query.filter_by(id=conv_id, user_id=current_user.id).first_or_404()
    turns = []
    for t in conv.turns:
        turn = {
            "id": t.id,
            "role": t.role,
            "content": t.content,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        if t.sources_json:
            try:
                turn["payload"] = _json.loads(t.sources_json)
            except Exception:
                turn["payload"] = None
        turns.append(turn)
    return jsonify({
        "id": conv.id,
        "title": conv.title,
        "scope": conv.scope,
        "tag_filter": conv.tag_filter,
        "strict_mode": bool(conv.strict_mode),
        "document_scope": conv.document_scope,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "turns": turns,
    })


@user_rag_bp.route("/recherche/conversations/<int:conv_id>", methods=["DELETE"])
@login_required
def rag_delete_conversation(conv_id):
    from app.ext import db
    from app.models.conversation import Conversation

    conv = Conversation.query.filter_by(id=conv_id, user_id=current_user.id).first_or_404()
    db.session.delete(conv)
    db.session.commit()
    return jsonify({"ok": True})
