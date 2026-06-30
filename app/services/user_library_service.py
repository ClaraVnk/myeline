"""
Per-plan limits for the personal library.

Direct file uploads are not exposed in any UI: the personal library
is fed exclusively via cloud connectors (Drive, OneDrive, Dropbox,
kDrive, Zotero, S3, WebDAV) and RSS / web scrapers. The limits below
are still consulted to size cloud-sync ingestion budgets.

Indexing for cloud-synced documents lives in
``app.services.cloud_sync_utils`` and ``app.services.rag_service``.
"""

from app.utils.logger import get_logger


logger = get_logger("user_library_service", subfolder="services")

# Document caps. OSS single-user build is unlimited.
# "Unlimited" is a large sentinel so the `total_docs >= max_files`
# comparison in cloud_sync_utils stays a simple int compare.
_UNLIMITED_FILES = 10_000_000

# File size cap is enforced by the cloud sync indexer. The "standard"
# entry is retained as a defensive fallback for callers that resolve
# limits when the user can't be loaded.
_LIMITS = {
    "standard": {"max_files": 500,             "max_file_size": 25  * 1024 * 1024},
    "pro":      {"max_files": _UNLIMITED_FILES, "max_file_size": 100 * 1024 * 1024},
}
_DEFAULT_LIMITS = _LIMITS["standard"]


def get_limits(user) -> dict:
    """Return {max_files, max_file_size} for the given user.

    OSS single-user build: the library is unlimited.
    """
    return _LIMITS["pro"]
