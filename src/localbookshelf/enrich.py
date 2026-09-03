"""Open Library metadata enrichment.

Rules of engagement, all of them load-bearing:

* Only fills fields that are still empty after embedded extraction.
* Every response is cached in SQLite, so a rescan of an unchanged library makes
  zero network requests.
* Search results must clear a confidence bar before they are accepted; below it
  the candidates are stored and the book is flagged for review rather than
  silently mismatched. Wrong metadata is worse than none.
* Rate limited and identified with a real User-Agent, because Open Library is a
  free service run by a nonprofit.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .db import Database
from .formats.base import clean_description, clean_text, parse_year

USER_AGENT = "LocalBookshelf/0.1 (personal library indexer; +https://github.com/localbookshelf)"
BASE_URL = "https://openlibrary.org"
COVERS_URL = "https://covers.openlibrary.org"
REQUEST_INTERVAL = 1.0  # seconds between requests
TIMEOUT = 15.0

ACCEPT_CONFIDENCE = 0.85
REVIEW_FLOOR = 0.45  # below this we don't even offer it as a candidate


@dataclass
class Candidate:
    openlibrary_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    cover_id: int | None = None
    publisher: str | None = None
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "openlibrary_id": self.openlibrary_id,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "cover_id": self.cover_id,
            "publisher": self.publisher,
            "confidence": round(self.confidence, 3),
        }


@dataclass
class EnrichmentResult:
    matched: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    description: str | None = None
    cover_bytes: bytes | None = None
    needs_review: bool = False


class OpenLibraryClient:
    """Cached, rate-limited, failure-tolerant Open Library access."""

    def __init__(self, db: Database, *, offline: bool = False) -> None:
        self.db = db
        self.offline = offline
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._client = None
        self._network_failures = 0

    # -- transport ---------------------------------------------------------

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    def _cached(self, url: str) -> str | None:
        row = self.db.query_one("SELECT body FROM http_cache WHERE url = ?", (url,))
        return row["body"] if row else None

    def _store(self, url: str, body: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO http_cache (url, body, fetched_at) VALUES (?, ?, ?)",
                (url, body, datetime.now(timezone.utc).isoformat()),
            )

    def get_json(self, url: str) -> dict | None:
        cached = self._cached(url)
        if cached is not None:
            return json.loads(cached) if cached != "null" else None
        if self.offline or self._network_failures >= 5:
            return None

        import httpx

        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < REQUEST_INTERVAL:
                time.sleep(REQUEST_INTERVAL - elapsed)
            self._last_request = time.monotonic()

        try:
            response = self._http().get(url)
        except httpx.HTTPError:
            self._network_failures += 1
            return None

        if response.status_code == 404:
            self._store(url, "null")
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        self._store(url, json.dumps(payload))
        self._network_failures = 0
        return payload

    def get_cover(self, cover_id: int) -> bytes | None:
        if self.offline or not cover_id or self._network_failures >= 5:
            return None
        import httpx

        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < REQUEST_INTERVAL:
                time.sleep(REQUEST_INTERVAL - elapsed)
            self._last_request = time.monotonic()
        try:
            response = self._http().get(f"{COVERS_URL}/b/id/{cover_id}-L.jpg")
        except httpx.HTTPError:
            self._network_failures += 1
            return None
        if response.status_code != 200 or len(response.content) < 1000:
            return None
        return response.content

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- lookups -----------------------------------------------------------

    def by_isbn(self, isbn: str) -> Candidate | None:
        edition = self.get_json(f"{BASE_URL}/isbn/{isbn}.json")
        if not edition:
            return None
        works = edition.get("works") or []
        work_key = works[0].get("key") if works else None
        candidate = Candidate(
            openlibrary_id=work_key or edition.get("key", f"/isbn/{isbn}"),
            title=clean_text(edition.get("title")) or "",
            year=parse_year(edition.get("publish_date")),
            publisher=clean_text((edition.get("publishers") or [None])[0]),
            confidence=1.0,  # an ISBN match is definitional, not a guess
        )
        covers = [c for c in (edition.get("covers") or []) if isinstance(c, int) and c > 0]
        if covers:
            candidate.cover_id = covers[0]
        candidate.authors = self._author_names(edition.get("authors") or [])
        return candidate

    def search(self, title: str, author: str | None) -> list[Candidate]:
        params = [f"title={_quote(title)}"]
        if author:
            params.append(f"author={_quote(author)}")
        params.append("limit=5")
        params.append(
            "fields=key,title,author_name,first_publish_year,cover_i,publisher"
        )
        payload = self.get_json(f"{BASE_URL}/search.json?" + "&".join(params))
        if not payload:
            return []
        out: list[Candidate] = []
        for doc in payload.get("docs", [])[:5]:
            doc_title = clean_text(doc.get("title"))
            if not doc_title:
                continue
            publishers = doc.get("publisher") or []
            out.append(
                Candidate(
                    openlibrary_id=doc.get("key", ""),
                    title=doc_title,
                    authors=[clean_text(a) or "" for a in (doc.get("author_name") or [])][:4],
                    year=doc.get("first_publish_year"),
                    cover_id=doc.get("cover_i"),
                    publisher=clean_text(publishers[0]) if publishers else None,
                )
            )
        return out

    def description_for(self, openlibrary_id: str) -> str | None:
        if not openlibrary_id.startswith("/works/"):
            return None
        work = self.get_json(f"{BASE_URL}{openlibrary_id}.json")
        if not work:
            return None
        description = work.get("description")
        if isinstance(description, dict):
            description = description.get("value")
        return clean_description(description)

    def _author_names(self, refs: list) -> list[str]:
        names: list[str] = []
        for ref in refs[:4]:
            key = ref.get("key") if isinstance(ref, dict) else None
            if not key:
                continue
            record = self.get_json(f"{BASE_URL}{key}.json")
            if record:
                name = clean_text(record.get("name"))
                if name:
                    names.append(name)
        return names


# -- matching --------------------------------------------------------------


def normalize_for_match(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"^(the|a|an)\s+", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_similarity(left: str, right: str) -> float:
    a, b = normalize_for_match(left), normalize_for_match(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # A subtitle on one side shouldn't tank an otherwise exact match.
    if a.startswith(b) or b.startswith(a):
        return 0.93
    return SequenceMatcher(None, a, b).ratio()


def author_matches(ours: list[str], theirs: list[str]) -> bool:
    """Compare on surnames: 'Dan Simmons' and 'Simmons, Dan' are one person."""
    if not ours or not theirs:
        return False
    def surnames(names: list[str]) -> set[str]:
        out = set()
        for name in names:
            normalized = normalize_for_match(name)
            parts = [p for p in normalized.split() if len(p) > 1]
            if parts:
                out.add(parts[-1])
                out.add(parts[0])
        return out
    return bool(surnames(ours) & surnames(theirs))


def score(candidate: Candidate, title: str, authors: list[str]) -> float:
    similarity = title_similarity(title, candidate.title)
    if authors and candidate.authors:
        similarity += 0.12 if author_matches(authors, candidate.authors) else -0.25
    return max(0.0, min(1.0, similarity))


def enrich(
    client: OpenLibraryClient,
    *,
    title: str | None,
    authors: list[str],
    isbn: str | None,
    want_cover: bool,
) -> EnrichmentResult:
    """Look a book up, refusing to guess when the evidence is thin."""
    result = EnrichmentResult()

    if isbn:
        matched = client.by_isbn(isbn)
        if matched and matched.title:
            result.matched = matched
            result.description = client.description_for(matched.openlibrary_id)
            if want_cover and matched.cover_id:
                result.cover_bytes = client.get_cover(matched.cover_id)
            return result

    # A one-word or two-character title carries no signal; searching it returns
    # a confident match to whichever unrelated book happens to rank first.
    if not title or len(title.strip()) < 4:
        return result

    candidates = client.search(title, authors[0] if authors else None)
    if not candidates and authors:
        candidates = client.search(title, None)
    for candidate in candidates:
        candidate.confidence = score(candidate, title, authors)
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    if not candidates:
        return result

    best = candidates[0]
    if best.confidence >= ACCEPT_CONFIDENCE:
        result.matched = best
        result.description = client.description_for(best.openlibrary_id)
        if want_cover and best.cover_id:
            result.cover_bytes = client.get_cover(best.cover_id)
    else:
        result.candidates = [c for c in candidates if c.confidence >= REVIEW_FLOOR]
        result.needs_review = bool(result.candidates)
    return result


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value[:200])
