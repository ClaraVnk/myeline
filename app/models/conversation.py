from app.ext import db
from app.utils.crypto import EncryptedString
from app.utils.time_utils import utcnow


class Conversation(db.Model):
    """A multi-turn RAG thread between a user and the assistant.

    The thread keeps scope / tag_filter / strict_mode pinned for the whole
    exchange so follow-up questions don't silently switch retrieval scope.
    """
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(EncryptedString(), nullable=False)
    scope = db.Column(db.String(20), default="personal", nullable=False)
    tag_filter = db.Column(db.String(100), nullable=True)
    strict_mode = db.Column(db.Boolean, default=True, nullable=False)
    # Optional document scope for single-document chat (#7) — nullable for
    # general conversations.
    document_scope = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship(
        "User",
        backref=db.backref("conversations", lazy="dynamic", passive_deletes=True),
    )
    turns = db.relationship(
        "ConversationTurn",
        backref="conversation",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="ConversationTurn.created_at",
    )


class ConversationTurn(db.Model):
    """A single message in a conversation, either user-authored or assistant.

    Sources / freshness / warning payloads from the assistant turn are
    serialised as JSON so the UI can re-render them without another search.
    """
    __tablename__ = "conversation_turns"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(16), nullable=False)  # "user" | "assistant"
    content = db.Column(EncryptedString(), nullable=False)
    # Assistant turns only — JSON-encoded sources list, freshness summary,
    # and ai_warning. Empty for user turns.
    sources_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
