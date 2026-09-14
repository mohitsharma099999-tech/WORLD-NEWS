"""
Global News Aggregator — RSS-based
Fetches headlines from curated RSS/Atom feeds, organized by ISO country codes.

Feed source priority (no auto-refresh by default):
  1. .cache/feeds.json          (used as-is, never expires)
  2. feeds_compact.json         (used if cache missing; seeds the cache)
  3. Bundled FALLBACK_FEEDS     (offline safety net)
  4. GitHub                     (only when you explicitly refresh)

Sources:
  • Feed list : https://github.com/mohitsharma099999-tech/news-feed-list-of-countries
                (active-feeds-auto-generated.json)
"""

import feedparser
import requests
import time
import random
import logging
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Iterable
from urllib.parse import urlparse

# ─────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rss_news")


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────
GITHUB_FEED_URL = (
    "https://raw.githubusercontent.com/"
    "mohitsharma099999-tech/news-feed-list-of-countries/"
    "master/active-feeds-auto-generated.json"
)

# ── Paths (absolute, so they survive CWD changes) ──────────────────
APP_DIR = Path.home() / ".rss_news_aggregator"
CACHE_FILE = APP_DIR / "feeds.json"
COMPACT_FILE = APP_DIR / "feeds_compact.json"
LEGACY_COMPACT_FILE = Path("feeds_compact.json")   # in case you ran the old script here

# Bundled fallback so the scraper works offline / when GitHub is down.
FALLBACK_FEEDS: dict[str, list[dict]] = {
    "USA": [
        {
            "publication_name": "NPR",
            "publication_website_uri": "https://www.npr.org/",
            "publication_rss_feed_uris": [
                {"uri": "https://feeds.npr.org/1001/rss.xml",
                 "language_code": "en", "language_name": "English"}
            ],
        },
        {
            "publication_name": "ABC News",
            "publication_website_uri": "https://abcnews.go.com/",
            "publication_rss_feed_uris": [
                {"uri": "https://abcnews.go.com/abcnews/topstories",
                 "language_code": "en", "language_name": "English"}
            ],
        },
    ],
    "GBR": [
        {
            "publication_name": "BBC News",
            "publication_website_uri": "https://www.bbc.com/news",
            "publication_rss_feed_uris": [
                {"uri": "https://feeds.bbci.co.uk/news/rss.xml",
                 "language_code": "en", "language_name": "English"}
            ],
        },
        {
            "publication_name": "The Guardian",
            "publication_website_uri": "https://www.theguardian.com/",
            "publication_rss_feed_uris": [
                {"uri": "https://www.theguardian.com/world/rss",
                 "language_code": "en", "language_name": "English"}
            ],
        },
    ],
    "IND": [
        {
            "publication_name": "NDTV",
            "publication_website_uri": "https://www.ndtv.com/",
            "publication_rss_feed_uris": [
                {"uri": "https://feeds.feedburner.com/ndtvnews-top-stories",
                 "language_code": "en", "language_name": "English"}
            ],
        },
        {
            "publication_name": "The Hindu",
            "publication_website_uri": "https://www.thehindu.com/",
            "publication_rss_feed_uris": [
                {"uri": "https://www.thehindu.com/news/national/feeder/default.rss",
                 "language_code": "en", "language_name": "English"}
            ],
        },
    ],
    "DEU": [
        {
            "publication_name": "Tagesschau",
            "publication_website_uri": "https://www.tagesschau.de/",
            "publication_rss_feed_uris": [
                {"uri": "https://www.tagesschau.de/xml/rss2/",
                 "language_code": "de", "language_name": "German"}
            ],
        },
    ],
    "JPN": [
        {
            "publication_name": "Japan Times",
            "publication_website_uri": "https://www.japantimes.co.jp/",
            "publication_rss_feed_uris": [
                {"uri": "https://www.japantimes.co.jp/feed/",
                 "language_code": "en", "language_name": "English"}
            ],
        },
    ],
    "AUS": [
        {
            "publication_name": "ABC News",
            "publication_website_uri": "https://www.abc.net.au/news",
            "publication_rss_feed_uris": [
                {"uri": "https://www.abc.net.au/news/feed/51120/rss.xml",
                 "language_code": "en", "language_name": "English"}
            ],
        },
    ],
}

# ISO 3166-1 alpha-3 → country name
COUNTRY_NAMES = {
    "AFG": "Afghanistan", "ALB": "Albania", "DZA": "Algeria", "AND": "Andorra",
    "ARG": "Argentina", "ARM": "Armenia", "AUS": "Australia", "AUT": "Austria",
    "AZE": "Azerbaijan", "BHS": "Bahamas", "BGD": "Bangladesh", "BRB": "Barbados",
    "BLR": "Belarus", "BEL": "Belgium", "BLZ": "Belize", "BEN": "Benin",
    "BMU": "Bermuda", "BOL": "Bolivia", "BIH": "Bosnia and Herzegovina",
    "BRA": "Brazil", "BGR": "Bulgaria", "BDI": "Burundi", "KHM": "Cambodia",
    "CMR": "Cameroon", "CAN": "Canada", "CYM": "Cayman Islands", "CHL": "Chile",
    "COL": "Colombia", "CRI": "Costa Rica", "HRV": "Croatia", "CUB": "Cuba",
    "CYP": "Cyprus", "CZE": "Czech Republic", "COD": "DR Congo", "DNK": "Denmark",
    "DJI": "Djibouti", "DMA": "Dominica", "DOM": "Dominican Republic",
    "ECU": "Ecuador", "EGY": "Egypt", "ERI": "Eritrea", "EST": "Estonia",
    "ETH": "Ethiopia", "FIN": "Finland", "FRA": "France", "GHA": "Ghana",
    "GRC": "Greece", "GTM": "Guatemala", "HKG": "Hong Kong", "HUN": "Hungary",
    "ISL": "Iceland", "IND": "India", "IDN": "Indonesia", "IRN": "Iran",
    "IRQ": "Iraq", "IRL": "Ireland", "ISR": "Israel", "ITA": "Italy",
    "JAM": "Jamaica", "JPN": "Japan", "JOR": "Jordan", "KAZ": "Kazakhstan",
    "KEN": "Kenya", "KOR": "South Korea", "KWT": "Kuwait", "LVA": "Latvia",
    "LBN": "Lebanon", "LBY": "Libya", "LTU": "Lithuania", "LUX": "Luxembourg",
    "MYS": "Malaysia", "MLT": "Malta", "MEX": "Mexico", "MDA": "Moldova",
    "MNG": "Mongolia", "MAR": "Morocco", "MMR": "Myanmar", "NPL": "Nepal",
    "NLD": "Netherlands", "NZL": "New Zealand", "NIC": "Nicaragua",
    "NGA": "Nigeria", "NOR": "Norway", "PAK": "Pakistan", "PAN": "Panama",
    "PRY": "Paraguay", "PER": "Peru", "PHL": "Philippines", "POL": "Poland",
    "PRT": "Portugal", "QAT": "Qatar", "ROU": "Romania", "RUS": "Russia",
    "SAU": "Saudi Arabia", "SRB": "Serbia", "SGP": "Singapore", "SVK": "Slovakia",
    "SVN": "Slovenia", "ZAF": "South Africa", "ESP": "Spain", "LKA": "Sri Lanka",
    "SWE": "Sweden", "CHE": "Switzerland", "TWN": "Taiwan", "THA": "Thailand",
    "TUR": "Turkey", "UKR": "Ukraine", "ARE": "UAE", "GBR": "United Kingdom",
    "USA": "United States", "URY": "Uruguay", "UZB": "Uzbekistan",
    "VEN": "Venezuela", "VNM": "Vietnam", "YEM": "Yemen", "ZMB": "Zambia",
    "ZWE": "Zimbabwe",
}


# ─────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────
@dataclass
class Article:
    title: str
    url: str
    published: Optional[str] = None
    summary: Optional[str] = None


@dataclass
class Feed:
    country_code: str
    country_name: str
    publication_name: str
    website: str
    feed_url: str
    language_code: str = "unknown"
    language_name: str = "Unknown"

    @property
    def domain(self) -> str:
        return urlparse(self.website).netloc.replace("www.", "")


@dataclass
class FeedResult:
    feed: Feed
    articles: list[Article] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_ms: int = 0

    @property
    def success(self) -> bool:
        return self.error is None and len(self.articles) > 0


# ─────────────────────────────────────────────────────────────────────
# Feed registry — cache-first, no auto-refresh
# ─────────────────────────────────────────────────────────────────────
class FeedRegistry:
    """
    Loads the country→feeds map.

    Priority (unless force_refresh=True):
      1. .cache/feeds.json   — used as-is, no TTL check
      2. feeds_compact.json  — expanded; seeds the cache
      3. FALLBACK_FEEDS      — bundled offline snapshot
      4. GitHub              — only with force_refresh=True
    """

    def __init__(self, source_url: str = GITHUB_FEED_URL):
        self.source_url = source_url
        self.raw: dict[str, list[dict]] = {}
        APP_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public API ───────────────────────────────────────────────────
    def load(self, force_refresh: bool = False) -> dict[str, list[dict]]:
        """
        Load feeds. Never contacts the network unless force_refresh=True.
        """
        if force_refresh:
            return self._refresh_from_github()

        # 1. Cache
        cached = self._read_cache()
        if cached:
            self.raw = cached
            logger.info(
                f"  ✓ Loaded {len(cached)} countries from cache "
                f"({CACHE_FILE})"
            )
            return cached

        # 2. Compact file (expanded)
        expanded = self._load_compact()
        if expanded:
            self.raw = expanded
            self._write_cache(expanded)   # seed cache so next run is instant
            logger.info(
                f"  ✓ Loaded {len(expanded)} countries from "
                f"{COMPACT_FILE.name} (cache seeded)"
            )
            return expanded

        # 3. Bundled fallback
        logger.warning("  ⚠ No cache / compact file — using bundled fallback")
        self.raw = FALLBACK_FEEDS
        self._write_cache(self.raw)       # seed cache from fallback too
        return self.raw

    def iter_feeds(self, country_filter: Optional[Iterable[str]] = None) -> Iterable[Feed]:
        wanted = {c.upper() for c in country_filter} if country_filter else None

        for code, pubs in self.raw.items():
            if wanted and code.upper() not in wanted:
                continue

            country_name = COUNTRY_NAMES.get(code.upper(), code.upper())

            for pub in pubs:
                name = pub.get("publication_name", "Unknown")
                website = pub.get("publication_website_uri", "")
                for feed in pub.get("publication_rss_feed_uris", []):
                    uri = feed.get("uri")
                    if not uri:
                        continue
                    yield Feed(
                        country_code=code.upper(),
                        country_name=country_name,
                        publication_name=name,
                        website=website,
                        feed_url=uri,
                        language_code=feed.get("language_code", "unknown"),
                        language_name=feed.get("language_name", "Unknown"),
                    )

    def countries(self) -> list[tuple[str, str, int]]:
        out = []
        for code, pubs in self.raw.items():
            n = sum(len(p.get("publication_rss_feed_uris", [])) for p in pubs)
            out.append((code, COUNTRY_NAMES.get(code, code), n))
        return sorted(out, key=lambda x: x[1])

    # ── Cache ────────────────────────────────────────────────────────
    def _read_cache(self) -> Optional[dict]:
        if not CACHE_FILE.exists():
            return None
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"  ⚠ Cache unreadable ({e}); ignoring")
            return None

    def _write_cache(self, data: dict) -> None:
        try:
            CACHE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"  ⚠ Could not write cache: {e}")

    # ── Compact file ─────────────────────────────────────────────────
    def _load_compact(self) -> Optional[dict]:
        path = COMPACT_FILE if COMPACT_FILE.exists() else (
            LEGACY_COMPACT_FILE if LEGACY_COMPACT_FILE.exists() else None
        )
        if not path:
            return None
        try:
            compact = json.loads(path.read_text(encoding="utf-8"))
            return self._expand_compact(compact)
        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"  ⚠ Compact file unreadable ({e})")
            return None

    @staticmethod
    def _expand_compact(compact: dict) -> dict[str, list[dict]]:
        """Turn {c: {n, p:{name:{w,f:[{u,l,ln}]}}}} back into the full shape."""
        expanded: dict[str, list[dict]] = {}
        for code, info in compact.items():
            pubs = []
            for name, pdata in info.get("p", {}).items():
                pubs.append({
                    "publication_name": name,
                    "publication_website_uri": pdata.get("w", ""),
                    "publication_rss_feed_uris": [
                        {
                            "uri": f["u"],
                            "language_code": f.get("l", ""),
                            "language_name": f.get("ln", ""),
                        }
                        for f in pdata.get("f", [])
                        if f.get("u")
                    ],
                })
            if pubs:
                expanded[code] = pubs
        return expanded

    # ── GitHub (only when asked) ─────────────────────────────────────
    def _refresh_from_github(self) -> dict[str, list[dict]]:
        logger.info("Fetching feed registry from GitHub (explicit refresh)...")
        try:
            resp = requests.get(self.source_url, timeout=30)
            resp.raise_for_status()
            self.raw = resp.json()
            self._write_cache(self.raw)
            logger.info(f"  ✓ Refreshed: {len(self.raw)} countries saved to cache")
        except Exception as e:
            logger.warning(f"  ✗ Refresh failed ({e}); keeping current data")
            if not self.raw:
                # Nothing loaded yet — fall back so the app still works
                self.raw = FALLBACK_FEEDS
        return self.raw


# ─────────────────────────────────────────────────────────────────────
# RSS reader
# ─────────────────────────────────────────────────────────────────────
class RSSNewsReader:
    """Parses RSS/Atom feeds and returns clean Article objects."""

    TITLE_BLOCKLIST = {
        "home", "menu", "login", "sign up", "subscribe", "newsletter",
        "contact", "about", "advertise", "privacy policy",
        "terms of service", "cookies", "more", "read more", "load more",
    }

    _TAG_RE = re.compile(r"<[^>]+>")
    _WS_RE = re.compile(r"\s+")

    def __init__(
        self,
        timeout: int = 15,
        max_articles: int = 25,
        min_title_length: int = 20,
        max_title_length: int = 300,
    ):
        self.timeout = timeout
        self.max_articles = max_articles
        self.min_title_length = min_title_length
        self.max_title_length = max_title_length

    def _clean_text(self, raw: Optional[str]) -> str:
        if not raw:
            return ""
        text = self._TAG_RE.sub(" ", raw)
        text = (
            text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&#39;", "'")
                .replace("&quot;", '"')
                .replace("&lt;", "<")
                .replace("&gt;", ">")
        )
        return self._WS_RE.sub(" ", text).strip()

    def _is_valid_title(self, title: str) -> bool:
        if not title:
            return False
        t = title.strip()
        if not (self.min_title_length <= len(t) <= self.max_title_length):
            return False
        if t.lower() in self.TITLE_BLOCKLIST:
            return False
        return True

    def parse_feed(self, feed: Feed) -> FeedResult:
        t0 = time.time()
        result = FeedResult(feed=feed)

        try:
            resp = requests.get(
                feed.feed_url,
                timeout=self.timeout,
                headers={
                    "User-Agent": "NewsAggregator/1.0 (+https://example.com/bot)",
                    "Accept": "application/rss+xml, application/atom+xml, "
                              "application/xml, text/xml, */*",
                },
                allow_redirects=True,
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except requests.exceptions.RequestException as e:
            result.error = f"Fetch error: {type(e).__name__}"
            result.elapsed_ms = int((time.time() - t0) * 1000)
            return result
        except Exception as e:
            result.error = f"Parse error: {e}"
            result.elapsed_ms = int((time.time() - t0) * 1000)
            return result

        if getattr(parsed, "bozo", 0) and not parsed.entries:
            result.error = (
                f"Malformed feed: {getattr(parsed, 'bozo_exception', 'unknown')}"
            )
            result.elapsed_ms = int((time.time() - t0) * 1000)
            return result

        seen_urls: set[str] = set()
        for entry in parsed.entries[: self.max_articles * 2]:
            title = self._clean_text(entry.get("title", ""))
            if not self._is_valid_title(title):
                continue

            link = entry.get("link") or entry.get("id") or ""
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)

            published = (
                entry.get("published")
                or entry.get("updated")
                or entry.get("pubDate")
            )
            summary = self._clean_text(
                entry.get("summary") or entry.get("description")
            )
            if summary and len(summary) > 300:
                summary = summary[:300].rsplit(" ", 1)[0] + "…"

            result.articles.append(
                Article(
                    title=title,
                    url=link,
                    published=published,
                    summary=summary or None,
                )
            )
            if len(result.articles) >= self.max_articles:
                break

        result.elapsed_ms = int((time.time() - t0) * 1000)
        if not result.articles and not result.error:
            result.error = "Feed parsed but had no usable entries"
        return result


# ─────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────
class NewsAggregator:
    def __init__(
        self,
        registry: Optional[FeedRegistry] = None,
        reader: Optional[RSSNewsReader] = None,
        max_workers: int = 12,
    ):
        self.registry = registry or FeedRegistry()
        self.reader = reader or RSSNewsReader()
        self.max_workers = max_workers

    def load_registry(self, force_refresh: bool = False) -> None:
        self.registry.load(force_refresh=force_refresh)

    def fetch_all(
        self,
        country_filter: Optional[Iterable[str]] = None,
        language_filter: Optional[str] = None,
    ) -> list[FeedResult]:
        feeds = list(self.registry.iter_feeds(country_filter))

        if language_filter:
            lang = language_filter.lower()
            feeds = [
                f for f in feeds
                if f.language_name.lower() == lang or f.language_code.lower() == lang
            ]

        if not feeds:
            logger.warning("No feeds matched the given filters.")
            return []

        logger.info(f"Fetching {len(feeds)} RSS feeds with {self.max_workers} workers...")

        results: list[FeedResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = {ex.submit(self.reader.parse_feed, f): f for f in feeds}
            for fut in as_completed(futures):
                feed = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    r = FeedResult(feed=feed, error=f"Unexpected: {e}")
                results.append(r)

                if r.success:
                    logger.info(
                        f"  ✓ [{feed.country_code}] {feed.publication_name} — "
                        f"{len(r.articles)} items"
                    )
                else:
                    logger.warning(
                        f"  ✗ [{feed.country_code}] {feed.publication_name}: {r.error}"
                    )

        return results

    # ── Output ───────────────────────────────────────────────────────
    @staticmethod
    def print_results(
        results: list[FeedResult],
        show_errors: bool = False,
        max_per_feed: int = 5,
        group_by_country: bool = True,
    ) -> None:
        ok = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        total_articles = sum(len(r.articles) for r in ok)

        if group_by_country:
            by_country: dict[str, list[FeedResult]] = {}
            for r in ok:
                by_country.setdefault(r.feed.country_name, []).append(r)

            for country in sorted(by_country):
                print(f"\n{'═' * 82}")
                print(f"  🌍  {country}")
                print(f"{'═' * 82}")
                for r in by_country[country]:
                    print(f"\n  📰 {r.feed.publication_name} "
                          f"({r.feed.language_name}) — {len(r.articles)} items")
                    for i, art in enumerate(r.articles[:max_per_feed], 1):
                        print(f"     {i:>2}. {art.title}")
                        if art.published:
                            print(f"         🕒 {art.published}")
                        print(f"         {art.url}")
        else:
            for r in ok:
                print(f"\n{'═' * 82}")
                print(f"  [{r.feed.country_code}] {r.feed.publication_name} "
                      f"({r.feed.language_name})")
                print(f"{'═' * 82}")
                for i, art in enumerate(r.articles[:max_per_feed], 1):
                    print(f"  {i:>2}. {art.title}")
                    print(f"      {art.url}")

        if show_errors and failed:
            print(f"\n{'─' * 82}")
            print("  Skipped feeds:")
            for r in failed:
                print(f"   • [{r.feed.country_code}] {r.feed.publication_name}: {r.error}")

        print(f"\n{'═' * 82}")
        print(
            f"📊  {len(ok)}/{len(results)} feeds OK  ·  "
            f"{total_articles} headlines  ·  {len(failed)} failed"
        )
        print(f"{'═' * 82}")

    @staticmethod
    def save_json(results: list[FeedResult], path: str = "news.json") -> None:
        payload = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_articles": sum(len(r.articles) for r in results if r.success),
            "feeds": [
                {
                    "country_code": r.feed.country_code,
                    "country_name": r.feed.country_name,
                    "publication": r.feed.publication_name,
                    "website": r.feed.website,
                    "feed_url": r.feed.feed_url,
                    "language": r.feed.language_name,
                    "elapsed_ms": r.elapsed_ms,
                    "error": r.error,
                    "articles": [asdict(a) for a in r.articles],
                }
                for r in results
            ],
        }
        Path(path).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(f"Saved → {path}")


# ─────────────────────────────────────────────────────────────────────
# Compact exporter
# ─────────────────────────────────────────────────────────────────────
def export_compact_feeds(
    registry: FeedRegistry,
    out_path: Optional[Path] = None,
) -> None:
    """Write a compact, deduped feeds file with short keys."""
    out_path = out_path or COMPACT_FILE
    raw = registry.raw or registry.load()

    compact: dict[str, dict] = {}
    for code, pubs in raw.items():
        pub_map: dict[str, dict] = {}
        for pub in pubs:
            name = pub.get("publication_name", "").strip()
            if not name:
                continue
            entry = pub_map.setdefault(name, {
                "w": pub.get("publication_website_uri", ""),
                "f": [],
            })
            seen_uris = {f["u"] for f in entry["f"]}
            for feed in pub.get("publication_rss_feed_uris", []):
                uri = feed.get("uri")
                if not uri or uri in seen_uris:
                    continue
                seen_uris.add(uri)
                entry["f"].append({
                    "u": uri,
                    "l": feed.get("language_code", ""),
                    "ln": feed.get("language_name", ""),
                })
        if pub_map:
            compact[code] = {
                "n": COUNTRY_NAMES.get(code, code),
                "p": pub_map,
            }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    src_size = len(json.dumps(raw))
    dst_size = len(json.dumps(compact, separators=(",", ":")))
    logger.info(
        f"Exported compact feed file → {out_path}\n"
        f"  original: {src_size/1024:.1f} KB  →  compact: {dst_size/1024:.1f} KB "
        f"({100 * (1 - dst_size/src_size):.0f}% smaller)"
    )


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────
def _print_menu(agg: NewsAggregator) -> None:
    countries = agg.registry.countries()
    total_feeds = sum(n for _, _, n in countries)
    cache_state = "present" if CACHE_FILE.exists() else "missing"

    print("\n" + "═" * 82)
    print("🌍  GLOBAL RSS NEWS AGGREGATOR")
    print("═" * 82)
    print(f"  Countries: {len(countries)}  ·  Total feeds: {total_feeds}")
    print(f"  Cache: {cache_state}  ({CACHE_FILE})")
    print("─" * 82)
    print("  1. Fetch all feeds")
    print("  2. Fetch a specific country (by code, e.g. IND)")
    print("  3. Fetch a specific language (e.g. English)")
    print("  4. Demo (5 reliable feeds)")
    print("  5. List countries with feed counts")
    print("  6. Export compact feeds.json")
    print("  7. Refresh feed registry from GitHub  ← only here")
    print("  0. Exit")
    print("═" * 82)


def main() -> None:
    agg = NewsAggregator()
    agg.load_registry()      # cache-first, no network

    while True:
        _print_menu(agg)
        try:
            choice = input("Enter choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "1":
            results = agg.fetch_all()
            agg.print_results(results, show_errors=False)
            agg.save_json(results, "news_all.json")

        elif choice == "2":
            code = input("Country code (e.g. IND, USA): ").strip().upper()
            results = agg.fetch_all(country_filter=[code])
            if results:
                agg.print_results(results, show_errors=True)
                agg.save_json(results, f"news_{code.lower()}.json")

        elif choice == "3":
            lang = input("Language (e.g. English): ").strip()
            results = agg.fetch_all(language_filter=lang)
            if results:
                agg.print_results(results, show_errors=True)

        elif choice == "4":
            demo_codes = ["GBR", "USA", "IND", "DEU", "JPN"]
            results = agg.fetch_all(country_filter=demo_codes)[:10]
            agg.print_results(results, show_errors=True)

        elif choice == "5":
            for code, name, n in agg.registry.countries():
                print(f"  {code}  {name:<30} {n:>4} feeds")

        elif choice == "6":
            export_compact_feeds(agg.registry)

        elif choice == "7":
            confirm = input(
                "This will fetch from GitHub and overwrite the cache. Continue? [y/N]: "
            ).strip().lower()
            if confirm == "y":
                agg.load_registry(force_refresh=True)
                print("  ✓ Registry refreshed from GitHub.")
            else:
                print("  Cancelled.")

        elif choice == "0":
            print("Bye 👋")
            return
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()