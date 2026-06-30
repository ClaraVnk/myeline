from datetime import datetime, timezone

from app.ext import db


SUPPORTED_PROVIDERS = ("gdrive", "onedrive", "dropbox", "kdrive", "zotero", "s3", "notion", "webdav")

PROVIDER_LABELS = {
    "gdrive": "Google Drive",
    "onedrive": "OneDrive",
    "dropbox": "Dropbox",
    "kdrive": "kDrive (Infomaniak)",
    "zotero": "Zotero",
    "s3": "Amazon S3 / S3-compatible",
    "notion": "Notion",
    "webdav": "WebDAV (Nextcloud…)",
}


class CloudConnection(db.Model):  # type: ignore
    __tablename__ = "cloud_connections"
    __table_args__ = (
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider = db.Column(db.String(20), nullable=False)
    access_token_enc = db.Column(db.Text, nullable=False)
    refresh_token_enc = db.Column(db.Text, nullable=True)
    token_expiry = db.Column(db.DateTime, nullable=True)
    # NULL = entire Drive; set to a folder ID to restrict scope
    folder_id = db.Column(db.String(255), nullable=True)
    folder_name = db.Column(db.String(255), nullable=True)
    # Provider-specific delta cursor (e.g. Drive pageToken)
    sync_cursor = db.Column(db.Text, nullable=True)
    last_sync_at = db.Column(db.DateTime, nullable=True)
    sync_frequency_minutes = db.Column(db.Integer, nullable=False, default=240, server_default="240")
    is_syncing = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    # Progress reporting for the in-flight sync (NULL outside an active run).
    # Set when the file list is resolved, incremented as process_and_index
    # finishes each item, cleared when the sync exits (success or failure).
    sync_started_at = db.Column(db.DateTime, nullable=True)
    sync_total_files = db.Column(db.Integer, nullable=True)
    sync_processed_files = db.Column(db.Integer, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    user = db.relationship(
        "User",
        backref=db.backref("cloud_connections", lazy="dynamic", cascade="all, delete-orphan"),
    )
    documents = db.relationship(
        "CloudDocument",
        backref="connection",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    @property
    def document_count(self) -> int:
        return self.documents.count()

    @property
    def indexed_count(self) -> int:
        return self.documents.filter(CloudDocument.indexed_at.isnot(None)).count()


class CloudDocument(db.Model):  # type: ignore
    __tablename__ = "cloud_documents"
    __table_args__ = (
        db.UniqueConstraint(
            "connection_id", "remote_id", name="uq_cloud_doc_connection_remote"
        ),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(
        db.Integer,
        db.ForeignKey("cloud_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remote_id = db.Column(db.String(500), nullable=False)
    name = db.Column(db.String(500), nullable=False)
    mime_type = db.Column(db.String(200), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    remote_modified_at = db.Column(db.DateTime, nullable=True)
    indexed_at = db.Column(db.DateTime, nullable=True)
    chunk_count = db.Column(db.Integer, nullable=False, default=0)
    # Public-facing URL where the user can open the document on the cloud
    # provider's web UI (Drive webViewLink, OneDrive webUrl, Dropbox
    # share-style preview, kDrive WebDAV browse URL, Zotero item URL,
    # S3 console object URL). Surfaced in RAG result sources so users
    # can jump straight back to the original.
    web_url = db.Column(db.String(2000), nullable=True)
    tags = db.relationship(
        "Tag",
        secondary="cloud_document_tags",
        lazy="select",
        order_by="Tag.name",
    )
