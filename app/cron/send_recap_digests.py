"""Cron: send personalized digest emails to users with active scraper sources.

Runs on every tick (suggested: hourly via system cron).
For each eligible user, checks whether enough time has elapsed since their
last digest based on their chosen frequency, then assembles recent scraper
articles (RSS/HTML) published in that window and emails them.

Only users who have at least one active scraper source AND have chosen a
digest frequency receive emails.
"""

import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from flask import render_template, url_for
from flask.cli import AppGroup

from app.ext import db
from app.models.user import User
from app.models.user_scraper import UserScraper, UserScraperArticle
from app.services.user_services import generate_unsubscribe_token
from app.utils.logger import get_logger
from app.utils.mailer import send_email_via_brevo_api
from app.utils.time_utils import utcnow


_MAX_WORKERS = 10


logger = get_logger("send_recap_digests", subfolder="cron")

CRON_SCHEDULE = "0 * * * *"
CRON_DESCRIPTION = "Hourly digest dispatch — fires per user-chosen frequency"

cron_cli = AppGroup("cron")

VALID_FREQUENCIES = ("hourly", "daily", "weekly", "monthly")

_DELTA = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}

_LABEL = {
    "hourly": "horaire",
    "daily": "quotidien",
    "weekly": "hebdomadaire",
    "monthly": "mensuel",
}


def _period_label(since, now):
    if since.date() == now.date():
        return f"du jour — {now.strftime('%d/%m/%Y')}"
    return f"du {since.strftime('%d/%m')} au {now.strftime('%d/%m/%Y')}"


def _fetch_scraper_articles(user_id: int, since) -> list[dict]:
    """Return scraper articles for *user_id* published after *since*.

    Returns a list of dicts with keys: title, url, content, published_at,
    source_name. Sorted by published_at descending (most recent first).
    """
    rows = (
        UserScraperArticle.query
        .join(UserScraper, UserScraperArticle.scraper_id == UserScraper.id)
        .filter(
            UserScraperArticle.user_id == user_id,
            UserScraper.is_active.is_(True),
            UserScraperArticle.published_at >= since,
        )
        .order_by(UserScraperArticle.published_at.desc())
        .with_entities(
            UserScraperArticle.title,
            UserScraperArticle.url,
            UserScraperArticle.content,
            UserScraperArticle.published_at,
            UserScraper.name.label("source_name"),
        )
        .all()
    )
    return [
        {
            "title": r.title,
            "url": r.url,
            "content": r.content or "",
            "published_at": r.published_at,
            "source_name": r.source_name,
        }
        for r in rows
    ]


def _user_has_active_scrapers(user_id: int) -> bool:
    return UserScraper.query.filter_by(user_id=user_id, is_active=True).count() > 0


# Cap the number of articles summarised per digest so a large batch can't run
# up the user's API bill (or stall the cron). Extra articles keep their raw
# excerpt.
_MAX_SUMMARIES_PER_DIGEST = 15


def _attach_summaries(user_id: int, articles: list[dict]) -> None:
    """Add a ``summary`` key to each article using the user's BYOK key.

    Best-effort and in-place: failures leave the article without a summary
    (the template falls back to the raw excerpt).
    """
    from app.services import rag_service
    for art in articles[:_MAX_SUMMARIES_PER_DIGEST]:
        summary = rag_service.summarize_for_user(art.get("content", ""), user_id)
        if summary:
            art["summary"] = summary


@cron_cli.command("send-recap-digests")
def send_recap_digests():
    logger.info("Lancement de send_recap_digests")
    try:
        now = utcnow()

        candidates = (
            User.query
            .filter(
                User.newsletter_frequency.in_(VALID_FREQUENCIES),
                User.is_confirmed.is_(True),
                User.is_active.is_(True),
            )
            .all()
        )
        logger.info("%d utilisateurs avec un récap actif", len(candidates))

        # Phase 1 (sequential, in app context): filter eligible users, fetch
        # their articles, and render the email body. Templates need the request
        # context for url_for(), so this stays single-threaded.
        tasks = []
        for user in candidates:
            try:
                if not (user.is_admin or user.is_pro_subscriber):
                    continue
                if not _user_has_active_scrapers(user.id):
                    continue

                delta = _DELTA[user.newsletter_frequency]
                last_sent = user.last_digest_sent_at
                if last_sent and (now - last_sent) < delta:
                    continue

                # Bug L5 (audit round 2) — sans cap, un user qui vient
                # de s'inscrire avec frequency=monthly recevait
                # immédiatement un digest contenant 30 jours d'articles
                # antérieurs à son abonnement (UX confondante). Borner
                # `since` à user.created_at.
                fallback_since = now - delta
                if user.created_at and user.created_at > fallback_since:
                    fallback_since = user.created_at
                since = last_sent or fallback_since
                articles = _fetch_scraper_articles(user.id, since)
                if not articles:
                    logger.debug(
                        "Aucun article scraper pour user_id=%s depuis %s",
                        user.id, since.date(),
                    )
                    continue

                # Optional per-user AI summary: when the user opted in AND a key
                # can be resolved — their own BYOK key (priority), or the
                # included platform Mistral for Pro/Enterprise/admin. Free users
                # with no personal key get None and keep the raw excerpt.
                # Best-effort: a failed summary leaves the raw excerpt.
                from app.services import rag_service
                if user.digest_summarize and rag_service.can_summarize_for_user(user):
                    _attach_summaries(user.id, articles)

                unsub_token = generate_unsubscribe_token(user)
                html = render_template(
                    "emails/recap_digest.html",
                    user=user,
                    articles=articles,
                    period_label=_period_label(since, now),
                    frequency_label=_LABEL[user.newsletter_frequency],
                    rag_url=url_for("user.dashboard", _external=True),
                    settings_url=url_for("user.account_settings", _external=True),
                    unsubscribe_url=url_for(
                        "user_tools.unsubscribe", token=unsub_token, _external=True
                    ),
                )
                tasks.append({
                    "user_id": user.id,
                    "email": user.email,
                    "html": html,
                    "freq": user.newsletter_frequency,
                    "n_articles": len(articles),
                })
            except Exception as e:
                logger.error("Erreur préparation récap user_id=%s : %s", user.id, e)

        if not tasks:
            logger.info("Aucun récap à envoyer.")
            return

        # Phase 2 (parallel I/O): dispatch the Brevo HTTP calls. Each worker
        # only does an HTTP POST, so no DB session sharing is required.
        subject = f"📬 Votre récap Myeline — {now.strftime('%d/%m/%Y')}"

        def _send_one(task):
            try:
                send_email_via_brevo_api(
                    to=task["email"],
                    subject=subject,
                    html_content=task["html"],
                )
                return (task["user_id"], task["freq"], task["n_articles"], True, None)
            except Exception as e:
                return (task["user_id"], task["freq"], task["n_articles"], False, str(e))

        # Bounded concurrent send. Each Brevo HTTP call has its own
        # 30 s timeout, but a global ceiling protects against the
        # whole batch hanging on a slow API: cancel anything still
        # queued when DIGEST_TIMEOUT seconds have elapsed and count
        # the partial successes. Without this, the cron worker could
        # get stuck for hours on a Brevo outage.
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from concurrent.futures import as_completed
        DIGEST_TIMEOUT = 600
        results = []
        executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
        try:
            futures = [executor.submit(_send_one, t) for t in tasks]
            try:
                for fut in as_completed(futures, timeout=DIGEST_TIMEOUT):
                    results.append(fut.result())
            except FuturesTimeoutError:
                cancelled = sum(1 for f in futures if f.cancel())
                logger.error(
                    "Timeout digest send après %ds (%d/%d completed, %d cancelled)",
                    DIGEST_TIMEOUT, len(results), len(futures), cancelled,
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # Phase 3 (back on main thread): persist last_digest_sent_at only for
        # successful sends, log the rest.
        # Bug M19 — ancien commit global en fin de boucle : si le worker
        # crashait à mi-chemin, tous les envois déjà partis n'avaient pas
        # leur last_digest_sent_at à jour → re-envoi des mêmes digests au
        # tick suivant. Fix : commit par envoi (coût négligeable, le bench
        # max c'est ~quelques centaines de digests/heure).
        sent = 0
        for user_id, freq, n_articles, ok, err in results:
            if ok:
                u = db.session.get(User, user_id)
                if u is not None:
                    u.last_digest_sent_at = now
                    db.session.commit()
                sent += 1
                logger.info(
                    "Récap envoyé user_id=%s freq=%s articles=%d", user_id, freq, n_articles
                )
            else:
                logger.error("Erreur envoi récap user_id=%s : %s", user_id, err)
        logger.info("Tâche terminée — %d récap(s) envoyé(s) sur %d", sent, len(tasks))

    except Exception:
        logger.error(traceback.format_exc())
