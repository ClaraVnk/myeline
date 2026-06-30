from app.ext import db
from app.utils.crypto import EncryptedString
from app.utils.time_utils import utcnow


class WatchAlert(db.Model):
    __tablename__ = "watch_alerts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    keyword = db.Column(EncryptedString(), nullable=False)
    scope = db.Column(db.String(20), default="all", nullable=False)
    frequency = db.Column(db.String(10), default="daily", nullable=False)  # "daily" | "weekly"
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    last_notified_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("watch_alerts", lazy="dynamic", passive_deletes=True))
