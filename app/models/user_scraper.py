from datetime import datetime, timezone

from app.ext import db


class UserScraper(db.Model):  # type: ignore
    __tablename__ = "user_scrapers"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(2000), nullable=False)
    scraper_type = db.Column(db.String(10), nullable=False, default="rss")  # "rss" | "html"
    scrape_frequency = db.Column(db.String(10), nullable=False, default="daily", server_default="daily")  # "daily" | "weekly" | "monthly"
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_scraped_at = db.Column(db.DateTime, nullable=True)
    article_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    # Progression du scrape en cours — pollée par la banner dashboard
    # via /user/scrapers/scrape-status (mirroir de cloud_connections.is_syncing).
    is_scraping = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    scrape_started_at = db.Column(db.DateTime, nullable=True)
    scrape_total = db.Column(db.Integer, nullable=True)
    scrape_processed = db.Column(db.Integer, nullable=True)
    scrape_since = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship(
        "User",
        backref=db.backref("scrapers", lazy="dynamic", cascade="all, delete-orphan"),
    )
    articles = db.relationship(
        "UserScraperArticle",
        backref="scraper",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )


class UserScraperArticle(db.Model):  # type: ignore
    __tablename__ = "user_scraper_articles"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    scraper_id = db.Column(
        db.Integer,
        db.ForeignKey("user_scrapers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Bug M3 (audit round 2) — `unique=True` global empêchait deux users
    # de scraper la même URL publique (le second se mangeait un
    # IntegrityError sans recours). On scope l'unicité à (user_id, url_hash)
    # via UniqueConstraint au niveau table — migration 0023 supprime
    # l'ancien index global et crée le composite.
    url_hash = db.Column(db.String(64), nullable=False, index=True)
    url = db.Column(db.String(2000), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    content = db.Column(db.Text, nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)
    indexed_at = db.Column(db.DateTime, nullable=True)
    chunk_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    tags = db.relationship(
        "Tag",
        secondary="scraper_article_tags",
        lazy="select",
        order_by="Tag.name",
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "url_hash", name="uq_user_scraper_article_user_url"),
    )
