from app.ext import db
from app.utils.crypto import EncryptedString
from app.utils.time_utils import utcnow


class RagFeedback(db.Model):
    __tablename__ = "rag_feedbacks"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query = db.Column(EncryptedString(), nullable=False)
    answer_excerpt = db.Column(EncryptedString(), nullable=True)
    rating = db.Column(db.String(12), nullable=False)  # "thumbs_up" | "thumbs_down"
    comment = db.Column(EncryptedString(), nullable=True)
    sources_count = db.Column(db.Integer, nullable=True)
    scope = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    user = db.relationship("User", backref=db.backref("rag_feedbacks", lazy="dynamic", passive_deletes=True))
