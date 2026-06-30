"""Audit log — append-only record of who-did-what, for compliance and
post-incident forensics.

Design notes:
  - Actor is captured both as a foreign key (best effort, may go NULL if
    the user is later deleted) AND as an immutable email snapshot, so a
    deleted account doesn't erase the trail.
  - target_type / target_id pin the affected resource (organisation,
    user, ticket, broadcast, etc.). Free-form by design — adding new
    audited resources never requires a schema change.
  - payload is JSON-serialised in `payload_json` (Text) for arbitrary
    context without bloating the column count.
  - created_at is indexed for the dominant query pattern (timeline
    paging) plus an actor index for "what did user X do".
"""
from app.ext import db
from app.utils.time_utils import utcnow


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    # Plain Integer rather than BigInteger because SQLite (used by tests)
    # only auto-increments INTEGER PRIMARY KEY. 2^31 rows is ~5500 years
    # at 1000 audited actions per day, so we don't need the wider range.
    id = db.Column(db.Integer, primary_key=True)
    # Actor: nullable so a user delete doesn't cascade-wipe their audit
    # trail. The immutable email snapshot keeps the record useful.
    actor_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_email = db.Column(db.String(255), nullable=False)
    action = db.Column(db.String(80), nullable=False, index=True)
    # Free-form so the schema stays stable when new audited resources
    # are added. Validated by callers, not by the DB.
    target_type = db.Column(db.String(40), nullable=True)
    target_id = db.Column(db.String(80), nullable=True)
    payload_json = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)  # IPv6-friendly
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(
        db.DateTime, default=utcnow, nullable=False, index=True
    )
