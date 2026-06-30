from app.ext import db


class Tag(db.Model):  # type: ignore
    __tablename__ = "tags"
    __table_args__ = (
        db.UniqueConstraint("name", "user_id", name="uq_tag_name_owner"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
