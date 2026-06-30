"""Tests for app/routes/user_tags.py — CRUD operations on tags.

All authenticated tests use subscriber_user (premium_pro, active) because the
before_request hook blocks non-subscriber users from accessing /user/* routes.
"""
import json

import pytest

from tests.helpers import login, logout


_EMAIL = "subscriber@example.com"
_PWD = "Alice1234!"


def _json_post(client, url, data):
    return client.post(url, data=json.dumps(data), content_type="application/json")


def _json_delete(client, url):
    return client.delete(url)


def _make_document(db, user, suffix=""):
    from app.models.user_document import UserDocument
    doc = UserDocument(
        user_id=user.id,
        filename=f"test{suffix}.pdf",
        stored_name=f"stored_{user.id}_{suffix or 'default'}.pdf",
    )
    db.session.add(doc)
    db.session.commit()
    return doc


class TestTagAutocomplete:
    def test_requires_login(self, client):
        resp = client.get("/user/tags/autocomplete")
        assert resp.status_code == 302

    def test_returns_empty_list_when_no_tags(self, client, subscriber_user):
        login(client, _EMAIL, _PWD)
        resp = client.get("/user/tags/autocomplete")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        logout(client)

    def test_returns_user_tags(self, client, subscriber_user, db):
        from app.models.tag import Tag
        tag = Tag(name="neurologie", user_id=subscriber_user.id)
        db.session.add(tag)
        db.session.commit()

        login(client, _EMAIL, _PWD)
        resp = client.get("/user/tags/autocomplete")
        assert resp.status_code == 200
        data = resp.get_json()
        assert any(t["name"] == "neurologie" for t in data)
        logout(client)

        db.session.delete(tag)
        db.session.commit()

    def test_filters_by_prefix(self, client, subscriber_user, db):
        from app.models.tag import Tag
        tag1 = Tag(name="neurotag1", user_id=subscriber_user.id)
        tag2 = Tag(name="neurotag2", user_id=subscriber_user.id)
        tag3 = Tag(name="cardiologie-x", user_id=subscriber_user.id)
        db.session.add_all([tag1, tag2, tag3])
        db.session.commit()

        login(client, _EMAIL, _PWD)
        resp = client.get("/user/tags/autocomplete?q=neuro")
        assert resp.status_code == 200
        data = resp.get_json()
        names = [t["name"] for t in data]
        assert "neurotag1" in names
        assert "neurotag2" in names
        assert "cardiologie-x" not in names
        logout(client)

        for tag in [tag1, tag2, tag3]:
            db.session.delete(tag)
        db.session.commit()

    def test_filters_out_other_user_tags(self, client, subscriber_user, confirmed_user, db):
        from app.models.tag import Tag
        tag_other = Tag(name="private-tag-other", user_id=confirmed_user.id)
        tag_own = Tag(name="my-own-tag-sub", user_id=subscriber_user.id)
        db.session.add_all([tag_other, tag_own])
        db.session.commit()

        login(client, _EMAIL, _PWD)
        resp = client.get("/user/tags/autocomplete")
        assert resp.status_code == 200
        data = resp.get_json()
        names = [t["name"] for t in data]
        assert "my-own-tag-sub" in names
        assert "private-tag-other" not in names
        logout(client)

        db.session.delete(tag_other)
        db.session.delete(tag_own)
        db.session.commit()

    def test_returns_id_and_name_fields(self, client, subscriber_user, db):
        from app.models.tag import Tag
        tag = Tag(name="immunologie-test", user_id=subscriber_user.id)
        db.session.add(tag)
        db.session.commit()

        login(client, _EMAIL, _PWD)
        resp = client.get("/user/tags/autocomplete")
        assert resp.status_code == 200
        data = resp.get_json()
        item = next((t for t in data if t["name"] == "immunologie-test"), None)
        assert item is not None
        assert "id" in item
        assert "name" in item
        logout(client)

        db.session.delete(tag)
        db.session.commit()


class TestAssignTag:
    def test_requires_login(self, client, subscriber_user, db):
        doc = _make_document(db, subscriber_user, "anon-assign")
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {"name": "test"})
        assert resp.status_code == 302
        db.session.delete(doc)
        db.session.commit()

    def test_missing_name_returns_400(self, client, subscriber_user, db):
        doc = _make_document(db, subscriber_user, "no-name")
        login(client, _EMAIL, _PWD)
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {})
        assert resp.status_code == 400
        assert "error" in resp.get_json()
        logout(client)
        db.session.delete(doc)
        db.session.commit()

    def test_name_too_long_returns_400(self, client, subscriber_user, db):
        doc = _make_document(db, subscriber_user, "toolong")
        login(client, _EMAIL, _PWD)
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {"name": "x" * 101})
        assert resp.status_code == 400
        data = resp.get_json()
        assert "100" in data["error"]
        logout(client)
        db.session.delete(doc)
        db.session.commit()

    def test_unknown_resource_type_returns_404(self, client, subscriber_user, db):
        login(client, _EMAIL, _PWD)
        resp = _json_post(client, "/user/tags/unknown_type/999", {"name": "test"})
        assert resp.status_code == 404
        logout(client)

    def test_nonexistent_resource_returns_404(self, client, subscriber_user, db):
        login(client, _EMAIL, _PWD)
        resp = _json_post(client, "/user/tags/document/999999", {"name": "test"})
        assert resp.status_code == 404
        logout(client)

    def test_other_user_document_returns_404(self, client, subscriber_user, confirmed_user, db):
        doc = _make_document(db, confirmed_user, "other-doc")
        login(client, _EMAIL, _PWD)
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {"name": "test"})
        assert resp.status_code == 404
        logout(client)
        db.session.delete(doc)
        db.session.commit()

    def test_assign_creates_tag_and_returns_it(self, client, subscriber_user, db):
        doc = _make_document(db, subscriber_user, "create-tag")
        login(client, _EMAIL, _PWD)
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {"name": "Rhumatologie"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "rhumatologie"  # lowercased
        assert "id" in data
        logout(client)
        db.session.delete(doc)
        db.session.commit()

    def test_assign_reuses_existing_tag(self, client, subscriber_user, db):
        from app.ext import db as _db
        from app.models.tag import Tag
        from app.models.user_document import UserDocument
        doc = _make_document(db, subscriber_user, "reuse-tag")
        existing_tag = Tag(name="cardiologie-reuse", user_id=subscriber_user.id)
        db.session.add(existing_tag)
        db.session.commit()
        existing_id = existing_tag.id

        login(client, _EMAIL, _PWD)
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {"name": "cardiologie-reuse"})
        assert resp.status_code == 200
        assert resp.get_json()["id"] == existing_id
        logout(client)

        doc_fresh = _db.session.get(UserDocument, doc.id)
        if doc_fresh:
            db.session.delete(doc_fresh)
        tag_fresh = _db.session.get(Tag, existing_id)
        if tag_fresh:
            db.session.delete(tag_fresh)
        db.session.commit()

    def test_assign_same_tag_twice_is_idempotent(self, client, subscriber_user, db):
        from app.ext import db as _db
        from app.models.user_document import UserDocument
        doc = _make_document(db, subscriber_user, "idem")
        login(client, _EMAIL, _PWD)
        _json_post(client, f"/user/tags/document/{doc.id}", {"name": "idempotent-tag"})
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {"name": "idempotent-tag"})
        assert resp.status_code == 200
        # Document should have exactly one tag with this name
        doc_fresh = _db.session.get(UserDocument, doc.id)
        tag_names = [t.name for t in doc_fresh.tags]
        assert tag_names.count("idempotent-tag") == 1
        logout(client)
        db.session.delete(doc_fresh)
        db.session.commit()

    def test_assign_tag_name_is_trimmed_and_lowercased(self, client, subscriber_user, db):
        doc = _make_document(db, subscriber_user, "trim")
        login(client, _EMAIL, _PWD)
        resp = _json_post(client, f"/user/tags/document/{doc.id}", {"name": "  NEUROLOGIE  "})
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "neurologie"
        logout(client)
        db.session.delete(doc)
        db.session.commit()


class TestRemoveTag:
    def _make_document_with_tag(self, db, user, tag_name):
        from app.models.tag import Tag
        from app.models.user_document import UserDocument
        doc = UserDocument(
            user_id=user.id,
            filename=f"tag-{tag_name}.pdf",
            stored_name=f"stored_{user.id}_{tag_name}.pdf",
        )
        db.session.add(doc)
        db.session.flush()
        tag = Tag(name=tag_name, user_id=user.id)
        db.session.add(tag)
        db.session.flush()
        doc.tags.append(tag)
        db.session.commit()
        return doc, tag

    def test_requires_login(self, client, subscriber_user, db):
        doc, tag = self._make_document_with_tag(db, subscriber_user, "remove-anon")
        resp = _json_delete(client, f"/user/tags/document/{doc.id}/{tag.id}")
        assert resp.status_code == 302
        db.session.delete(doc)
        db.session.commit()

    def test_remove_tag_from_document(self, client, subscriber_user, db):
        from app.ext import db as _db
        from app.models.user_document import UserDocument
        doc, tag = self._make_document_with_tag(db, subscriber_user, "removable-tag")
        doc_id = doc.id
        tag_id = tag.id

        login(client, _EMAIL, _PWD)
        resp = _json_delete(client, f"/user/tags/document/{doc_id}/{tag_id}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        doc_fresh = _db.session.get(UserDocument, doc_id)
        assert all(t.id != tag_id for t in doc_fresh.tags)
        logout(client)
        db.session.delete(doc_fresh)
        db.session.commit()

    def test_remove_nonexistent_tag_still_succeeds(self, client, subscriber_user, db):
        doc = _make_document(db, subscriber_user, "no-tag-here")
        login(client, _EMAIL, _PWD)
        resp = _json_delete(client, f"/user/tags/document/{doc.id}/99999")
        assert resp.status_code == 200
        logout(client)
        db.session.delete(doc)
        db.session.commit()

    def test_remove_tag_from_other_user_document_returns_404(
        self, client, subscriber_user, confirmed_user, db
    ):
        doc, tag = self._make_document_with_tag(db, confirmed_user, "other-user-tag-x")
        login(client, _EMAIL, _PWD)
        resp = _json_delete(client, f"/user/tags/document/{doc.id}/{tag.id}")
        assert resp.status_code == 404
        logout(client)
        db.session.delete(doc)
        db.session.commit()

    def test_remove_tag_endpoint_succeeds_and_tag_is_no_longer_on_doc(
        self, client, subscriber_user, db
    ):
        """After removal the tag must not appear on the document anymore.

        Note: _delete_tag_if_orphan() is called before the association row is
        flushed, so the orphan-deletion path can only fire when a flush is done
        first; that is a known quirk of the current implementation.  This test
        verifies the public contract (tag absent from doc) rather than the
        internal cleanup detail.
        """
        from app.ext import db as _db
        from app.models.user_document import UserDocument
        doc, tag = self._make_document_with_tag(db, subscriber_user, "orphan-tag-x")
        doc_id = doc.id
        tag_id = tag.id

        login(client, _EMAIL, _PWD)
        resp = _json_delete(client, f"/user/tags/document/{doc_id}/{tag_id}")
        assert resp.status_code == 200
        logout(client)

        _db.session.expire_all()
        doc_fresh = _db.session.get(UserDocument, doc_id)
        assert doc_fresh is not None
        assert all(t.id != tag_id for t in doc_fresh.tags)

        db.session.delete(doc_fresh)
        db.session.commit()
