from app.ext import db


user_document_tags = db.Table(
    "user_document_tags",
    db.Column(
        "document_id",
        db.Integer,
        db.ForeignKey("user_documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "tag_id",
        db.Integer,
        db.ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Index("ix_user_document_tags_tag_id", "tag_id"),
)

scraper_article_tags = db.Table(
    "scraper_article_tags",
    db.Column(
        "article_id",
        db.Integer,
        db.ForeignKey("user_scraper_articles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "tag_id",
        db.Integer,
        db.ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Index("ix_scraper_article_tags_tag_id", "tag_id"),
)

cloud_document_tags = db.Table(
    "cloud_document_tags",
    db.Column(
        "document_id",
        db.Integer,
        db.ForeignKey("cloud_documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "tag_id",
        db.Integer,
        db.ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Index("ix_cloud_document_tags_tag_id", "tag_id"),
)
