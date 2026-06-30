"""
Expose the main SQLAlchemy models so that
`from app.models import ModelName` works.
"""

from .association import cloud_document_tags, scraper_article_tags, user_document_tags
from .audit_log import AuditLog
from .cloud_connection import CloudConnection, CloudDocument
from .conversation import Conversation, ConversationTurn
from .newsletter import Newsletter
from .newsletter_section import NewsletterSection
from .query_history import QueryHistory, SavedQuery
from .rag_feedback import RagFeedback
from .shared_rag_result import SharedRagResult
from .sharing import DocumentShare, ScraperShare
from .status_sample import StatusSample
from .support_ticket import SupportTicket, TicketMessage
from .tag import Tag
from .user import User
from .user_document import UserDocument
from .user_scraper import UserScraper, UserScraperArticle
from .watch_alert import WatchAlert


__all__ = [
    "User",
    "Newsletter",
    "Tag",
    "NewsletterSection",
    "SharedRagResult",
    "UserDocument",
    "UserScraper",
    "UserScraperArticle",
    "ScraperShare",
    "DocumentShare",
    "CloudConnection",
    "CloudDocument",
    "user_document_tags",
    "scraper_article_tags",
    "cloud_document_tags",
    "SupportTicket",
    "TicketMessage",
    "RagFeedback",
    "QueryHistory",
    "SavedQuery",
    "WatchAlert",
    "Conversation",
    "ConversationTurn",
    "AuditLog",
    "StatusSample",
]
