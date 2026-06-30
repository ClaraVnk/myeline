from app.ext import db
from app.utils.time_utils import utcnow


class OAuthAccount(db.Model):
    """Links an external OAuth identity (Google, Microsoft, Apple) to a Myeline user."""

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        db.UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = db.Column(db.String(20), nullable=False)       # google | microsoft | apple
    provider_user_id = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(150), nullable=True)          # absent on subsequent Apple logins
    created_at = db.Column(db.DateTime, default=utcnow)

    user = db.relationship(
        "User",
        backref=db.backref(
            "oauth_accounts",
            lazy="dynamic",
            cascade="all, delete-orphan",
        ),
    )
