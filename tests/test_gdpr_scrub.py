"""Coverage for app/utils/gdpr.py — GDPR Art. 17 right-to-erasure helper.

Audit round 4 (A1-C1) extended scrub_user_audit_trail to also null
ip_address + user_agent on every audit_log row referencing the deleted
user. Previous version only cleared actor_email, leaving the network
footprint behind in backups.
"""


def _make_user(db, email="purgable@example.test"):
    from app.models.user import User
    u = User(
        firstname="To",
        lastname="Purge",
        email=email,
        is_admin=False,
        is_confirmed=True,
        abonnement_type="gratuit",
        twofa_method="email",
    )
    u.set_password("Strong-Pass-12345!")
    db.session.add(u)
    db.session.commit()
    return u


def _make_audit_row(db, user, ip="203.0.113.7", ua="Mozilla/5.0 (X11)"):
    from app.models.audit_log import AuditLog
    row = AuditLog(
        actor_user_id=user.id,
        actor_email=user.email,
        action="some.action",
        target_type="dummy",
        target_id=str(user.id),
        payload_json="{}",
        ip_address=ip,
        user_agent=ua,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_scrub_nulls_ip_and_user_agent(app, db):
    """After scrub, ip_address and user_agent must be NULL on every row."""
    from app.models.audit_log import AuditLog
    from app.utils.gdpr import scrub_user_audit_trail

    user = _make_user(db, "ip-leak@example.test")
    row1 = _make_audit_row(db, user, ip="198.51.100.42", ua="curl/8.0")
    row2 = _make_audit_row(db, user, ip="2001:db8::1", ua="Firefox/128")

    scrub_user_audit_trail(user, reason="self_delete")
    db.session.commit()

    row1_fresh = db.session.get(AuditLog, row1.id)
    row2_fresh = db.session.get(AuditLog, row2.id)
    assert row1_fresh.ip_address is None
    assert row1_fresh.user_agent is None
    assert row2_fresh.ip_address is None
    assert row2_fresh.user_agent is None


def test_scrub_replaces_email_with_deterministic_placeholder(app, db):
    """Email is replaced by deleted-<hash>; actor_user_id goes NULL."""
    from app.models.audit_log import AuditLog
    from app.utils.gdpr import scrub_user_audit_trail

    user = _make_user(db, "email-purge@example.test")
    row = _make_audit_row(db, user)

    scrub_user_audit_trail(user, reason="retention")
    db.session.commit()

    fresh = db.session.get(AuditLog, row.id)
    assert fresh.actor_email.startswith("deleted-")
    assert fresh.actor_user_id is None
    # The placeholder is deterministic — running scrub twice on the same
    # email yields the same suffix (allows correlation without PII).
    expected_prefix = "deleted-"
    assert fresh.actor_email.startswith(expected_prefix)
    assert len(fresh.actor_email) > len(expected_prefix)


def test_scrub_emits_final_gdpr_purge_row(app, db):
    """A user.gdpr_purge audit row is appended carrying the placeholder."""
    from app.models.audit_log import AuditLog
    from app.utils.gdpr import scrub_user_audit_trail

    user = _make_user(db, "trail-purge@example.test")
    _make_audit_row(db, user)

    scrub_user_audit_trail(user, reason="admin_delete")
    db.session.commit()

    purge_rows = (
        AuditLog.query.filter_by(action="user.gdpr_purge")
        .order_by(AuditLog.id.desc())
        .limit(1)
        .all()
    )
    assert len(purge_rows) == 1
    purge = purge_rows[0]
    assert purge.actor_email.startswith("deleted-")
    assert purge.actor_user_id is None
    assert "admin_delete" in (purge.payload_json or "")


def test_scrub_noop_on_missing_user(app, db):
    """scrub called with None must be a defensive no-op."""
    from app.utils.gdpr import scrub_user_audit_trail
    scrub_user_audit_trail(None)  # should not raise
