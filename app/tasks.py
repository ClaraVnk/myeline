"""Background tasks executed by the rq worker.

Each task creates its own Flask app instance so it runs independently of the
web process.  All parameters must be JSON-serialisable (IDs, strings, lists).
"""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import as_completed

from app.utils.logger import get_logger


logger = get_logger("tasks", subfolder="")


def sync_cloud_connection_task(connection_id: int) -> dict:
    """Sync a single cloud connection (one RQ job per connection).

    Dispatches to the correct service module based on the connection's provider.
    """
    app = _make_app()
    with app.app_context():
        try:
            from app.ext import db
            from app.models.cloud_connection import CloudConnection

            conn = db.session.get(CloudConnection, connection_id)
            if not conn:
                logger.error("sync_cloud_connection_task: connection %s not found", connection_id)
                return {"indexed": 0, "errors": 1}

            provider = conn.provider
            if provider == "gdrive":
                from app.services.gdrive_service import sync_connection
            elif provider == "onedrive":
                from app.services.onedrive_service import sync_connection
            elif provider == "dropbox":
                from app.services.dropbox_service import sync_connection
            elif provider == "kdrive":
                from app.services.kdrive_service import sync_connection
            elif provider == "zotero":
                from app.services.zotero_service import sync_connection
            elif provider == "s3":
                from app.services.s3_service import sync_connection
            else:
                logger.error("sync_cloud_connection_task: unknown provider %s for conn %s", provider, connection_id)
                return {"indexed": 0, "errors": 1}

            return sync_connection(connection_id)
        except Exception as e:
            logger.error("sync_cloud_connection_task conn=%s: %s", connection_id, e)
            return {"indexed": 0, "errors": 1}


def scrape_source_task(scraper_id: int) -> dict:
    """Scrape a single RSS/web source (one RQ job per scraper)."""
    app = _make_app()
    with app.app_context():
        try:
            from app.services.user_scraper_service import scrape_source
            new_articles, err = scrape_source(scraper_id)
            return {"new_articles": new_articles, "error": err}
        except Exception as e:
            logger.error("scrape_source_task scraper=%s: %s", scraper_id, e)
            return {"new_articles": 0, "error": True}


def rag_query_task(
    user_id: int, query: str, tag_filter, scope: str,
    *, date_from: str = None, date_to: str = None, strict_mode: bool = True,
    conversation_id: int = None, document_scope: str = None,
) -> dict:
    """Run a RAG search as a background task.

    When `conversation_id` is provided (and owned by the user), prior turns
    are loaded as continuity history and the new user/assistant pair is
    appended. Otherwise, a fresh conversation is created on the first turn
    and its id is returned in the result for the client to keep using.
    """
    app = _make_app()
    with app.app_context():
        outcome = "success"

        # Progress reporter: writes the current pipeline stage into the RQ
        # job meta so the frontend can poll /recherche/status and show a
        # humane milestone ("searching the knowledge base…", "writing the
        # answer…") instead of a silent 5-30 s spinner. Failures are
        # swallowed so a broken meta channel can never block a search.
        from rq import get_current_job

        _job = get_current_job()

        def _publish_stage(name: str) -> None:
            if _job is None:
                return
            try:
                _job.meta["stage"] = name
                _job.save_meta()
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("save_meta(stage=%s) failed: %s", name, exc)

        try:
            from app.services.rag_service import search

            history, conv = _load_conversation_history(user_id, conversation_id)

            result = search(
                query, tag_filter=tag_filter, scope=scope,
                user_id=user_id,
                date_from=date_from, date_to=date_to,
                strict_mode=strict_mode,
                history=history,
                document_scope=document_scope,
                on_stage=_publish_stage,
            )

            if not (result.get("sources") or []):
                outcome = "empty"

            conv_id_out = _persist_conversation_turn(
                user_id=user_id, conv=conv, query=query, scope=scope,
                tag_filter=tag_filter, strict_mode=strict_mode,
                result=result, document_scope=document_scope,
            )
            if conv_id_out:
                result["conversation_id"] = conv_id_out

            _log_query_history(user_id, query, scope, result)
            return result
        except Exception as e:
            outcome = "error"
            logger.error(f"Erreur RAG task (user {user_id}): {e}")
            return {"error": "Erreur interne. Veuillez réessayer."}
        finally:
            try:
                from app.utils.metrics import rag_queries_total
                rag_queries_total.labels(scope=scope or "shared", outcome=outcome).inc()
            except Exception:
                pass


def _load_conversation_history(user_id, conversation_id):
    """Return (history_list, conv_or_None). history is the last 6 turns."""
    if not conversation_id:
        return [], None
    try:
        from app.models.conversation import Conversation
        conv = (
            Conversation.query
            .filter_by(id=conversation_id, user_id=user_id)
            .first()
        )
        if not conv:
            return [], None
        # Bug M4 (audit round 2) — `order_by(None)` clear l'ordre par
        # défaut du model : DB-dependent (typiquement insertion order
        # InnoDB, mais non garanti). Le slice `recent[-6:]` derrière
        # ordonnance Python-side sur un set non-trié → pour une conv
        # > 6 turns, l'historique injecté en prompt LLM devenait
        # aléatoire. Trier explicitement par created_at desc, limit 6,
        # reverser pour ordre chrono ASC.
        from app.models.conversation import ConversationTurn
        recent = (
            conv.turns.order_by(ConversationTurn.created_at.desc()).limit(6).all()
        )
        history = [{"role": t.role, "content": t.content} for t in reversed(recent)]
        return history, conv
    except Exception as exc:
        logger.warning("Failed to load conversation %s: %s", conversation_id, exc)
        return [], None


def _persist_conversation_turn(
    *, user_id, conv, query, scope, tag_filter, strict_mode, result,
    document_scope=None,
):
    """Append the user + assistant turns; create the conversation if needed.

    Returns the conversation id (so the route can echo it back to the
    client and keep the multi-turn loop going).
    """
    import json as _json

    try:
        from app.ext import db
        from app.models.conversation import Conversation, ConversationTurn
        from app.utils.time_utils import utcnow

        if conv is None:
            # Title = first ~60 chars of the user's first question.
            title = (query or "Conversation").strip()[:60]
            conv = Conversation(
                user_id=user_id, title=title, scope=scope,
                tag_filter=tag_filter, strict_mode=bool(strict_mode),
                document_scope=document_scope,
            )
            db.session.add(conv)
            db.session.flush()  # assign conv.id

        # Compact assistant payload — keep only what the UI needs to re-render.
        payload = {
            "sources": result.get("sources") or [],
        }
        if "freshness" in result:
            payload["freshness"] = result["freshness"]
        if "ai_warning" in result:
            payload["ai_warning"] = result["ai_warning"]

        db.session.add(ConversationTurn(
            conversation_id=conv.id, role="user", content=query,
        ))
        db.session.add(ConversationTurn(
            conversation_id=conv.id, role="assistant",
            content=result.get("answer", "") or "",
            sources_json=_json.dumps(payload, ensure_ascii=False),
        ))
        conv.updated_at = utcnow()
        db.session.commit()
        return conv.id
    except Exception as exc:
        logger.warning("Failed to persist conversation turn: %s", exc)
        return None


def _log_query_history(user_id: int, query: str, scope: str, result: dict) -> None:
    """Persist a QueryHistory row; silently swallows errors to never block the RAG task."""
    try:
        from app.ext import db
        from app.models.query_history import QueryHistory
        from app.utils.time_utils import utcnow

        answer = result.get("answer", "") or ""
        sources = result.get("sources") or []
        entry = QueryHistory(
            user_id=user_id,
            query=query[:400],
            scope=scope,
            answer_excerpt=answer[:500] if answer else None,
            sources_count=len(sources),
            executed_at=utcnow(),
        )
        db.session.add(entry)
        # NOTE: QueryHistory has a `query` column that shadows
        # Flask-SQLAlchemy's Model.query accessor. Use db.session.query()
        # so we dispatch through the Session, not the Column descriptor.
        total = db.session.query(QueryHistory).filter_by(user_id=user_id).count()
        if total > 200:
            oldest_ids = (
                db.session.query(QueryHistory.id)
                .filter_by(user_id=user_id)
                .order_by(QueryHistory.executed_at.asc())
                .limit(total - 200)
                .subquery()
            )
            db.session.query(QueryHistory).filter(
                QueryHistory.id.in_(oldest_ids)
            ).delete(synchronize_session=False)
        db.session.commit()
    except Exception as e:
        logger.warning("Failed to log query history for user %s: %s", user_id, e)


def _make_app():
    from app import create_app
    return create_app()


def reindex_embeddings_task() -> dict:
    """Re-embed every ChromaDB chunk with the currently active embed model.

    Triggered by the admin embedder picker (Sovereign) after the new model
    is selected. Holds `myeline:embed_reindex_lock` in Redis so the
    picker UI can show "reindex in progress" and refuse a second submit.
    The lock is released in finally — even on exception — so a crashed
    worker can't permanently jam future switches.
    """
    import os as _os

    app = _make_app()
    with app.app_context():
        try:
            from sqlalchemy.exc import SQLAlchemyError

            from app.ext import db
            from app.models.cloud_connection import CloudDocument
            from app.models.newsletter import Newsletter
            from app.models.user_document import UserDocument
            from app.services.rag_service import (
                EmbedDimensionMismatch,
                _active_embed_model,
                _embed,
                _get_collection,
                _get_user_collection,
                assert_collection_compat_for_reindex,
            )
            from app.utils.text_extraction import chunk_text as _chunk_text

            active = _active_embed_model()
            logger.info("reindex_embeddings_task starting (active model=%s)", active)
            total = 0
            # Audit round 4 — A3-C3. Track collections we've already validated
            # so the pre-flight assertion runs once per collection instead of
            # once per document.
            _checked_cols: set = set()
            _skipped_cols: set = set()

            def _check_or_skip(col, scope: str) -> bool:
                """Return True if the collection is safe to write to. False
                means we logged an ERROR and the caller must skip this doc."""
                cid = id(col)
                if cid in _checked_cols:
                    return True
                if cid in _skipped_cols:
                    return False
                try:
                    assert_collection_compat_for_reindex(col)
                    _checked_cols.add(cid)
                    return True
                except EmbedDimensionMismatch as mismatch:
                    logger.error(
                        "reindex_dim_mismatch scope=%s collection=%s existing=%s requested=%s "
                        "— skipping all documents in this collection (operator must wipe first)",
                        scope, getattr(col, "name", "?"),
                        mismatch.existing, mismatch.requested,
                    )
                    _skipped_cols.add(cid)
                    return False

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
                except (SQLAlchemyError, IOError, OSError, ValueError) as exc:
                    logger.warning("reindex user_doc %s skipped: %s", d.id, exc)

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
                except (SQLAlchemyError, IOError, OSError, ValueError) as exc:
                    logger.warning("reindex cloud_doc %s skipped: %s", d.id, exc)

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
                except (SQLAlchemyError, IOError, OSError, ValueError) as exc:
                    logger.warning("reindex newsletter %s skipped: %s", nl.id, exc)

            logger.info("reindex_embeddings_task done: %d chunks re-embedded with %s",
                        total, active)
            return {"reindexed_chunks": total, "model": active}
        finally:
            # Always clear the lock so a crash doesn't permanently block
            # future switches via the admin UI.
            redis_url = _os.getenv("REDIS_URL")
            if redis_url:
                try:
                    from redis import Redis

                    from app.services.rag_service import _EMBED_REINDEX_LOCK_KEY
                    Redis.from_url(redis_url, socket_connect_timeout=1).delete(_EMBED_REINDEX_LOCK_KEY)
                except Exception:
                    pass


def broadcast_task(recipient_ids, subject, html_content, url_root):
    """Broadcast a free-form email to all eligible subscribers."""
    from flask import render_template, url_for

    from app.models.user import User
    from app.services.user_services import generate_unsubscribe_token
    from app.utils.logger import get_logger as _get_logger
    from app.utils.mailer import send_email_via_brevo_api as send_email

    _log = _get_logger("tasks", subfolder="")
    app = _make_app()
    with app.app_context():
        with app.test_request_context(base_url=url_root):
            try:
                users = User.query.filter(User.id.in_(recipient_ids)).all()
                tasks = []
                for u in users:
                    token = generate_unsubscribe_token(u)
                    unsub = url_for("user_tools.unsubscribe", token=token, _external=True)
                    rendered = render_template(
                        "emails/broadcast_email.html",
                        subject=subject,
                        html_content=html_content,
                        user=u,
                        unsubscribe_url=unsub,
                    )
                    tasks.append((u.email, rendered))

                def _send_one(t):
                    addr, body = t
                    try:
                        send_email(addr, subject, body)
                        return True
                    except Exception as e:
                        _log.error("Erreur broadcast %s: %s", addr, e)
                        return False

                # Bounded by `BROADCAST_TIMEOUT_SECONDS`. We submit individually
                # (rather than using executor.map) so that, on timeout, we can:
                #  - count the successes we DID get instead of discarding them
                #  - cancel pending futures (cancel_futures=True on shutdown)
                #    so the rq job exits promptly even if a Brevo POST hangs
                #    longer than the 30 s HTTP timeout for some reason.
                BROADCAST_TIMEOUT = 300
                successes = 0
                completed = 0
                pending_cancelled = 0
                executor = ThreadPoolExecutor(max_workers=10)
                try:
                    futures = [executor.submit(_send_one, t) for t in tasks]
                    try:
                        for fut in as_completed(futures, timeout=BROADCAST_TIMEOUT):
                            completed += 1
                            if fut.result():
                                successes += 1
                    except FuturesTimeoutError:
                        # Cancel anything not yet started; in-flight calls will
                        # finish on their own (capped by the Brevo HTTP timeout).
                        pending_cancelled = sum(1 for f in futures if f.cancel())
                        _log.error(
                            "Timeout broadcast après %ds (%d/%d completed, %d cancelled)",
                            BROADCAST_TIMEOUT, completed, len(futures), pending_cancelled,
                        )
                finally:
                    # Bug M7 (audit round 2) — wait=False permettait aux
                    # threads en cours (HTTP Brevo) de survivre au retour
                    # de la fonction RQ, conservant un app_context et
                    # une session SQLAlchemy non-removée. Sur worker RQ
                    # immédiatement recyclé, transactions zombie. Avec
                    # wait=True + cancel_futures, on attend les calls en
                    # cours (cap déjà au BROADCAST_TIMEOUT global du
                    # ThreadPoolExecutor.shutdown).
                    executor.shutdown(wait=True, cancel_futures=True)

                _log.info(
                    "Broadcast terminé : %d/%d envois réussis (%d annulés)",
                    successes, len(tasks), pending_cancelled,
                )
            except Exception:
                _log.error("Erreur worker broadcast", exc_info=True)
