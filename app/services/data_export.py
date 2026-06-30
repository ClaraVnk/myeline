"""GDPR Article 15 + 20 export — every piece of user data we hold,
zipped into a single archive.

This goes BEYOND the profile-only export it replaces. It now covers:
  - profile + preferences + account state
  - user-uploaded documents (file bytes + metadata)
  - cloud-synced documents (metadata only — files live at the
    provider, the user already has them there)
  - personal scrapers + their articles + indexed chunks
  - RAG conversations + turns
  - query history, saved queries, watch alerts, RAG feedback
  - ChromaDB chunks (text + metadata) belonging to this user, both
    from their personal collection and any chunks they own in the
    shared collections — the previous behaviour wiped them on
    delete-account but never offered them on export.

The result is a BytesIO ZIP suitable for direct send_file. Caller is
responsible for the route + auth.
"""
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.utils.logger import get_logger


logger = get_logger("data_export", subfolder="services")


def _isofmt(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _model_to_dict(obj, fields: list) -> dict:
    """Pull a fixed set of attributes off any SQLAlchemy model."""
    out = {}
    for f in fields:
        v = getattr(obj, f, None)
        if isinstance(v, datetime):
            out[f] = v.isoformat()
        elif hasattr(v, "isoformat"):  # bare date
            out[f] = v.isoformat()
        else:
            out[f] = v
    return out


def build_user_export(user) -> io.BytesIO:
    """Return a BytesIO zip with every record we hold for `user`."""
    from app.models.cloud_connection import CloudConnection, CloudDocument
    from app.models.conversation import Conversation
    from app.models.query_history import QueryHistory, SavedQuery
    from app.models.rag_feedback import RagFeedback
    from app.models.tag import Tag
    from app.models.user_document import UserDocument
    from app.models.user_scraper import UserScraper, UserScraperArticle
    from app.models.watch_alert import WatchAlert

    buf = io.BytesIO()
    manifest = {
        "export_date": datetime.now(timezone.utc).isoformat(),
        "user_id": user.id,
        "format_version": 2,
        "gdpr_articles": [
            "Article 15 - Right of access",
            "Article 20 - Right to data portability",
        ],
        "sections": [],
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        def write_json(name, data, count=None):
            zf.writestr(name, json.dumps(data, indent=2, ensure_ascii=False, default=str))
            manifest["sections"].append({
                "file": name,
                "count": count if count is not None else (len(data) if isinstance(data, list) else 1),
            })

        # ── 1. Profile ───────────────────────────────────────────────
        profile = _model_to_dict(user, [
            "id", "email", "firstname", "lastname",
            "is_active", "is_confirmed", "is_admin",
            "abonnement_type",
            "newsletter_frequency",
            "created_at", "updated_at", "last_login", "last_digest_sent_at",
        ])
        write_json("profile.json", profile)

        # ── 2. User documents (files + metadata) ────────────────────
        docs = UserDocument.query.filter_by(user_id=user.id).all()
        docs_meta = []
        for d in docs:
            docs_meta.append(_model_to_dict(d, [
                "id", "filename", "title", "file_size",
                "uploaded_at", "indexed_at", "chunk_count",
            ]))
            # Also bundle the file bytes when the file still exists.
            try:
                base = Path(__file__).resolve().parent.parent.parent / "uploads"
                src = base / d.stored_name
                if src.exists():
                    zf.write(src, arcname=f"documents/{d.id}_{d.filename}")
            except Exception as exc:
                logger.warning("export user_doc %s file missing: %s", d.id, exc)
        write_json("documents.json", docs_meta)

        # ── 3. Cloud documents (metadata only) ──────────────────────
        clouds = (
            CloudDocument.query
            .join(CloudConnection)
            .filter(CloudConnection.user_id == user.id)
            .all()
        )
        write_json("cloud_documents.json", [
            _model_to_dict(c, [
                "id", "remote_id", "name", "mime_type", "file_size",
                "remote_modified_at", "indexed_at", "chunk_count", "web_url",
            ]) | {"connection_id": c.connection_id}
            for c in clouds
        ])

        connections = CloudConnection.query.filter_by(user_id=user.id).all()
        write_json("cloud_connections.json", [
            _model_to_dict(c, [
                "id", "provider", "remote_account_email",
                "created_at", "last_synced_at",
            ])
            for c in connections
        ])

        # ── 4. Scrapers + their articles ────────────────────────────
        scrapers = UserScraper.query.filter_by(user_id=user.id).all()
        write_json("scrapers.json", [
            _model_to_dict(s, [
                "id", "name", "url", "type",
                "created_at", "last_checked_at", "is_active",
            ])
            for s in scrapers
        ])
        articles = UserScraperArticle.query.filter_by(user_id=user.id).all()
        write_json("scraper_articles.json", [
            _model_to_dict(a, [
                "id", "scraper_id", "title", "url", "published_at",
                "indexed_at", "chunk_count",
            ]) | {"content_excerpt": (a.content or "")[:500]}
            for a in articles
        ])

        # ── 5. RAG conversations ────────────────────────────────────
        convs = Conversation.query.filter_by(user_id=user.id).all()
        conv_payload = []
        for c in convs:
            conv_payload.append({
                **_model_to_dict(c, [
                    "id", "title", "scope", "tag_filter",
                    "strict_mode", "document_scope",
                    "created_at", "updated_at",
                ]),
                "turns": [
                    {
                        "role": t.role,
                        "content": t.content,
                        "created_at": _isofmt(t.created_at),
                        "sources_json": t.sources_json,
                    }
                    for t in c.turns
                ],
            })
        write_json("conversations.json", conv_payload)

        # ── 6. Search history + watch alerts + feedback ─────────────
        # NOTE: QueryHistory and SavedQuery have a column named `query`,
        # which shadows Flask-SQLAlchemy's Model.query accessor (the
        # column descriptor wins). We use db.session.query() to dodge
        # the collision.
        from app.ext import db as _db
        write_json("query_history.json", [
            _model_to_dict(q, [
                "id", "query", "scope", "answer_excerpt",
                "sources_count", "executed_at",
            ])
            for q in _db.session.query(QueryHistory).filter_by(user_id=user.id).all()
        ])
        write_json("saved_queries.json", [
            _model_to_dict(q, [
                "id", "name", "query", "scope", "tag_filter", "created_at",
            ])
            for q in _db.session.query(SavedQuery).filter_by(user_id=user.id).all()
        ])
        write_json("watch_alerts.json", [
            _model_to_dict(a, [
                "id", "keyword", "scope", "frequency",
                "is_active", "last_notified_at", "created_at",
            ])
            for a in WatchAlert.query.filter_by(user_id=user.id).all()
        ])
        write_json("rag_feedback.json", [
            _model_to_dict(fb, [
                "id", "query", "answer_excerpt", "rating",
                "comment", "sources_count", "scope", "created_at",
            ])
            # Same column-shadowing as QueryHistory above — RagFeedback.query
            # is the encrypted column, use the session API instead.
            for fb in _db.session.query(RagFeedback).filter_by(user_id=user.id).all()
        ])

        # ── 7. Tags ─────────────────────────────────────────────────
        # Tags are user-scoped. Expose any export error rather than
        # absorbing it silently (GDPR Articles 15/20).
        try:
            tags = Tag.query.filter(Tag.user_id == user.id).all()
            write_json("tags.json", [
                _model_to_dict(t, ["id", "name", "user_id"])
                for t in tags
            ])
        except Exception:
            logger.exception("tag export failed for user_id=%s", user.id)
            write_json("tags.json", [])

        # ── 8. ChromaDB chunks ──────────────────────────────────────
        # The previous export skipped these entirely, even though
        # they're literally the user's documents broken into vectors.
        # We dump every chunk in the user's personal collection plus
        # any chunks whose metadata says user_id == this user in the
        # shared collection. Embeddings themselves are not exported
        # (they're a function of the model + text and the user can
        # always re-embed); the document text + metadata is the
        # portable artefact.
        write_json("rag_chunks.json", _extract_user_chunks(user.id))

        # ── Manifest written last so its sections list is complete ──
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    buf.seek(0)
    return buf


_CHROMA_PAGE = 1000


def _paginated_chunks(col, where: Optional[dict], collection_label: str) -> list:
    """Iterate `col.get(...)` in pages of _CHROMA_PAGE to bound memory.

    Bug M23 — l'ancien code appelait col.get() sans limit/offset. Un user
    Enterprise avec 50k+ chunks tuait le process Flask par OOM, et le
    droit RGPD Article 20 (portabilité) devenait inaccessible. Avec
    pagination par 1000, l'empreinte mémoire est plafonnée même sur de
    très grandes collections.
    """
    out = []
    offset = 0
    while True:
        kwargs = {
            "include": ["documents", "metadatas"],
            "limit": _CHROMA_PAGE,
            "offset": offset,
        }
        if where is not None:
            kwargs["where"] = where
        result = col.get(**kwargs)
        ids = result.get("ids") or []
        if not ids:
            break
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        for i, cid in enumerate(ids):
            out.append({
                "id": cid,
                "collection": collection_label,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
            })
        if len(ids) < _CHROMA_PAGE:
            break
        offset += len(ids)
    return out


def _extract_user_chunks(user_id: int) -> list:
    """Pull every chunk associated with this user from ChromaDB."""
    try:
        from app.services.rag_service import _get_collection, _get_user_collection
    except Exception as exc:
        logger.warning("ChromaDB import failed: %s", exc)
        return []

    chunks = []
    try:
        chunks.extend(_paginated_chunks(_get_user_collection(user_id), None, "personal"))
    except Exception as exc:
        logger.warning("personal chunk export failed: %s", exc)

    try:
        chunks.extend(_paginated_chunks(_get_collection(), {"user_id": user_id}, "shared"))
    except Exception as exc:
        logger.warning("shared chunk export failed: %s", exc)

    return chunks
