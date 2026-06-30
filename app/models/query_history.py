from app.ext import db
from app.utils.crypto import EncryptedString
from app.utils.time_utils import utcnow


class QueryHistory(db.Model):
    __tablename__ = "query_history"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query = db.Column(EncryptedString(), nullable=False)
    scope = db.Column(db.String(20), nullable=True)
    answer_excerpt = db.Column(EncryptedString(), nullable=True)
    sources_count = db.Column(db.Integer, default=0)
    executed_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    user = db.relationship("User", backref=db.backref("query_history", lazy="dynamic", passive_deletes=True))


class SavedQuery(db.Model):
    __tablename__ = "saved_queries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(EncryptedString(), nullable=False)
    query = db.Column(EncryptedString(), nullable=False)
    scope = db.Column(db.String(20), default="personal", nullable=False)
    tag_filter = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("saved_queries", lazy="dynamic", passive_deletes=True))
