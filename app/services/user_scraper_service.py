"""
Custom scraper service for Pro subscribers.

Supports two modes:
  - "rss"  : parse an RSS/Atom feed via feedparser
  - "html" : scrape article links from an HTML listing page, then fetch each article

New articles are deduplicated by URL hash and indexed into the user's
personal ChromaDB collection (user_{id}_personal) with source_type
"user_scraper_article".
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from app.utils.logger import get_logger
from app.utils.safe_http import SSRFBlockedError, safe_get


try:
    import trafilatura
    _HAS_TRAFILATURA = True
except ImportError:
    _HAS_TRAFILATURA = False


logger = get_logger("user_scraper_service", subfolder="services")

USER_SCRAPER_MAX_SOURCES = 10  # default (pro plan)

# Article storage caps (per source, in DB). OSS single-user build is
# unlimited — kept as a sentinel so callers that branch on a cap still work.
_ARTICLE_CAPS = {
    "gratuit": None,  # unlimited
}


def get_scraper_limit(user):
    """Return max scraper sources allowed for this user, or None if unlimited.

    OSS single-user build: unlimited sources.
    """
    return None


def get_article_limit(user) -> Optional[int]:
    """Return max articles stored per source, or None for unlimited.

    OSS single-user build: unlimited.
    """
    return None


_CONTENT_MAX_CHARS = 8_000
_REQUEST_TIMEOUT = 15
_ARTICLE_TIMEOUT = 10
_MAX_LINKS_SCAN = 50   # links examined per HTML listing page
_MAX_ARTICLES_PER_RUN = 20  # max new articles fetched per run
_FETCH_CONCURRENCY = 5      # parallel article fetches per HTML scraper run
_MIN_CONTENT_CHARS = 150  # articles below this threshold are skipped (paywall / nav page)
# Atom/RSS pagination caps — protect against pathological feeds.
# Most feeds expose 25-50 entries on page 1 ; quelques-uns paginent
# (Substack, Medium, blogs WordPress) via <link rel="next" href="...">.
_RSS_PAGINATION_PAGE_CAP = 100         # hard stop on number of pages
_RSS_PAGINATION_ENTRY_CAP = 5_000      # soft cap on total entries accumulated

_BROWSER_UA = (
    "Mozilla/5.0 (compatible; Myeline-bot/1.0; +https://myeline.io)"
)

# URL path segments that indicate non-article pages
_SKIP_SEGMENTS = frozenset([
    "tag", "tags", "category", "categories", "author", "authors",
    "page", "wp-admin", "feed", "rss", "atom", "login", "signup",
    "register", "about", "contact", "privacy", "terms", "sitemap",
    "search", "archive", "archives", "cdn-cgi", "wp-content",
    "wp-includes", "static", "assets", "media", "images",
])

# File extensions that are never articles
_SKIP_EXTS = frozenset([
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
    ".mp3", ".mp4", ".zip", ".tar", ".gz",
])


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode(), usedforsecurity=False).hexdigest()[:64]


def _parse_entry_content(entry) -> str:
    """Extract the best available text content from a feedparser entry."""
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].get("value", "") or ""
    if hasattr(entry, "summary"):
        return entry.summary or ""
    if hasattr(entry, "description"):
        return entry.description or ""
    return ""


# ---------------------------------------------------------------------------
# Feed / URL detection
# ---------------------------------------------------------------------------

def _try_rss(url: str) -> Optional[str]:
    """Return the RSS/Atom feed URL if one is found at or near *url*, else None.

    SSRF-safe: every fetch goes through safe_get (validates URL,
    blocks private/loopback/metadata IPs, caps body, defeats DNS
    rebinding by pinning the resolved IP). feedparser is no longer
    allowed to do its own network call — we hand it bytes we already
    fetched safely.
    """
    try:
        resp = safe_get(url, timeout=_REQUEST_TIMEOUT,
                        headers={"User-Agent": _BROWSER_UA})
    except SSRFBlockedError as exc:
        logger.warning("scraper _try_rss SSRF refusal for %r: %s", url, exc)
        return None
    except requests.RequestException:
        return None

    d = feedparser.parse(resp.content)
    if d.entries:
        return url

    # HTML autodiscovery: parse <link rel="alternate" type="application/...+xml">
    try:
        if "html" in resp.headers.get("Content-Type", ""):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text[:65536], "html.parser")
            for link in soup.find_all("link", rel="alternate"):
                t = link.get("type", "")
                if "rss" in t or "atom" in t:
                    href = link.get("href", "").strip()
                    if href:
                        feed_url = href if href.startswith("http") else urljoin(url, href)
                        try:
                            r2 = safe_get(feed_url, timeout=_REQUEST_TIMEOUT,
                                          headers={"User-Agent": _BROWSER_UA})
                        except SSRFBlockedError as exc:
                            logger.warning(
                                "scraper RSS autodiscover SSRF refusal for %r: %s",
                                feed_url, exc,
                            )
                            continue
                        except requests.RequestException:
                            continue
                        d2 = feedparser.parse(r2.content)
                        if d2.entries:
                            return feed_url
    except Exception:
        pass

    candidates = ["/feed", "/rss", "/atom", "/feed.xml", "/rss.xml", "/atom.xml",
                  "/feed/rss", "/rss/feed", "/news/feed", "/blog/feed"]
    for path in candidates:
        candidate = urljoin(url, path)
        try:
            # Bug H7 — feedparser.parse(candidate_url) faisait son propre
            # appel HTTP sans safe_http → SSRF possible (sondage interne).
            # On pré-fetch via safe_get qui bloque IPs internes / metadata
            # / DNS rebind, puis on passe le contenu déjà chargé à
            # feedparser pour qu'il ne fasse aucun appel réseau.
            try:
                r3 = safe_get(candidate, timeout=_REQUEST_TIMEOUT,
                              headers={"User-Agent": _BROWSER_UA})
            except SSRFBlockedError:
                continue
            d = feedparser.parse(r3.content)
            if d.entries:
                return candidate
        except Exception:
            continue
    return None


def detect_source(raw_url: str) -> tuple[str, str]:
    """
    Detect whether *raw_url* has an RSS/Atom feed or is a plain HTML page.

    Returns:
        (resolved_url, scraper_type)  where scraper_type is "rss" or "html"

    Raises:
        ValueError  if the URL is neither a valid feed nor a reachable HTTP page.
    """
    raw_url = raw_url.strip()
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url

    feed_url = _try_rss(raw_url)
    if feed_url:
        return feed_url, "rss"

    # Validate the URL as a reachable HTML page (SSRF-safe).
    try:
        resp = safe_get(
            raw_url,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": _BROWSER_UA},
        )
        resp.raise_for_status()
        # Confirm it's HTML-ish
        ct = resp.headers.get("Content-Type", "")
        if "html" in ct or not ct:
            return raw_url, "html"
        raise ValueError(
            f"Le type de contenu '{ct}' n'est pas supporté. "
            "Fournissez l'URL d'une page d'articles ou d'un flux RSS."
        )
    except requests.RequestException as e:
        raise ValueError(f"URL inaccessible : {e}") from e


# ---------------------------------------------------------------------------
# HTML scraping helpers
# ---------------------------------------------------------------------------

def _base_domain(netloc: str) -> str:
    """Strip 'www.' prefix so www.example.com and example.com match."""
    return netloc.lower().removeprefix("www.")


def _is_article_link(href: str, base_netloc: str) -> bool:
    """Return True if *href* looks like an article URL on the same root domain."""
    try:
        parsed = urlparse(href)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    if _base_domain(parsed.netloc) != _base_domain(base_netloc):
        return False

    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return False

    ext = path.rsplit(".", 1)[-1].lower() if "." in path.split("/")[-1] else ""
    if "." + ext in _SKIP_EXTS:
        return False

    segments = set(path.lower().strip("/").split("/"))
    if segments & _SKIP_SEGMENTS:
        return False

    return path.count("/") >= 1


def _fetch_raw(url: str, timeout: int, retries: int = 2) -> Optional[requests.Response]:
    """GET with simple retry/backoff on 429 / 5xx (SSRF-safe)."""
    for attempt in range(retries + 1):
        try:
            resp = safe_get(
                url,
                timeout=timeout,
                headers={"User-Agent": _BROWSER_UA},
            )
            if resp.status_code == 429 and attempt < retries:
                retry_after = int(resp.headers.get("Retry-After", 5))
                time.sleep(min(retry_after, 10))
                continue
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt == retries:
                logger.debug(f"_fetch_raw failed for {url}: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def _try_sitemap(base_url: str, base_netloc: str) -> list[str]:
    """
    Try to fetch sitemap.xml (and sitemap index) to collect article URLs.
    Returns a list of candidate URLs from the same domain.
    """
    candidates = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/sitemap_index.xml"),
        urljoin(base_url, "/sitemap-news.xml"),
    ]
    found: list[str] = []
    for sitemap_url in candidates:
        resp = _fetch_raw(sitemap_url, timeout=8, retries=1)
        if not resp:
            continue
        ct = resp.headers.get("Content-Type", "")
        if "xml" not in ct and not resp.text.strip().startswith("<?xml"):
            continue
        try:
            soup = BeautifulSoup(resp.text, "xml")
            # Sitemap index: recurse one level
            for loc in soup.find_all("sitemap"):
                child_loc = loc.find("loc")
                if not child_loc:
                    continue
                child_url = child_loc.get_text(strip=True)
                child_resp = _fetch_raw(child_url, timeout=8, retries=1)
                if child_resp:
                    child_soup = BeautifulSoup(child_resp.text, "xml")
                    for url_tag in child_soup.find_all("url"):
                        u = url_tag.find("loc")
                        if u:
                            found.append(u.get_text(strip=True))
            # Regular sitemap
            for url_tag in soup.find_all("url"):
                u = url_tag.find("loc")
                if u:
                    found.append(u.get_text(strip=True))
        except Exception:
            continue
        if found:
            break

    return [u for u in found if _is_article_link(u, base_netloc)]


def _find_article_links(listing_url: str) -> list[str]:
    """
    Return up to _MAX_LINKS_SCAN article-like URLs from *listing_url*.

    Strategy (in priority order):
    1. Links inside semantic containers (article, main, [role=main])
    2. All same-domain links from the full page
    3. Sitemap.xml fallback (if fewer than 5 links found above)

    Results are de-duplicated and sorted by path depth (deepest = most specific).
    """
    resp = _fetch_raw(listing_url, timeout=_REQUEST_TIMEOUT)
    if not resp:
        logger.warning(f"_find_article_links: could not fetch {listing_url}")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    base_netloc = urlparse(listing_url).netloc
    seen: set[str] = set()

    def _collect(tags) -> list[str]:
        found = []
        for a in tags:
            href = a.get("href", "").strip()
            if not href:
                continue
            href = urljoin(listing_url, href).split("#")[0].split("?")[0]
            if href in seen:
                continue
            seen.add(href)
            if _is_article_link(href, base_netloc):
                found.append(href)
        return found

    # Priority: links within semantic article containers
    priority_links: list[str] = []
    for container in soup.select("article, main, [role='main'], .post-listing, .article-list, #content"):
        priority_links.extend(_collect(container.find_all("a", href=True)))

    # All remaining links on the page
    all_links = _collect(soup.find_all("a", href=True))

    merged = priority_links + [u for u in all_links if u not in set(priority_links)]

    # Sitemap fallback when the listing page has few links
    if len(merged) < 5:
        sitemap_links = _try_sitemap(listing_url, base_netloc)
        for u in sitemap_links:
            if u not in seen:
                merged.append(u)

    merged.sort(key=lambda u: (-urlparse(u).path.count("/"), -len(urlparse(u).path)))
    return merged[:_MAX_LINKS_SCAN]


def _extract_date(soup: BeautifulSoup) -> Optional[datetime]:
    """Extract publication date from common meta/LD+JSON patterns."""
    # Open Graph / Dublin Core meta tags
    for prop in [
        "article:published_time", "og:article:published_time",
        "article:modified_time", "datePublished",
    ]:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            try:
                return datetime.fromisoformat(tag["content"].replace("Z", "+00:00"))
            except Exception:
                pass

    for name in ["pubdate", "date", "publishdate", "article.published",
                 "DC.date.issued", "dcterms.created", "publication_date"]:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            try:
                return datetime.fromisoformat(tag["content"].replace("Z", "+00:00"))
            except Exception:
                pass

    # JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0] if data else {}
            date_str = data.get("datePublished") or data.get("dateCreated")
            if date_str:
                return datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        except Exception:
            pass

    # <time datetime="..."> elements
    for time_tag in soup.find_all("time", datetime=True):
        try:
            return datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
        except Exception:
            pass

    return None


def _fetch_article(url: str) -> tuple[str, str, Optional[datetime]]:
    """
    Fetch *url* and return (title, plain_text_content, published_at).
    Returns ("", "", None) on failure.

    Uses trafilatura as primary extractor (better boilerplate removal and
    language agnosticism) with a BeautifulSoup fallback.
    """
    resp = _fetch_raw(url, timeout=_ARTICLE_TIMEOUT)
    if not resp:
        return "", "", None

    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    # --- Publication date ---
    published_at = _extract_date(soup)

    # --- Title: og:title > h1 > <title> ---
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        t_tag = soup.find("title")
        if t_tag:
            title = t_tag.get_text(strip=True)

    # --- Content: trafilatura first, BeautifulSoup fallback ---
    content = ""

    if _HAS_TRAFILATURA:
        try:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                favor_precision=True,
                deduplicate=True,
            )
            if extracted:
                content = extracted
        except Exception as e:
            logger.debug(f"trafilatura failed for {url}: {e}")

    if not content:
        # BeautifulSoup fallback: semantic containers then <p> tags
        for tag in soup(["nav", "footer", "header", "aside", "script", "style", "noscript"]):
            tag.decompose()

        for selector in [
            "article", "main", "[role='main']",
            ".post-content", ".entry-content", ".article-body",
            ".article-content", ".content-body", ".story-body",
            ".post-body", ".td-post-content", ".page-content",
        ]:
            container = soup.select_one(selector)
            if container:
                content = container.get_text(separator="\n", strip=True)
                break

        if not content:
            paras = [p.get_text(strip=True) for p in soup.find_all("p")]
            content = "\n".join(p for p in paras if len(p) > 50)

    # Last-resort: meta description as a minimal summary
    if len(content) < _MIN_CONTENT_CHARS:
        desc = soup.find("meta", attrs={"name": "description"}) or \
               soup.find("meta", property="og:description")
        if desc and desc.get("content"):
            content = content + "\n" + desc["content"].strip() if content else desc["content"].strip()

    return title[:500], content[:_CONTENT_MAX_CHARS], published_at


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def _bump_article_count(db, scraper_id: int) -> None:
    """Increment user_scrapers.{article_count,scrape_processed} atomiquement.

    Permet à l'UI dashboard (banner de progression) et à l'admin/scrapers
    de voir la progression EN COURS de batch. UPDATE direct one-row + commit
    rapide, compatible avec le pattern "commit per article" du fix Lock wait
    timeout (cf. _index_article).

    article_count = total cumulé (compteur produit du scraper depuis sa création)
    scrape_processed = compteur du run actuel (remis à 0 au début de scrape_source)
    """
    from app.models.user_scraper import UserScraper
    db.session.query(UserScraper).filter(UserScraper.id == scraper_id).update(
        {
            UserScraper.article_count: UserScraper.article_count + 1,
            UserScraper.scrape_processed: UserScraper.scrape_processed + 1,
        },
        synchronize_session=False,
    )
    db.session.commit()


def _index_article(
    *,
    scraper_id: int,
    user_id: int,
    scraper_name: str,
    url: str,
    title: str,
    content: str,
    published_at: Optional[datetime],
    collection,
    db,
    UserScraperArticle,
    chunk_text,
    _embed,
) -> bool:
    """
    Create a UserScraperArticle row, chunk, embed, and upsert into ChromaDB.
    Returns True on success, False if rejected (quality) or on error.
    """
    # Quality gate: skip articles with no usable content
    clean_content = (content or "").strip()
    if len(clean_content) < _MIN_CONTENT_CHARS:
        logger.debug(f"Skipping low-content article ({len(clean_content)} chars): {url[:80]}")
        return False
    # Skip articles whose title is just their own URL (navigation / listing pages)
    if title.strip().rstrip("/") == url.rstrip("/"):
        logger.debug(f"Skipping title=url article: {url[:80]}")
        return False

    article = UserScraperArticle(
        scraper_id=scraper_id,
        user_id=user_id,
        url_hash=_url_hash(url),
        url=url,
        title=title[:500],
        content=content[:_CONTENT_MAX_CHARS] if content else None,
        published_at=published_at,
        created_at=datetime.now(timezone.utc),
    )

    try:
        # IMPORTANT — commit par article, pas par batch :
        # Sans ça, la transaction tient des locks gap+record sur la table
        # `user_scraper_articles` pendant TOUTE la boucle de scraping.
        # Embed Ollama = 5-30s par article → pour N articles la transaction
        # dure N × 10s+. Une autre tentative d'INSERT (re-scrape concurrent,
        # autre cron) timeout sur `innodb_lock_wait_timeout=50s` par défaut.
        # En committant ici, chaque article est sa propre micro-transaction.
        db.session.add(article)
        db.session.flush()

        full_text = f"{title}\n\n{content}".strip() if content else title

        # Per-tier token budget — a scraper that returns a massive HTML page
        # (huge static dump, malformed extraction) shouldn't burn through
        # the user's embed budget. Same caps as cloud/user docs.
        try:
            from app.models.user import User
            from app.services.rag_service import check_doc_token_budget
            _u = db.session.get(User, user_id)
            _budget_err = check_doc_token_budget(full_text, _u.abonnement_type if _u else None)
            if _budget_err:
                logger.warning("Scraper article rejected user_id=%s url=%s: %s",
                               user_id, url[:80], _budget_err)
                db.session.rollback()
                return False
        except Exception as _e:
            logger.debug("Budget check skipped for scraper article: %s", _e)

        chunks = chunk_text(full_text) or [full_text[:1000]]

        ids = [f"userscraper_{article.id}_chunk_{i}" for i in range(len(chunks))]
        from app.services.rag_service import _doc_date_meta
        doc_date = _doc_date_meta(published_at) if published_at else {}
        metadatas = [
            {
                "source_type": "user_scraper_article",
                "scraper_id": scraper_id,
                "article_id": article.id,
                "user_id": user_id,
                "source_name": scraper_name,
                "article_title": title[:255],
                "article_url": url,
                "tag": "",
                "published_at": published_at.isoformat() if published_at else "",
                "indexed_at": datetime.now(tz=timezone.utc).isoformat(),
                **doc_date,
            }
            for _ in chunks
        ]

        embeddings = _embed(chunks, doc_title=title, user_id=user_id)
        collection.upsert(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)
        article.indexed_at = datetime.now(timezone.utc)
        article.chunk_count = len(chunks)
        db.session.commit()
        return True
    except Exception as e:
        from app.services.rag_service import EmbedKeyRequiredError
        if isinstance(e, EmbedKeyRequiredError):
            # Free tier with no embedding key — no platform fallback (€0 rule).
            logger.warning(
                "Article not indexed: user %s has no embedding key (Free, no platform fallback)",
                user_id,
            )
            db.session.rollback()
            return False
        logger.error(f"Embed/upsert failed for article {url[:60]}: {e}")
        # Rollback annule l'INSERT en cours — pas d'article orphelin en DB,
        # pas de chunks orphelins côté Chroma (upsert n'a pas eu lieu).
        db.session.rollback()
        return False


def scrape_source(scraper_id: int) -> tuple[int, Optional[str]]:
    """
    Fetch and index new articles for a UserScraper.
    Dispatches to RSS or HTML mode based on scraper.scraper_type.
    Returns (new_articles_count, error_message_or_None).
    """
    from app.ext import db
    from app.models.user import User
    from app.models.user_scraper import UserScraper, UserScraperArticle
    from app.services.rag_service import _embed, _get_user_collection
    from app.utils.text_extraction import chunk_text

    scraper = db.session.get(UserScraper, scraper_id)
    if not scraper or not scraper.is_active:
        return 0, "Scraper introuvable ou inactif."

    user_id = scraper.user_id
    collection = _get_user_collection(user_id)
    existing_count = UserScraperArticle.query.filter_by(scraper_id=scraper_id).count()

    user = db.session.get(User, user_id)
    # Fall back to the strictest cap (Free plan) when the user can't be
    # resolved — defensive default so a deleted user doesn't get unlimited storage.
    max_articles = get_article_limit(user) if user else _ARTICLE_CAPS["gratuit"]

    scraper_type = getattr(scraper, "scraper_type", "rss") or "rss"

    # Pose le flag is_scraping pour que la banner du dashboard utilisateur
    # affiche la progression. Un try/finally garantit qu'on lève toujours
    # le flag, même en cas d'exception non capturée par les sub-fonctions.
    scraper.is_scraping = True
    scraper.scrape_started_at = datetime.now(timezone.utc)
    scraper.scrape_total = None
    scraper.scrape_processed = 0
    db.session.commit()

    new_count, err = 0, None
    try:
        if scraper_type == "html":
            new_count, err = _run_html_scrape(
                scraper=scraper,
                user_id=user_id,
                existing_count=existing_count,
                max_articles=max_articles,
                collection=collection,
                db=db,
                UserScraperArticle=UserScraperArticle,
                chunk_text=chunk_text,
                _embed=_embed,
            )
        else:
            new_count, err = _run_rss_scrape(
                scraper=scraper,
                user_id=user_id,
                existing_count=existing_count,
                max_articles=max_articles,
                collection=collection,
                db=db,
                UserScraperArticle=UserScraperArticle,
                chunk_text=chunk_text,
                _embed=_embed,
            )
    finally:
        scraper.last_scraped_at = datetime.now(timezone.utc)
        # `.count()` reflète déjà l'état post-commits per-article fait dans
        # _index_article — plus besoin d'y ajouter new_count (double-comptage).
        scraper.article_count = UserScraperArticle.query.filter_by(scraper_id=scraper_id).count()
        scraper.last_error = err
        scraper.is_scraping = False
        db.session.commit()

    logger.info(
        f"Scraper {scraper_id} ({scraper.name}) [{scraper_type}]: "
        f"{new_count} new articles for user {user_id}"
    )
    return new_count, err


_BLOGGER_FEED_PATH = "/feeds/posts/default"
_BLOGGER_PAGE_SIZE = 500


def _blogger_pagination_url(start_url: str, start_index: int) -> Optional[str]:
    """Build the next Blogger feed URL using ?start-index= cursor.

    Blogger Atom feeds default to 25 entries/page and never expose
    ``rel="next"``, so the generic pagination code above gives up after
    page 1. Without this, a user who opted into a since-date covering
    months of history only ever gets the trailing 25 posts — which
    makes the since-date UI a lie. Returns None when the URL doesn't
    look like a Blogger feed.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(start_url)
    if _BLOGGER_FEED_PATH not in parts.path:
        return None
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))
    qs["start-index"] = str(start_index)
    qs.setdefault("max-results", str(_BLOGGER_PAGE_SIZE))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(qs), parts.fragment))


def _fetch_atom_pages(
    start_url: str,
    since: Optional[datetime],
) -> tuple[list, Optional[str]]:
    """Fetch start_url and follow Atom/RSS <link rel="next" href="..."> pages.

    Most feeds n'ont qu'une page (25-50 entries) — auquel cas cette fonction
    se comporte comme un simple `feedparser.parse(safe_get(url).content)`.
    Pour les feeds paginés (Substack, certains WordPress), on suit les
    rel="next" jusqu'à :
      - atteindre _RSS_PAGINATION_PAGE_CAP pages (safety),
      - accumuler _RSS_PAGINATION_ENTRY_CAP entries (soft cap),
      - tomber sur une entry plus ancienne que `since` (les pages
        suivantes ne contiendront que du plus vieux encore),
      - détecter une boucle (URL déjà vue — flux mal configuré).

    Blogger feeds (/feeds/posts/default) ne publient jamais rel="next"
    et capent à 25 entries par défaut — on bascule explicitement sur
    leur cursor ?start-index= dès qu'on les détecte, sinon la
    since-date proposée à l'user ne sert à rien.

    Toute requête réseau passe par safe_get (audit SSRF C-1).

    Returns (entries_list, error_message_or_None). En cas d'erreur sur une
    page > 1, on retourne ce qu'on a déjà accumulé + un message non bloquant.
    """
    # Bump Blogger's default 25/page to 500 on the very first request
    # so we don't waste a round-trip on the small page.
    if _BLOGGER_FEED_PATH in start_url and "max-results=" not in start_url:
        first = _blogger_pagination_url(start_url, start_index=1)
        if first:
            start_url = first

    entries: list = []
    seen_urls: set[str] = set()
    url: Optional[str] = start_url
    page = 0
    while url and page < _RSS_PAGINATION_PAGE_CAP:
        if url in seen_urls:
            logger.warning("RSS pagination loop detected at %s", url)
            break
        seen_urls.add(url)
        page += 1
        try:
            resp = safe_get(url, timeout=_REQUEST_TIMEOUT,
                            headers={"User-Agent": _BROWSER_UA})
            feed = feedparser.parse(resp.content)
        except SSRFBlockedError as exc:
            if page == 1:
                return entries, f"URL refusée : {exc}"
            logger.warning("Pagination page %d refused: %s", page, exc)
            break
        except Exception as e:
            if page == 1:
                return entries, f"Erreur de lecture du flux : {e}"
            logger.warning("Pagination page %d failed: %s", page, e)
            break

        page_entries = list(feed.entries or [])
        entries.extend(page_entries)

        if len(entries) >= _RSS_PAGINATION_ENTRY_CAP:
            logger.info(
                "RSS pagination soft cap hit at %d entries (page %d) on %s",
                len(entries), page, start_url,
            )
            break

        # Si l'entry la plus ancienne de cette page est déjà avant `since`,
        # les pages suivantes (plus anciennes encore en ordre chronologique
        # décroissant) ne contiennent que du hors-périmètre.
        if since and page_entries:
            oldest = page_entries[-1]
            published_at = None
            if hasattr(oldest, "published_parsed") and oldest.published_parsed:
                try:
                    published_at = datetime(*oldest.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass
            if published_at and published_at < since:
                break

        # Recherche du rel="next" dans les links du feed (Atom standard,
        # supporté par feedparser via feed.feed.links).
        # Bug N8 (audit round 2) — un flux malveillant pouvait pointer
        # rel="next" vers un domaine arbitraire, transformant la plateforme
        # en proxy pour scraper / bomber des cibles tierces. Pagination
        # Atom est par design intra-feed → on contraint same-netloc.
        from urllib.parse import urlparse as _urlparse
        start_netloc = _urlparse(start_url).netloc.lower()
        next_url: Optional[str] = None
        feed_meta = getattr(feed, "feed", None)
        for link in (getattr(feed_meta, "links", None) or []):
            if isinstance(link, dict) and link.get("rel") == "next":
                href = link.get("href")
                if href and _urlparse(href).netloc.lower() == start_netloc:
                    next_url = href
                    break
                elif href:
                    logger.warning(
                        "RSS pagination rel=next cross-domain refused: %s → %s",
                        start_url, href,
                    )

        # Blogger fallback: no rel="next" is ever advertised. If we got
        # back a full page (== _BLOGGER_PAGE_SIZE), advance the
        # start-index cursor and keep going; if the page came back
        # short, we've reached the tail.
        if next_url is None and _BLOGGER_FEED_PATH in url:
            if len(page_entries) >= _BLOGGER_PAGE_SIZE:
                next_url = _blogger_pagination_url(
                    start_url, start_index=len(entries) + 1,
                )

        url = next_url

    return entries, None


def _run_rss_scrape(
    *, scraper, user_id, existing_count, max_articles, collection, db,
    UserScraperArticle, chunk_text, _embed,
) -> tuple[int, Optional[str]]:
    # SSRF-safe fetch via _fetch_atom_pages — follows Atom <link rel="next">
    # pagination if present, sinon comportement strictement identique à un
    # simple feedparser.parse de la page 1.
    since: Optional[datetime] = getattr(scraper, "scrape_since", None)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    all_entries, err = _fetch_atom_pages(scraper.url, since)
    if not all_entries:
        return 0, err or "Flux vide ou inaccessible."

    # Without a since-date set, cap to 50 to keep first-run fast.
    # Avec since, on peut aller jusqu'au cap de pagination (5000) puisque
    # l'objectif est explicitement de rattraper l'historique.
    entry_limit = _RSS_PAGINATION_ENTRY_CAP if since else 50
    entries = all_entries[:entry_limit]

    # Pre-compute URL hashes and run a single IN query to find which ones
    # already exist — was N+1 (one SELECT per entry). Perf audit H5.
    candidate_hashes = []
    for entry in entries:
        url = getattr(entry, "link", None) or getattr(entry, "id", None) or ""
        if url:
            candidate_hashes.append(_url_hash(url))
    existing_hashes = (
        {row[0] for row in db.session.query(UserScraperArticle.url_hash)
         .filter(UserScraperArticle.url_hash.in_(candidate_hashes)).all()}
        if candidate_hashes else set()
    )

    # Total estimé de nouveaux articles à indexer (pour la banner UI).
    # On compte les entries dont le hash n'est pas déjà connu — c'est le
    # majorant ; les filtres ultérieurs (date, content vide…) peuvent
    # encore réduire le nombre réel d'inserts.
    scraper.scrape_total = sum(1 for h in candidate_hashes if h not in existing_hashes)
    db.session.commit()

    new_count = 0
    for entry in entries:
        url = getattr(entry, "link", None) or getattr(entry, "id", None) or ""
        if not url:
            continue

        uhash = _url_hash(url)
        if uhash in existing_hashes:
            continue

        if max_articles is not None and existing_count + new_count >= max_articles:
            break

        title = getattr(entry, "title", "") or url
        raw_content = _parse_entry_content(entry)
        if raw_content:
            raw_content = BeautifulSoup(raw_content, "html.parser").get_text(separator="\n", strip=True)
        raw_content = raw_content[:_CONTENT_MAX_CHARS]

        published_at = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass

        # Skip entries older than scrape_since (entries without a date are kept)
        if since and published_at and published_at < since:
            continue

        ok = _index_article(
            scraper_id=scraper.id, user_id=user_id, scraper_name=scraper.name,
            url=url, title=title, content=raw_content, published_at=published_at,
            collection=collection, db=db, UserScraperArticle=UserScraperArticle,
            chunk_text=chunk_text, _embed=_embed,
        )
        if ok:
            new_count += 1
            _bump_article_count(db, scraper.id)

    return new_count, None


def _run_html_scrape(
    *, scraper, user_id, existing_count, max_articles, collection, db,
    UserScraperArticle, chunk_text, _embed,
) -> tuple[int, Optional[str]]:
    links = _find_article_links(scraper.url)
    if not links:
        return 0, "Aucun lien d'article trouvé sur cette page. Vérifiez l'URL."

    since: Optional[datetime] = getattr(scraper, "scrape_since", None)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    # Filter out URLs we've already indexed (single IN query — was a
    # SELECT per URL, perf audit H5) and cap to the per-run budget.
    link_hashes = [_url_hash(url) for url in links]
    existing_hashes = (
        {row[0] for row in db.session.query(UserScraperArticle.url_hash)
         .filter(UserScraperArticle.url_hash.in_(link_hashes)).all()}
        if link_hashes else set()
    )
    candidate_urls = []
    for url, uhash in zip(links, link_hashes):
        if max_articles is not None and existing_count + len(candidate_urls) >= max_articles:
            break
        if len(candidate_urls) >= _MAX_ARTICLES_PER_RUN:
            break
        if uhash in existing_hashes:
            continue
        candidate_urls.append(url)

    if not candidate_urls:
        return 0, "Aucun nouvel article indexé (contenu déjà connu ou extraction échouée)."

    # Total estimé pour la banner UI (majorant — les filtres post-fetch
    # peuvent encore réduire).
    scraper.scrape_total = len(candidate_urls)
    db.session.commit()

    # Parallel HTTP fetch — concurrency-bounded to avoid hammering the source.
    # Each _fetch_article() is pure I/O (urllib + trafilatura) and thread-safe.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=_FETCH_CONCURRENCY) as pool:
        fetched_results = list(pool.map(_fetch_article, candidate_urls))

    # Sequential index writes — keeps the SQLAlchemy session single-threaded
    # and the ChromaDB collection (`collection`) simple to reason about.
    new_count = 0
    for url, (title, content, published_at) in zip(candidate_urls, fetched_results):
        if not title and not content:
            continue
        if since and published_at and published_at < since:
            continue
        if not title:
            title = url
        ok = _index_article(
            scraper_id=scraper.id, user_id=user_id, scraper_name=scraper.name,
            url=url, title=title, content=content, published_at=published_at,
            collection=collection, db=db, UserScraperArticle=UserScraperArticle,
            chunk_text=chunk_text, _embed=_embed,
        )
        if ok:
            new_count += 1
            _bump_article_count(db, scraper.id)

    err = None if new_count > 0 else "Aucun nouvel article indexé (contenu déjà connu ou extraction échouée)."
    return new_count, err


# ---------------------------------------------------------------------------
# Deletion / cron helpers
# ---------------------------------------------------------------------------

def delete_scraper_from_index(scraper_id: int, user_id: int) -> None:
    """Remove all ChromaDB entries for a scraper's articles."""
    from app.models.user_scraper import UserScraperArticle
    from app.services.rag_service import _get_user_collection

    articles = UserScraperArticle.query.filter_by(scraper_id=scraper_id).all()
    ids_to_delete = []
    for art in articles:
        for i in range(art.chunk_count):
            ids_to_delete.append(f"userscraper_{art.id}_chunk_{i}")

    if ids_to_delete:
        try:
            collection = _get_user_collection(user_id)
            collection.delete(ids=ids_to_delete)
        except Exception as e:
            logger.error(f"Failed to remove scraper {scraper_id} from index: {e}")


def scrape_all_for_user(user_id: int) -> int:
    """Run all active scrapers for a given user. Returns total new articles."""
    from app.models.user_scraper import UserScraper
    scrapers = UserScraper.query.filter_by(user_id=user_id, is_active=True).all()
    total = 0
    for scraper in scrapers:
        n, _ = scrape_source(scraper.id)
        total += n
    return total


_FREQUENCY_SECONDS = {
    "daily": 86_400,
    "weekly": 7 * 86_400,
    "monthly": 30 * 86_400,
}


def _scraper_is_due(scraper) -> bool:
    """Return True if the scraper's frequency window has elapsed since last run."""
    window = _FREQUENCY_SECONDS.get(scraper.scrape_frequency or "daily", 86_400)
    if scraper.last_scraped_at is None:
        return True
    age = (datetime.now(timezone.utc) - scraper.last_scraped_at.replace(tzinfo=timezone.utc)).total_seconds()
    return age >= window


def scrape_all_users() -> dict:
    """Run active scrapers for Pro users, respecting each scraper's frequency. Called by cron."""
    from app.models.user import User
    from app.models.user_scraper import UserScraper

    # OSS self-host: scrapers run for every user (no paid-tier gating).
    pro_users = User.query.all()

    results = {"users": 0, "total_new_articles": 0, "errors": 0, "skipped": 0}
    for user in pro_users:
        scrapers = UserScraper.query.filter_by(user_id=user.id, is_active=True).all()
        if not scrapers:
            continue
        user_ran = False
        for scraper in scrapers:
            if not _scraper_is_due(scraper):
                results["skipped"] += 1
                continue
            user_ran = True
            n, err = scrape_source(scraper.id)
            results["total_new_articles"] += n
            if err:
                results["errors"] += 1
        if user_ran:
            results["users"] += 1

    logger.info(
        f"scrape_all_users: {results['users']} users, "
        f"{results['total_new_articles']} new articles, "
        f"{results['errors']} errors, {results['skipped']} skipped (frequency)"
    )
    return results
