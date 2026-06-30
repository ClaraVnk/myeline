from datetime import datetime, timezone

from app.ext import db


class ScraperShare(db.Model):  # type: ignore
    __tablename__ = "scraper_shares"
    __table_args__ = (
        db.UniqueConstraint("scraper_id", "shared_with_id", name="uq_scraper_share"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    scraper_id = db.Column(
        db.Integer,
        db.ForeignKey("user_scrapers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    shared_with_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    scraper = db.relationship(
        "UserScraper",
        backref=db.backref("shares", lazy="dynamic", cascade="all, delete-orphan"),
    )
    owner = db.relationship("User", foreign_keys=[owner_id])
    shared_with = db.relationship("User", foreign_keys=[shared_with_id])


class DocumentShare(db.Model):  # type: ignore
    __tablename__ = "document_shares"
    __table_args__ = (
        db.UniqueConstraint("doc_id", "shared_with_id", name="uq_doc_share"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    doc_id = db.Column(
        db.Integer,
        db.ForeignKey("user_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    shared_with_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    document = db.relationship(
        "UserDocument",
        backref=db.backref("shares", lazy="dynamic", cascade="all, delete-orphan"),
    )
    owner = db.relationship("User", foreign_keys=[owner_id])
    shared_with = db.relationship("User", foreign_keys=[shared_with_id])
