"""Tests for the cloud-sync progress helpers + /user/cloud/sync-status endpoint.

The helpers are wired into every connector and bumped per processed file;
the endpoint is polled every ~10 s by the dashboard banner partial.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def pro_user(app, db):
    from app.models.user import User
    with app.app_context():
        u = User(
            email=f"sync-pro-{uuid.uuid4().hex[:6]}@example.com",
            firstname="P", lastname="C",
            is_confirmed=True, is_active=True,
            abonnement_type="premium_pro",
            twofa_method="email",
        )
        u.set_password("ProSync1!")
        db.session.add(u)
        db.session.commit()
        user_id = u.id
    yield user_id
    with app.app_context():
        from app.models.cloud_connection import CloudConnection
        CloudConnection.query.filter_by(user_id=user_id).delete()
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
        db.session.commit()


@pytest.fixture
def gdrive_conn(app, db, pro_user):
    """A CloudConnection row owned by pro_user."""
    from app.models.cloud_connection import CloudConnection
    with app.app_context():
        c = CloudConnection(
            user_id=pro_user, provider="gdrive",
            access_token_enc="x", refresh_token_enc="x",
            folder_id=None,
        )
        db.session.add(c)
        db.session.commit()
        cid = c.id
    yield cid
    with app.app_context():
        c = db.session.get(CloudConnection, cid)
        if c:
            db.session.delete(c)
            db.session.commit()


# ─── start / set / bump / clear ──────────────────────────────────────

class TestStartSyncProgress:
    def test_initialises_fields(self, app, db, gdrive_conn):
        from app.models.cloud_connection import CloudConnection
        from app.services.cloud_sync_utils import start_sync_progress
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            start_sync_progress(c, total=42)
            db.session.refresh(c)
        assert c.sync_total_files == 42
        assert c.sync_processed_files == 0
        assert c.sync_started_at is not None

    def test_clamps_negative_total_to_zero(self, app, db, gdrive_conn):
        from app.models.cloud_connection import CloudConnection
        from app.services.cloud_sync_utils import start_sync_progress
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            start_sync_progress(c, total=-5)
            assert c.sync_total_files == 0


class TestSetSyncTotal:
    def test_updates_total_without_resetting_processed(self, app, db, gdrive_conn):
        from app.models.cloud_connection import CloudConnection
        from app.services.cloud_sync_utils import bump_sync_progress, set_sync_total, start_sync_progress
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            start_sync_progress(c, total=10)
            bump_sync_progress(c)
            bump_sync_progress(c)
            set_sync_total(c, 25)
            db.session.refresh(c)
        assert c.sync_total_files == 25
        assert c.sync_processed_files == 2  # still 2, not reset


class TestBumpSyncProgress:
    def test_increments_atomically(self, app, db, gdrive_conn):
        from app.models.cloud_connection import CloudConnection
        from app.services.cloud_sync_utils import bump_sync_progress, start_sync_progress
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            start_sync_progress(c, total=10)
            for _ in range(7):
                bump_sync_progress(c)
            db.session.refresh(c)
        assert c.sync_processed_files == 7

    def test_handles_null_initial_value(self, app, db, gdrive_conn):
        """Starting from NULL (a sync that didn't go through start_sync_progress)
        the helper must still bump cleanly to 1."""
        from app.models.cloud_connection import CloudConnection
        from app.services.cloud_sync_utils import bump_sync_progress
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            assert c.sync_processed_files is None
            bump_sync_progress(c)
            db.session.refresh(c)
        assert c.sync_processed_files == 1

    def test_no_op_for_unsaved_connection(self, app):
        from unittest.mock import MagicMock

        from app.services.cloud_sync_utils import bump_sync_progress

        # Connection without an .id — must not crash, must not query.
        c = MagicMock(id=None)
        with app.app_context():
            bump_sync_progress(c)


class TestClearSyncProgress:
    def test_resets_all_fields(self, app, db, gdrive_conn):
        from app.models.cloud_connection import CloudConnection
        from app.services.cloud_sync_utils import clear_sync_progress, start_sync_progress
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            start_sync_progress(c, total=10)
            clear_sync_progress(c)
            db.session.refresh(c)
        assert c.sync_total_files is None
        assert c.sync_processed_files is None
        assert c.sync_started_at is None


# ─── /user/cloud/sync-status endpoint ────────────────────────────────

def _login_as(client, app, db, user_id, password):
    from app.models.user import User
    from tests.helpers import login
    with app.app_context():
        u = db.session.get(User, user_id)
        email = u.email
    return login(client, email, password)


class TestSyncStatusEndpoint:
    def test_requires_login(self, client):
        r = client.get("/user/cloud/sync-status", follow_redirects=False)
        assert r.status_code in (301, 302)
        # Flask-Login default redirects unauthenticated users to /login.
        assert "/login" in r.headers.get("Location", "")

    def test_empty_payload_when_no_active_sync(
        self, app, db, client, pro_user, gdrive_conn,
    ):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            c.is_syncing = False
            db.session.commit()
        _login_as(client, app, db, pro_user, "ProSync1!")
        r = client.get("/user/cloud/sync-status")
        assert r.status_code == 200
        assert r.get_json() == {"syncs": []}

    def test_returns_active_sync(
        self, app, db, client, pro_user, gdrive_conn,
    ):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            c.is_syncing = True
            c.sync_total_files = 100
            c.sync_processed_files = 25
            c.sync_started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
            db.session.commit()
        _login_as(client, app, db, pro_user, "ProSync1!")
        r = client.get("/user/cloud/sync-status")
        assert r.status_code == 200
        data = r.get_json()
        assert len(data["syncs"]) == 1
        s = data["syncs"][0]
        assert s["provider"] == "gdrive"
        assert s["provider_label"] == "Google Drive"
        assert s["processed"] == 25
        assert s["total"] == 100
        # ETA is computed once we have ≥3 processed and elapsed > 0.
        assert s["eta_s"] is not None
        assert s["elapsed_s"] is not None and s["elapsed_s"] >= 30

    def test_eta_skipped_below_minimum_processed(
        self, app, db, client, pro_user, gdrive_conn,
    ):
        from app.models.cloud_connection import CloudConnection
        with app.app_context():
            c = db.session.get(CloudConnection, gdrive_conn)
            c.is_syncing = True
            c.sync_total_files = 100
            c.sync_processed_files = 1  # <3 → no ETA yet
            c.sync_started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
            db.session.commit()
        _login_as(client, app, db, pro_user, "ProSync1!")
        data = client.get("/user/cloud/sync-status").get_json()
        assert data["syncs"][0]["eta_s"] is None

    def test_does_not_leak_other_users_syncs(
        self, app, db, client, pro_user, gdrive_conn,
    ):
        # Another user with an active sync — must NOT show up.
        from app.models.cloud_connection import CloudConnection
        from app.models.user import User
        with app.app_context():
            other = User(
                email=f"other-{uuid.uuid4().hex[:6]}@example.com",
                is_confirmed=True, is_active=True,
                abonnement_type="premium_pro",
            )
            other.set_password("Other1234!")
            db.session.add(other)
            db.session.commit()
            other_conn = CloudConnection(
                user_id=other.id, provider="dropbox",
                access_token_enc="x", refresh_token_enc="x",
                is_syncing=True, sync_total_files=50,
                sync_processed_files=10,
                sync_started_at=datetime.now(timezone.utc),
            )
            db.session.add(other_conn)
            # Our pro_user has nothing running.
            c = db.session.get(CloudConnection, gdrive_conn)
            c.is_syncing = False
            db.session.commit()

        _login_as(client, app, db, pro_user, "ProSync1!")
        r = client.get("/user/cloud/sync-status")
        assert r.get_json() == {"syncs": []}


# ─── Flat cron throttle (60-min floor, no per-tier gating) ───────────

class TestSyncFloor:
    def test_request_finer_than_floor_is_clamped(self):
        """A row asking for a finer cadence than the 60-min floor is clamped."""
        from unittest.mock import MagicMock

        from app.cron.check_cloud_sync import _effective_min_minutes

        conn = MagicMock(sync_frequency_minutes=30)
        assert _effective_min_minutes(conn) == 60

    def test_request_coarser_than_floor_is_respected(self):
        """A row asking for a coarser cadence than the floor is honoured."""
        from unittest.mock import MagicMock

        from app.cron.check_cloud_sync import _effective_min_minutes

        conn = MagicMock(sync_frequency_minutes=1440)
        assert _effective_min_minutes(conn) == 1440

    def test_due_when_beyond_effective_floor(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock

        from app.cron.check_cloud_sync import _is_due

        conn = MagicMock(
            sync_frequency_minutes=240,  # effective floor = 240 min (4 h)
            last_sync_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
        assert _is_due(conn) is True

    def test_not_due_within_effective_floor(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock

        from app.cron.check_cloud_sync import _is_due

        conn = MagicMock(
            sync_frequency_minutes=240,
            last_sync_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert _is_due(conn) is False

    def test_due_when_never_synced(self):
        from unittest.mock import MagicMock

        from app.cron.check_cloud_sync import _is_due

        conn = MagicMock(sync_frequency_minutes=240, last_sync_at=None)
        assert _is_due(conn) is True
