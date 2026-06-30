"""Tests for the RAG features that landed in the recent push:
freshness indicator, temporal filter, strict-mode toggle, multi-turn
conversations, single-document chat, and Pro+ gating.

These exercise the helpers and the routes; the actual ChromaDB calls
are mocked away because the test environment doesn't ship a Chroma
instance.
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import login


@pytest.fixture()
def standard_user(app, db):
    """A second confirmed user — used for the cross-user access-control
    test (one user must not see another user's conversations).

    The OSS single-user build removed paid tiers, so this is just a plain
    confirmed account with full access like every other user."""
    from app.models.user import User
    user = User(
        firstname="Sam", lastname="Standard",
        email="standard@example.com",
        is_admin=False,
        is_confirmed=True,
        is_active=True,
        abonnement_type="gratuit",
        twofa_method="email",
    )
    user.set_password("Alice1234!")
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.delete(user)
    db.session.commit()


# ─── _doc_date_meta ────────────────────────────────────────────────────

class TestDocDateMeta:
    def _call(self, value):
        from app.services.rag_service import _doc_date_meta
        return _doc_date_meta(value)

    def test_datetime_returns_iso_and_ts(self):
        dt = datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc)
        out = self._call(dt)
        assert out["doc_date"] == "2026-03-15"
        assert out["doc_ts"] == int(dt.timestamp())

    def test_naive_datetime_assumed_utc(self):
        dt = datetime(2026, 3, 15)  # naive
        out = self._call(dt)
        assert out["doc_date"] == "2026-03-15"

    def test_iso_string_round_trip(self):
        out = self._call("2024-06-15T10:00:00Z")
        assert out["doc_date"] == "2024-06-15"
        assert isinstance(out["doc_ts"], int)

    @pytest.mark.parametrize("v", [None, "", "garbage", 0])
    def test_falsy_or_unparseable_returns_empty(self, v):
        assert self._call(v) == {}


# ─── _freshness_summary ────────────────────────────────────────────────

class TestFreshnessSummary:
    def test_none_when_no_dated_source(self):
        from app.services.rag_service import _freshness_summary
        assert _freshness_summary([{"num": 1}, {"num": 2, "doc_ts": None}]) is None

    def test_picks_newest_oldest(self):
        from app.services.rag_service import _freshness_summary
        now = datetime.now(timezone.utc)
        sources = [
            {"doc_ts": int((now - timedelta(days=2)).timestamp()), "doc_date": "2026-05-03"},
            {"doc_ts": int((now - timedelta(days=30)).timestamp()), "doc_date": "2026-04-05"},
            {},  # no date
        ]
        s = _freshness_summary(sources)
        assert s["newest_iso"] == "2026-05-03"
        assert s["oldest_iso"] == "2026-04-05"
        # 2 dated out of 3
        assert s["coverage"] == 0.67


# ─── search() temporal filter ──────────────────────────────────────────

class TestTemporalFilter:
    """search() should turn date_from / date_to into Chroma $and clauses."""

    @patch("app.services.rag_service._embed_query", return_value=[0.0] * 10)
    @patch("app.services.rag_service._hybrid_query_collection",
           return_value=([], [], [], []))
    def test_both_dates_build_and_clause(self, hybrid_mock, _embed_mock, app):
        from app.services.rag_service import search
        with app.app_context():
            search("test", scope="shared",
                   date_from="2026-01-01", date_to="2026-12-31")
        # _hybrid_query_collection is called once per (collection, variant)
        # — we assert the where clause it received contained the $and
        # combiner with both bounds.
        call_args = hybrid_mock.call_args_list
        assert call_args, "expected at least one query"
        where = call_args[0][0][4] if len(call_args[0][0]) >= 5 else call_args[0].kwargs.get("where")
        assert where is not None
        assert "$and" in where
        operators = {list(c["doc_ts"].keys())[0] for c in where["$and"] if "doc_ts" in c}
        assert operators == {"$gte", "$lte"}

    @patch("app.services.rag_service._embed_query", return_value=[0.0] * 10)
    @patch("app.services.rag_service._hybrid_query_collection",
           return_value=([], [], [], []))
    def test_only_lower_bound(self, hybrid_mock, _embed_mock, app):
        from app.services.rag_service import search
        with app.app_context():
            search("test", scope="shared", date_from="2026-01-01")
        where = hybrid_mock.call_args[0][4]
        assert where == {"doc_ts": {"$gte": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())}}


# ─── search() document scope ────────────────────────────────────────────

class TestDocumentScope:
    @patch("app.services.rag_service._embed_query", return_value=[0.0] * 10)
    @patch("app.services.rag_service._hybrid_query_collection",
           return_value=([], [], [], []))
    @patch("app.services.rag_service._get_user_collection")
    def test_user_doc_scope_filters_by_id(self, get_col_mock, hybrid_mock, _embed_mock, app):
        get_col_mock.return_value = MagicMock()
        from app.services.rag_service import search
        with app.app_context():
            search("test", user_id=42, document_scope="user_doc:7")
        # The where clause must pin user_doc_id=7 — and crucially, NO
        # other source-type filter should sneak in (single-doc means
        # ONE doc, not user_documents-and-cloud_documents-of-this-user).
        seen_filters = [c[0][4] for c in hybrid_mock.call_args_list]
        # Only one collection should have been queried — the user one —
        # with exactly the user_doc_id filter.
        assert len(seen_filters) == 1
        assert seen_filters[0] == {"user_doc_id": 7}

    def test_invalid_kind_silently_ignored(self, app):
        """A bogus document_scope shouldn't blow up — it returns no sources."""
        from app.services.rag_service import search
        with app.app_context():
            with patch("app.services.rag_service._embed_query", return_value=[0.0] * 10), \
                 patch("app.services.rag_service._hybrid_query_collection",
                       return_value=([], [], [], [])):
                r = search("test", user_id=42, document_scope="garbage:7")
        assert r["sources"] == []


# ─── strict_mode propagation ───────────────────────────────────────────

class TestStrictMode:
    @patch("app.services.rag_service._chat_ollama")
    def test_strict_true_uses_strict_prompt(self, chat_mock, app):
        from app.services.rag_service import _RAG_SYSTEM_PROMPT_STRICT, _chat
        chat_mock.return_value = "ok"
        with app.app_context():
            with patch.dict(os.environ, {"SOVEREIGN_MODE": "true"}):
                _chat("question", strict_mode=True)
        # Last positional / keyword: system_prompt
        kwargs = chat_mock.call_args.kwargs
        assert kwargs.get("system_prompt") == _RAG_SYSTEM_PROMPT_STRICT

    @patch("app.services.rag_service._chat_ollama")
    def test_strict_false_uses_relaxed_prompt(self, chat_mock, app):
        from app.services.rag_service import _RAG_SYSTEM_PROMPT_RELAXED, _chat
        chat_mock.return_value = "ok"
        with app.app_context():
            with patch.dict(os.environ, {"SOVEREIGN_MODE": "true"}):
                _chat("question", strict_mode=False)
        kwargs = chat_mock.call_args.kwargs
        assert kwargs.get("system_prompt") == _RAG_SYSTEM_PROMPT_RELAXED


# ─── Multi-turn conversations API ──────────────────────────────────────

class TestConversationsAPI:
    def _make_conv(self, app, db, user_id, title="Test conv"):
        from app.models.conversation import Conversation, ConversationTurn
        with app.app_context():
            conv = Conversation(user_id=user_id, title=title, scope="personal")
            db.session.add(conv)
            db.session.flush()
            db.session.add(ConversationTurn(
                conversation_id=conv.id, role="user", content="hello",
            ))
            db.session.add(ConversationTurn(
                conversation_id=conv.id, role="assistant", content="hi back",
            ))
            db.session.commit()
            return conv.id

    def test_list_returns_user_conversations(self, client, app, db, subscriber_user):
        cid = self._make_conv(app, db, subscriber_user.id)
        login(client, subscriber_user.email, "Alice1234!")
        r = client.get("/user/recherche/conversations")
        assert r.status_code == 200
        data = r.get_json()
        assert any(c["id"] == cid for c in data["conversations"])

    def test_get_returns_turns(self, client, app, db, subscriber_user):
        cid = self._make_conv(app, db, subscriber_user.id)
        login(client, subscriber_user.email, "Alice1234!")
        r = client.get(f"/user/recherche/conversations/{cid}")
        assert r.status_code == 200
        data = r.get_json()
        assert len(data["turns"]) == 2
        assert data["turns"][0]["role"] == "user"

    def test_delete_removes_conversation(self, client, app, db, subscriber_user):
        cid = self._make_conv(app, db, subscriber_user.id)
        login(client, subscriber_user.email, "Alice1234!")
        r = client.delete(f"/user/recherche/conversations/{cid}")
        assert r.status_code == 200
        # Re-fetch should 404
        r2 = client.get(f"/user/recherche/conversations/{cid}")
        assert r2.status_code == 404

    def test_other_user_cannot_access(self, client, app, db, subscriber_user, standard_user):
        cid = self._make_conv(app, db, subscriber_user.id)
        login(client, standard_user.email, "Alice1234!")
        r = client.get(f"/user/recherche/conversations/{cid}")
        assert r.status_code == 404


# ─── /recherche/query access (paid gating removed) ─────────────────────

class TestQueryAccess:
    """The OSS single-user build removed Free/Pro tiers — every authenticated
    user has full access to multi-turn conversations and single-document chat.

    The RAG pipeline itself isn't exercised here — we stop once the route
    accepts the payload so we don't need ChromaDB / Ollama / Mistral.
    """

    def test_user_passes_gate(self, client, app, db, subscriber_user):
        """Any confirmed user gets past the (now removed) gate and the
        request goes to RQ. We assert the response includes a job_id which
        means the route accepted the payload."""
        login(client, subscriber_user.email, "Alice1234!")
        # The route imports get_queue locally inside rag_query, so we
        # patch at the import site (app.routes.user_rag) rather than
        # the source module — otherwise the local binding still points
        # at the real RQ + Redis (which CI doesn't have, hence the 500).
        with patch("app.routes.user_rag.get_queue") as gq:
            mock_q = MagicMock()
            mock_q.enqueue.return_value = MagicMock(id="job-abc")
            gq.return_value = mock_q
            r = client.post(
                "/user/recherche/query",
                json={"query": "hello", "scope": "personal",
                      "conversation_id": 42},
            )
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.get_json()}"
        assert r.get_json().get("job_id") == "job-abc", "Pro user must receive the queued job_id"
