from app.ext import db


class Newsletter(db.Model):  # type: ignore
    __tablename__ = "newsletters"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    contenu = db.Column(db.Text, nullable=False)
    date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)

    created_at = db.Column(
        db.TIMESTAMP,
        server_default=db.func.current_timestamp(),
    )
    updated_at = db.Column(
        db.TIMESTAMP,
        server_default=db.func.current_timestamp(),
        server_onupdate=db.func.current_timestamp(),
    )

    sections = db.relationship(
        "NewsletterSection",
        back_populates="newsletter",
        lazy="select",
        cascade="all, delete-orphan",
    )
