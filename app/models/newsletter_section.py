from app.ext import db


class NewsletterSection(db.Model):  # type: ignore
    __tablename__ = "newsletter_section"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.String(500), nullable=True)
    order = db.Column(db.Integer, nullable=False, default=0)
    tag = db.Column(db.String(64), nullable=True)
    source_pub_date = db.Column(db.Date, nullable=True)

    # Clé étrangère vers newsletters.id (table EXISTANTE en base)
    newsletter_id = db.Column(
        db.Integer, db.ForeignKey("newsletters.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    newsletter = db.relationship("Newsletter", back_populates="sections")
