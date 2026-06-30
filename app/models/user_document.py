from datetime import datetime, timezone

from app.ext import db


class UserDocument(db.Model):  # type: ignore
    __tablename__ = "user_documents"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True)
    title = db.Column(db.String(500), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    uploaded_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    indexed_at = db.Column(db.DateTime, nullable=True)
    chunk_count = db.Column(db.Integer, nullable=False, default=0)

    user = db.relationship(
        "User",
        backref=db.backref("documents", lazy="dynamic", cascade="all, delete-orphan"),
    )
    tags = db.relationship(
        "Tag",
        secondary="user_document_tags",
        lazy="select",
        order_by="Tag.name",
    )

    @property
    def display_title(self) -> str:
        return self.title or self.filename

    @property
    def is_indexed(self) -> bool:
        return self.indexed_at is not None and self.chunk_count > 0
