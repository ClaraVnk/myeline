import re

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import or_

from app.ext import db
from app.models.tag import Tag
from app.utils.logger import get_logger


# Bug M17 — pas de validation des caractères : \n, emoji, contrôles
# passaient ; les tags étaient .lower()+tronqués silencieusement.
# Whitelist : lettres ASCII minuscules, chiffres, espace, tiret, _, /,
# longueur 1..100. La normalisation (.lower(), trim) reste dans
# _get_or_create_tag — la validation se fait sur la valeur reçue brute.
_TAG_VALID_RE = re.compile(r"^[A-Za-z0-9 \-_/]{1,100}$")


logger = get_logger("user_tags", subfolder="routes")
user_tags_bp = Blueprint("user_tags", __name__)

_RESOURCE_LOADERS = {}


def _register_resource(resource_type, loader, owner_check):
    _RESOURCE_LOADERS[resource_type] = (loader, owner_check)


def _load_resource(resource_type, resource_id):
    entry = _RESOURCE_LOADERS.get(resource_type)
    if not entry:
        return None, "Type de ressource inconnu."
    loader, owner_check = entry
    obj = loader(resource_id)
    if obj is None:
        return None, "Ressource introuvable."
    if not owner_check(obj):
        return None, "Accès refusé."
    return obj, None


def _get_or_create_tag(name: str) -> Tag:
    """Return an existing tag owned by the current user, creating it if needed."""
    name = name.strip().lower()[:100]
    tag = Tag.query.filter_by(name=name, user_id=current_user.id).first()
    if not tag:
        tag = Tag(name=name, user_id=current_user.id)
        db.session.add(tag)
        db.session.flush()
    return tag


@user_tags_bp.route("/tags/autocomplete")
@login_required
def autocomplete():
    """Return tag suggestions for the current user matching the query prefix."""
    q = request.args.get("q", "").strip().lower()
    query = Tag.query.filter(Tag.user_id == current_user.id)
    if q:
        query = query.filter(Tag.name.ilike(f"{q}%"))
    tags = query.order_by(Tag.name).limit(20).all()
    return jsonify([{"id": t.id, "name": t.name} for t in tags])


@user_tags_bp.route("/tags/<resource_type>/<int:resource_id>", methods=["POST"])
@login_required
def assign_tag(resource_type, resource_id):
    """Add a tag (by name) to a resource. Creates the tag if it doesn't exist."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nom de tag manquant."}), 400
    if len(name) > 100:
        return jsonify({"error": "Nom de tag trop long (100 caractères max)."}), 400
    # Bug M17 — whitelist explicite des caractères.
    if not _TAG_VALID_RE.match(name):
        return jsonify({
            "error": "Nom de tag invalide (lettres, chiffres, espace, tiret, _ ou / uniquement).",
        }), 400

    obj, err = _load_resource(resource_type, resource_id)
    if err:
        return jsonify({"error": err}), 404

    tag = _get_or_create_tag(name)
    if tag not in obj.tags:
        obj.tags.append(tag)
    try:
        db.session.commit()
        logger.info("Tag '%s' ajouté à %s#%s par user_id=%s", name, resource_type, resource_id, current_user.id)
        return jsonify({"id": tag.id, "name": tag.name})
    except Exception as e:
        db.session.rollback()
        logger.error("Erreur ajout tag user_id=%s : %s", current_user.id, e)
        return jsonify({"error": "Erreur lors de l'ajout du tag."}), 500


@user_tags_bp.route("/tags/<resource_type>/<int:resource_id>/<int:tag_id>", methods=["DELETE"])
@login_required
def remove_tag(resource_type, resource_id, tag_id):
    """Remove a tag from a resource."""
    obj, err = _load_resource(resource_type, resource_id)
    if err:
        return jsonify({"error": err}), 404

    tag = db.session.get(Tag, tag_id)
    if tag and tag in obj.tags:
        obj.tags.remove(tag)
        db.session.flush()  # flush removal before orphan check (association table)
        _delete_tag_if_orphan(tag)
    try:
        db.session.commit()
        logger.info("Tag #%s retiré de %s#%s par user_id=%s", tag_id, resource_type, resource_id, current_user.id)
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        logger.error("Erreur retrait tag user_id=%s : %s", current_user.id, e)
        return jsonify({"error": "Erreur lors du retrait du tag."}), 500


def _delete_tag_if_orphan(tag: Tag) -> None:
    """Delete a tag that is no longer attached to any resource."""
    from app.models.association import cloud_document_tags, scraper_article_tags, user_document_tags
    for table in (user_document_tags, scraper_article_tags, cloud_document_tags):
        if db.session.execute(
            table.select().where(table.c.tag_id == tag.id).limit(1)
        ).first():
            return
    db.session.delete(tag)


# ---------------------------------------------------------------------------
# Resource loaders registered at module level
# ---------------------------------------------------------------------------

def _init_loaders():
    from app.models.cloud_connection import CloudDocument
    from app.models.user_document import UserDocument
    from app.models.user_scraper import UserScraperArticle

    _register_resource(
        "document",
        lambda rid: db.session.get(UserDocument, rid),
        lambda obj: obj.user_id == current_user.id,
    )
    _register_resource(
        "article",
        lambda rid: db.session.get(UserScraperArticle, rid),
        lambda obj: obj.user_id == current_user.id,
    )
    _register_resource(
        "cloud",
        lambda rid: db.session.get(CloudDocument, rid),
        lambda obj: obj.user_id == current_user.id,
    )


_init_loaders()
