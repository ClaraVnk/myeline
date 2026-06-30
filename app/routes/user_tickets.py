from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.ext import db
from app.models.support_ticket import SupportTicket, TicketMessage
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api


logger = get_logger("user_tickets", subfolder="routes")

user_tickets_bp = Blueprint("user_tickets", __name__)

ALLOWED_STATUSES = ("open", "in_progress", "resolved", "closed")


def _notify_support_new_ticket(ticket: SupportTicket, user_email: str, body: str) -> None:
    notify_to = current_app.config.get("SUPPORT_NOTIFY_EMAIL", "")
    if not notify_to:
        return
    ticket_url = url_for("user_tickets.ticket_detail", ticket_id=ticket.id, _external=True)
    # Bug H10 — l'ancien code interpolait subject/body/user_email
    # directement dans une f-string HTML envoyée à SUPPORT_NOTIFY_EMAIL.
    # Phishing ciblé ou prise de contrôle session admin si la webmail
    # (Roundcube, Outlook) rend du JS depuis le body de l'email.
    # Fix : html.escape() pour tout le contenu user-controlled.
    from markupsafe import escape as _esc
    html = f"""
    <p><strong>Nouveau ticket #{ticket.id}</strong> ouvert par <strong>{_esc(user_email)}</strong></p>
    <p><strong>Sujet :</strong> {_esc(ticket.subject)}</p>
    <hr>
    <p style="white-space:pre-wrap">{_esc(body)}</p>
    <p><a href="{_esc(ticket_url)}">Voir le ticket dans l'admin</a></p>
    """
    try:
        send_email_via_brevo_api(
            to=notify_to,
            # subject est aussi user-controlled — pas d'interpolation
            # HTML mais un sujet email peut contenir des chars qui
            # cassent l'encoding MIME. Restreindre à 200 chars + strip.
            subject=f"[Support Myeline] #{ticket.id} — {(ticket.subject or '')[:200]}",
            html_content=html,
        )
    except Exception:
        logger.exception("Failed to send new-ticket notification for ticket_id=%s", ticket.id)


def _notify_support_user_reply(ticket: SupportTicket, user_email: str, body: str) -> None:
    notify_to = current_app.config.get("SUPPORT_NOTIFY_EMAIL", "")
    if not notify_to:
        return
    ticket_url = url_for("user_tickets.ticket_detail", ticket_id=ticket.id, _external=True)
    # Bug H10 — idem, escape obligatoire sur tout user-controlled.
    from markupsafe import escape as _esc
    html = f"""
    <p><strong>Nouvelle réponse utilisateur</strong> sur le ticket #{ticket.id} par <strong>{_esc(user_email)}</strong></p>
    <p><strong>Sujet :</strong> {_esc(ticket.subject)}</p>
    <hr>
    <p style="white-space:pre-wrap">{_esc(body)}</p>
    <p><a href="{_esc(ticket_url)}">Voir le ticket dans l'admin</a></p>
    """
    try:
        send_email_via_brevo_api(
            to=notify_to,
            subject=f"[Support Myeline] #{ticket.id} — Réponse utilisateur",
            html_content=html,
        )
    except Exception:
        logger.exception("Failed to send reply notification for ticket_id=%s", ticket.id)


@user_tickets_bp.route("/tickets")
@login_required
def ticket_list():
    tickets = (
        SupportTicket.query.filter_by(user_id=current_user.id)
        .order_by(SupportTicket.updated_at.desc())
        .all()
    )
    return render_template("user/tickets.html", tickets=tickets)


@user_tickets_bp.route("/tickets/new", methods=["GET", "POST"])
@login_required
def ticket_new():
    if request.method == "POST":
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        if not subject or not body:
            flash("Sujet et message requis.", "danger")
            return render_template("user/ticket_new.html")
        if len(subject) > 255:
            flash("Sujet trop long (255 caractères max).", "danger")
            return render_template("user/ticket_new.html")
        ticket = SupportTicket(user_id=current_user.id, subject=subject)
        db.session.add(ticket)
        db.session.flush()
        msg = TicketMessage(
            ticket_id=ticket.id,
            author_id=current_user.id,
            is_staff=False,
            body=body,
        )
        db.session.add(msg)
        db.session.commit()

        _notify_support_new_ticket(ticket, current_user.email, body)

        flash("Votre ticket a été créé.", "success")
        return redirect(url_for("user_tickets.ticket_detail", ticket_id=ticket.id))
    return render_template("user/ticket_new.html")


@user_tickets_bp.route("/tickets/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def ticket_detail(ticket_id):
    ticket = SupportTicket.query.filter_by(id=ticket_id, user_id=current_user.id).first_or_404()
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        if not body:
            flash("Message vide.", "danger")
        elif ticket.status in ("resolved", "closed"):
            flash("Ce ticket est clôturé.", "warning")
        else:
            msg = TicketMessage(
                ticket_id=ticket.id,
                author_id=current_user.id,
                is_staff=False,
                body=body,
            )
            db.session.add(msg)
            ticket.status = "open"
            db.session.commit()
            _notify_support_user_reply(ticket, current_user.email, body)
            flash("Réponse envoyée.", "success")
        return redirect(url_for("user_tickets.ticket_detail", ticket_id=ticket.id))
    messages = ticket.messages.order_by(TicketMessage.created_at.asc()).all()
    return render_template("user/ticket_detail.html", ticket=ticket, messages=messages)
