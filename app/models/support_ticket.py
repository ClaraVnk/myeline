from app.ext import db
from app.utils.time_utils import utcnow


class SupportTicket(db.Model):
    __tablename__ = "support_tickets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subject = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="open", index=True)  # open, in_progress, resolved, closed
    priority = db.Column(db.String(20), nullable=False, default="normal")  # low, normal, high, urgent
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user = db.relationship("User", backref=db.backref("support_tickets", lazy="dynamic"))
    messages = db.relationship(
        "TicketMessage",
        backref="ticket",
        cascade="all, delete-orphan",
        order_by="TicketMessage.created_at",
        lazy="dynamic",
    )

    @property
    def last_message_at(self):
        last = self.messages.order_by(TicketMessage.created_at.desc()).first()
        return last.created_at if last else self.created_at

    @property
    def message_count(self):
        return self.messages.count()


class TicketMessage(db.Model):
    __tablename__ = "ticket_messages"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_staff = db.Column(db.Boolean, nullable=False, default=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    author = db.relationship("User", foreign_keys=[author_id])
