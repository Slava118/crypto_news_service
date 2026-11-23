import datetime as dt
import email.utils
import html
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .http import ServiceError, fetch


@dataclass(frozen=True)
class Feed:
    publisher: str
    url: str
    domain: str


FEEDS = (
    Feed("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "coindesk.com"),
    Feed("Cointelegraph", "https://cointelegraph.com/rss", "cointelegraph.com"),
    Feed("Decrypt", "https://decrypt.co/feed", "decrypt.co"),
    Feed("The Block", "https://www.theblock.co/rss.xml", "theblock.co"),
)


@dataclass(frozen=True)
class Article:
    publisher: str
    title: str
    description: str
    url: str
    published: str


def canonical_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").removeprefix("www.")
        if (parsed.scheme not in {"http", "https"} or "." not in host
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return ""
    except ValueError:
        return ""
    query = sorted((key, value) for key, value in parse_qsl(parsed.query)
                   if not key.lower().startswith("utm_")
                   and key.lower() not in {"fbclid", "gclid", "cmp", "ocid"})
    return urlunsplit(("https", host, parsed.path.rstrip("/") or "/", urlencode(query), ""))


def plain(text):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", text)).split())


def parse_feed(data, feed, now):
    root = ET.fromstring(data)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    articles = []
    for item in root.findall("./channel/item") + root.findall("entry"):
        link = item.findtext("link") or ""
        for node in item.findall("link"):
            if node.get("rel", "alternate") == "alternate" and node.get("href"):
                link = node.get("href")
                break
        matched_url = canonical_url(link.strip())
        host = urlsplit(matched_url).hostname or ""
        if host != feed.domain and not host.endswith("." + feed.domain):
            continue
        title = plain(item.findtext("title") or "")[:240]
        description = plain(item.findtext("description") or item.findtext("summary") or "")[:900]
        raw_date = (item.findtext("pubDate") or item.findtext("published")
                    or item.findtext("date") or item.findtext("updated") or "")
        try:
            try:
                published = dt.datetime.fromisoformat(raw_date)
            except ValueError:
                published = email.utils.parsedate_to_datetime(raw_date)
            if published.tzinfo is None:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        if title and description and now - dt.timedelta(hours=36) <= published <= now:
            # Use the original publisher URL in Telegram. Canonicalization is for matching.
            articles.append(Article(feed.publisher, title, description, link.strip(),
                                    published.astimezone(dt.UTC).isoformat()))
    return sorted(articles, key=lambda article: article.published, reverse=True)[:12]


def collect(now):
    def load(feed):
        try:
            articles = parse_feed(fetch(feed.url), feed, now)
        except ServiceError as exc:
            logging.warning("Feed unavailable: %s (%s)", feed.publisher, exc)
            return []
        except ET.ParseError:
            logging.warning("Feed contains invalid XML: %s", feed.publisher)
            return []
        logging.info("Feed %s: %d recent articles", feed.publisher, len(articles))
        return articles

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        articles = [article for batch in pool.map(load, FEEDS) for article in batch]
    unique = list({canonical_url(article.url): article for article in articles}.values())
    if len({article.publisher for article in unique}) < 2:
        raise ServiceError("Fewer than two publishers supplied recent articles")
    return unique
