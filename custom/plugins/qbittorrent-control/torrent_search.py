"""Deterministic, fail-closed torrent search and single-file preflight.

The module deliberately keeps policy and validation independent from the
qBittorrent transport.  Search metadata is untrusted input: it is never
passed to a shell, used as a path, or accepted as proof of payload size.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable
from contextvars import ContextVar
from enum import Enum

LOGGER = logging.getLogger("hermes.qbittorrent.torrent_search")
GIB = 1024 ** 3
MAX_PAYLOAD_FILES = 1
MAX_TORRENTS_STARTED_PER_REQUEST = 1
DEFAULT_MIN_SIZE = 1 * GIB
DEFAULT_MAX_SIZE = 5 * GIB
DEFAULT_MAX_RESULTS = 100
DEFAULT_MAX_PREFLIGHT = 3
MAX_CONCURRENT_PREFLIGHTS_PER_REQUEST = 1
SEARCH_TIMEOUT_SECONDS = 45
METADATA_TIMEOUT_SECONDS = 60
POLL_SECONDS = 1.0
VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".mpg"})
LOW_QUALITY = frozenset({"cam", "camrip", "hdcam", "ts", "telesync", "hdts", "tc", "telecine"})
NORMAL_QUALITY = frozenset({"web", "webdl", "webrip", "bluray", "bdrip", "brrip", "hdtv", "dvdrip"})
LANGUAGE_TOKENS = frozenset({"lat", "latino", "latina", "latam", "latinoamerica", "en", "eng", "english"})
FORMAT_TOKENS = frozenset({
    "mkv", "mp4", "avi", "mpg", "480p", "576p", "720p", "1080p", "1440p",
    "2160p", "4k", "8k", "uhd", "fhd", "hd", "sd",
})
RESOLUTION_RE = re.compile(r"^(?:\d{3,4}p|[1-8]k)$", re.I)
SAMPLE_TOKENS = frozenset({
    "sample", "trailer", "preview", "proof", "extras", "extra", "featurette",
    "behind", "scenes", "bonus", "deleted", "teaser",
})
BTIH_RE = re.compile(r"^(?:[0-9a-f]{40}|[a-z2-7]{32})$", re.I)


class SearchFailure(RuntimeError):
    """A structured failure that must not be hidden from Hermes."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class RateLimited(SearchFailure):
    def __init__(self, message: str = "BTDig/qBittorrent rate limit") -> None:
        super().__init__("BTDIG_RATE_LIMIT", message)


class MetadataTimeout(SearchFailure):
    def __init__(self, message: str = "metadata did not arrive before timeout") -> None:
        super().__init__("METADATA_TIMEOUT", message)


class OperationState(str, Enum):
    SEARCHING = "SEARCHING"
    PREFLIGHT = "PREFLIGHT"
    VALIDATING = "VALIDATING"
    READY_TO_START = "READY_TO_START"
    STARTING = "STARTING"
    STARTED = "STARTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


FALLBACK_REJECTION_CODES = frozenset({
    "METADATA_TIMEOUT",
    "NO_VALID_VIDEO_FILE",
    "AMBIGUOUS_CONTENT",
    "MULTIPLE_FILES_SELECTED",
})
STARTED_QBIT_STATES = frozenset({
    "downloading", "stalleddl", "queuedl", "pausedl", "checkingdl",
    "stoppedl", "uploading", "stalledup", "queuedup", "pausedup",
    "checkingup", "seeding", "stoppedup",
})


def _diagnostic_bucket(diagnostics: dict[str, Any] | None, key: str) -> dict[str, int]:
    if diagnostics is None:
        return {}
    bucket = diagnostics.setdefault(key, {})
    return bucket


def _count_diagnostic(diagnostics: dict[str, Any] | None, key: str, reason: str, amount: int = 1) -> None:
    bucket = _diagnostic_bucket(diagnostics, key)
    bucket[reason] = bucket.get(reason, 0) + amount


@dataclass(frozen=True)
class TorrentCandidate:
    query: str
    name: str
    magnet_uri: str
    infohash: str
    search_size: int | None
    seeders: int
    leechers: int
    description_url: str
    engine: str
    source_rank: int
    language: str
    quality: str
    title_similarity: float


@dataclass(frozen=True)
class TorrentFile:
    index: int
    name: str
    size: int
    priority: int = 0
    progress: float = 0.0
    availability: float = -1.0


@dataclass(frozen=True)
class ValidatedFile:
    file: TorrentFile
    similarity: float


@dataclass
class SelectionPolicy:
    min_size_bytes: int = DEFAULT_MIN_SIZE
    max_size_bytes: int = DEFAULT_MAX_SIZE
    language_preference: tuple[str, ...] = ("lat", "en", "unknown")
    prefer_recent: bool = True
    allow_cam: bool = True
    max_results: int = DEFAULT_MAX_RESULTS
    max_preflight_candidates: int = DEFAULT_MAX_PREFLIGHT
    media_type: str = "video"
    allowed_extensions: frozenset[str] = field(default_factory=lambda: VIDEO_EXTENSIONS)
    preferred_resolution: str | None = None
    preferred_extension: str | None = None


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def tokens(value: Any) -> tuple[str, ...]:
    return tuple(normalize_text(value).split())


def parse_magnet(raw: Any) -> tuple[str, str]:
    magnet = str(raw or "").strip()
    if len(magnet) > 4096 or not magnet.lower().startswith("magnet:?"):
        raise SearchFailure("NO_VALID_MAGNET", "magnet URI malformed")
    if any(ord(char) < 32 or char.isspace() for char in magnet):
        raise SearchFailure("NO_VALID_MAGNET", "magnet contains whitespace/control characters")
    parsed = urllib.parse.urlsplit(magnet)
    if parsed.scheme.casefold() != "magnet" or parsed.netloc:
        raise SearchFailure("NO_VALID_MAGNET", "magnet scheme malformed")
    values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("xt", [])
    hashes = [value[9:] for value in values if value.casefold().startswith("urn:btih:")]
    if not hashes or not BTIH_RE.fullmatch(hashes[0]):
        raise SearchFailure("NO_VALID_MAGNET", "magnet lacks a valid BTIH infohash")
    return magnet, hashes[0].casefold()


def _title_terms(query: str) -> tuple[str, ...]:
    ignored = LANGUAGE_TOKENS | LOW_QUALITY | NORMAL_QUALITY | FORMAT_TOKENS | {
        "the", "a", "an", "of", "and", "or", "with", "in", "on", "to",
    }
    return tuple(term for term in tokens(query) if term not in ignored)


def extract_media_preferences(query: str) -> tuple[str | None, str | None]:
    """Extract requested resolution/extension without treating them as title."""
    found = tokens(query)
    resolution = next((term for term in found if RESOLUTION_RE.fullmatch(term)), None)
    extension = next((f".{term}" for term in found if f".{term}" in VIDEO_EXTENSIONS), None)
    return resolution, extension


def title_similarity(query: str, name: str) -> float:
    required = _title_terms(query)
    candidate = set(tokens(name))
    if not required:
        return 0.0
    matched = sum(term in candidate for term in required)
    token_score = matched / len(required)
    sequence_score = difflib.SequenceMatcher(None, normalize_text(query), normalize_text(name)).ratio()
    return round((token_score * 0.75) + (sequence_score * 0.25), 6)


def title_matches(query: str, name: str, threshold: float = 0.72) -> bool:
    required = _title_terms(query)
    candidate = set(tokens(name))
    if not required or not all(term in candidate for term in required):
        return False
    return title_similarity(query, name) >= threshold


def classify_language(name: str) -> str:
    found = set(tokens(name))
    if found & {"lat", "latino", "latina", "latam", "latinoamerica"}:
        return "lat"
    if found & {"eng", "english", "en"}:
        return "en"
    return "unknown"


def classify_quality(name: str) -> str:
    found = set(tokens(name))
    return "LOW_QUALITY_FALLBACK" if found & LOW_QUALITY else "NORMAL_QUALITY"


def quality_label(name: str) -> str:
    found = set(tokens(name))
    for label in ("WEB-DL", "WEBRIP", "BLURAY", "BDRIP", "BRRIP", "HDTV", "DVDRIP", "CAMRIP", "HDCAM", "CAM", "TS", "TELESYNC", "HDTS", "TC", "TELECINE"):
        if normalize_text(label) in found:
            return label
    return "NORMAL"


def release_source_label(name: str) -> str:
    """Return a short distributor/source label when the release name provides one."""
    found = set(tokens(name))
    for aliases, label in (
        (("amazon", "amzn"), "Amazon"),
        (("netflix", "nf"), "Netflix"),
        (("hulu",), "Hulu"),
        (("disney",), "Disney+"),
        (("hbo", "max"), "Max/HBO"),
        (("apple", "atvp"), "Apple TV+"),
        (("paramount",), "Paramount+"),
        (("peacock",), "Peacock"),
        (("crunchyroll",), "Crunchyroll"),
    ):
        if found.intersection(aliases):
            return label
    return ""


def _to_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_search_candidate_with_reason(raw: dict[str, Any], query: str, source_rank: int) -> tuple[TorrentCandidate | None, str]:
    name = str(raw.get("fileName") or raw.get("name") or "").strip()
    magnet = raw.get("fileUrl") or raw.get("link") or ""
    if not name or not magnet:
        return None, "missing_name_or_magnet"
    if not title_matches(query, name):
        return None, "title_mismatch"
    try:
        magnet_uri, infohash = parse_magnet(magnet)
    except SearchFailure:
        return None, "invalid_magnet"
    return TorrentCandidate(
        query=query,
        name=name,
        magnet_uri=magnet_uri,
        infohash=infohash,
        search_size=_to_int(raw.get("fileSize"), -1),
        seeders=_to_int(raw.get("nbSeeders", raw.get("seeds")), -1),
        leechers=_to_int(raw.get("nbLeechers", raw.get("leech")), -1),
        description_url=str(raw.get("descrLink") or raw.get("description_url") or ""),
        engine=str(raw.get("engineName") or raw.get("engine_url") or "btdig"),
        source_rank=source_rank,
        language=classify_language(name),
        quality=classify_quality(name),
        title_similarity=title_similarity(query, name),
    ), "accepted"


def parse_search_candidate(raw: dict[str, Any], query: str, source_rank: int) -> TorrentCandidate | None:
    candidate, _reason = parse_search_candidate_with_reason(raw, query, source_rank)
    return candidate


def _language_rank(language: str, preference: Iterable[str]) -> int:
    clean = [str(item).casefold() for item in preference]
    try:
        return len(clean) - clean.index(language)
    except ValueError:
        return 0


def ranking_key(candidate: TorrentCandidate, policy: SelectionPolicy) -> tuple[Any, ...]:
    quality_score = 1 if candidate.quality == "NORMAL_QUALITY" else 0
    format_score = 0
    candidate_tokens = set(tokens(candidate.name))
    if policy.preferred_resolution and policy.preferred_resolution in candidate_tokens:
        format_score += 1
    if policy.preferred_extension and policy.preferred_extension[1:] in candidate_tokens:
        format_score += 1
    freshness = -candidate.source_rank if policy.prefer_recent else 0
    availability = max(candidate.seeders, 0)
    return (
        -quality_score,
        -_language_rank(candidate.language, policy.language_preference),
        -format_score,
        -freshness,
        -candidate.title_similarity,
        -availability,
        normalize_text(candidate.name),
        candidate.infohash,
    )


def rank_candidates(candidates: Iterable[TorrentCandidate], policy: SelectionPolicy) -> list[TorrentCandidate]:
    unique: dict[str, TorrentCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.infohash, candidate)
    return sorted(unique.values(), key=lambda candidate: ranking_key(candidate, policy))


def _btdig_query(query: str) -> str:
    """Search BTDig with content identity, not delivery-format preferences."""
    content_terms = _title_terms(query)
    return " ".join(content_terms) or " ".join(tokens(query))


def _unsafe_internal_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return True
    path = PurePosixPath(name)
    return path.is_absolute() or ".." in path.parts or name.startswith("/")


def _is_sample(name: str) -> bool:
    return bool(set(tokens(name)) & SAMPLE_TOKENS)


def _file_matches_requested_format(file_name: str, policy: SelectionPolicy, context_name: str = "") -> bool:
    suffix = PurePosixPath(file_name.replace("\\", "/")).suffix.casefold()
    if policy.preferred_extension and suffix != policy.preferred_extension:
        return False
    if policy.preferred_resolution:
        file_resolutions = {term for term in tokens(file_name) if RESOLUTION_RE.fullmatch(term)}
        context_resolutions = {term for term in tokens(context_name) if RESOLUTION_RE.fullmatch(term)}
        if file_resolutions:
            return policy.preferred_resolution in file_resolutions
        if policy.preferred_resolution not in context_resolutions:
            return False
    return True


def select_single_file(
    files: Iterable[dict[str, Any] | TorrentFile],
    query: str,
    policy: SelectionPolicy,
    context_name: str = "",
) -> ValidatedFile:
    valid: list[ValidatedFile] = []
    for raw in files:
        file = raw if isinstance(raw, TorrentFile) else TorrentFile(
            index=_to_int(raw.get("index"), -1),
            name=str(raw.get("name") or ""),
            size=_to_int(raw.get("size"), 0),
            priority=_to_int(raw.get("priority"), 0),
            progress=float(raw.get("progress") or 0),
            availability=float(raw.get("availability") or -1),
        )
        suffix = PurePosixPath(file.name.replace("\\", "/")).suffix.casefold()
        if file.index < 0 or _unsafe_internal_name(file.name) or suffix not in policy.allowed_extensions:
            continue
        if file.size <= 0 or not (policy.min_size_bytes <= file.size <= policy.max_size_bytes):
            continue
        if not _file_matches_requested_format(file.name, policy, context_name):
            continue
        if _is_sample(file.name) or not title_matches(query, file.name):
            continue
        valid.append(ValidatedFile(file=file, similarity=title_similarity(query, file.name)))
    if not valid:
        raise SearchFailure("NO_VALID_VIDEO_FILE", "no single internal video file matched policy")
    valid.sort(key=lambda item: (-item.similarity, -item.file.size, normalize_text(item.file.name), item.file.index))
    if len(valid) > 1 and valid[0].similarity == valid[1].similarity and valid[0].file.size == valid[1].file.size:
        raise SearchFailure("AMBIGUOUS_CONTENT", "multiple equivalent payload files matched")
    return valid[0]


def request_fingerprint(args: dict[str, Any], policy: SelectionPolicy) -> str:
    payload = {
        "query": normalize_text(args.get("query")),
        "media_type": policy.media_type,
        "min": policy.min_size_bytes,
        "max": policy.max_size_bytes,
        "language": policy.language_preference,
        "recent": policy.prefer_recent,
        "cam": policy.allow_cam,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def _decode(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError):
        return default


def _validate_search_payload(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """Validate the qBittorrent/BTDig adapter contract before parsing rows."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchFailure("BTDIG_PARSER_ERROR", "search results payload is not a result object")
    rows = [item for item in payload["results"] if isinstance(item, dict)]
    reported_total = _to_int(payload.get("total"), len(rows))
    if reported_total > 0 and not rows:
        raise SearchFailure("BTDIG_PARSER_ERROR", "BTDig reported results but returned no result rows")
    malformed = [item for item in rows if not (item.get("fileName") or item.get("name")) or not (item.get("fileUrl") or item.get("link"))]
    if rows and len(malformed) == len(rows):
        raise SearchFailure("BTDIG_PARSER_ERROR", "BTDig result rows have an unexpected shape")
    return rows, max(reported_total, len(rows))


def validate_completed_video_file(path: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    """Return true only when ffprobe sees a video stream in the finished file.

    The path is passed as one argument without a shell.  This helper is kept
    separate from metadata preflight because invoking it before completion
    would turn an in-progress torrent into a false negative.
    """
    clean_path = os.fspath(path)
    if not clean_path or "\x00" in clean_path:
        return False
    try:
        result = runner(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1",
                clean_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if getattr(result, "returncode", 1) != 0:
        return False
    return any(line.strip().casefold() == "video" for line in str(getattr(result, "stdout", "")).splitlines())


def _request(request_fn: Callable[[str, str, dict[str, Any] | None], Any], method: str, path: str, fields: dict[str, Any] | None = None) -> Any:
    try:
        return request_fn(method, path, fields)
    except Exception as exc:
        status = getattr(exc, "code", None)
        if path.startswith("/api/v2/search/"):
            if status == 429:
                raise RateLimited() from exc
            if isinstance(status, int) and status >= 400:
                raise SearchFailure("BTDIG_HTTP_ERROR", f"BTDig/qBittorrent search HTTP {status}") from exc
            if isinstance(exc, (TimeoutError, socket.timeout)):
                raise SearchFailure("SEARCH_TIMEOUT", "search request timed out") from exc
        raise


def search_btdig(
    request_fn: Callable[[str, str, dict[str, Any] | None], Any],
    query: str,
    policy: SelectionPolicy,
    sleep_fn: Callable[[float], None] = time.sleep,
    diagnostics: dict[str, Any] | None = None,
) -> list[TorrentCandidate]:
    # Each query has its own source ordering.  Positions from different
    # searches are intentionally never treated as comparable dates.
    search_query = _btdig_query(query)
    search_queries = [
        f"{search_query} LAT",
        f"{search_query} English",
        search_query,
        f"{search_query} CAM LAT",
        f"{search_query} CAM English",
        f"{search_query} CAM",
    ]
    all_candidates: list[TorrentCandidate] = []
    if diagnostics is not None:
        diagnostics.setdefault("results_returned", 0)
        diagnostics.setdefault("parser_accepted", 0)
        diagnostics.setdefault("parser_rejected", {})
        diagnostics.setdefault("statuses", {})
    for search_query in search_queries:
        _check_operation_cancelled()
        source_rank = 0
        if diagnostics is not None:
            diagnostics.setdefault("queries", []).append(search_query)
            diagnostics["jobs"] = diagnostics.get("jobs", 0) + 1
        start = _decode(_request(request_fn, "POST", "/api/v2/search/start", {
            "pattern": search_query, "plugins": "btdig", "category": "all",
        }), {})
        search_id = _to_int(start.get("id"), -1) if isinstance(start, dict) else -1
        if search_id < 0:
            raise SearchFailure("BTDIG_PARSER_ERROR", "qBittorrent did not return search id")
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        try:
            while True:
                _check_operation_cancelled()
                status_rows = _decode(_request(request_fn, "GET", "/api/v2/search/status", {"search_id": search_id}), [])
                row = next((item for item in status_rows if _to_int(item.get("id"), -1) == search_id), None)
                status = str(row.get("status") if row else "Missing")
                _count_diagnostic(diagnostics, "statuses", status)
                if status == "Stopped":
                    break
                if status == "Failed":
                    raise SearchFailure("BTDIG_HTTP_ERROR", "BTDig search job failed")
                if time.monotonic() >= deadline:
                    raise SearchFailure("SEARCH_TIMEOUT", "search job remained Running until timeout")
                sleep_fn(POLL_SECONDS)
            result_payload = _decode(_request(request_fn, "GET", "/api/v2/search/results", {
                "id": search_id, "limit": policy.max_results, "offset": 0,
            }), {})
            _check_operation_cancelled()
            results, reported_total = _validate_search_payload(result_payload)
            if diagnostics is not None:
                diagnostics["results_returned"] = diagnostics.get("results_returned", 0) + reported_total
            for result in results:
                candidate, reason = parse_search_candidate_with_reason(result, search_query, source_rank)
                source_rank += 1
                if candidate:
                    all_candidates.append(candidate)
                    if diagnostics is not None:
                        diagnostics["parser_accepted"] = diagnostics.get("parser_accepted", 0) + 1
                else:
                    _count_diagnostic(diagnostics, "parser_rejected", reason)
        finally:
            # qBittorrent 2.15.1 uses search_id for status but id for
            # the mutating lifecycle endpoints. Cleanup must not mask the
            # original BTDig/timeout/parser error.
            for cleanup_path in ("/api/v2/search/stop", "/api/v2/search/delete"):
                try:
                    _request(request_fn, "POST", cleanup_path, {"id": search_id})
                except Exception as cleanup_exc:
                    LOGGER.warning("search cleanup failed at %s: %s", cleanup_path, type(cleanup_exc).__name__)
    if not all_candidates:
        if diagnostics and diagnostics.get("results_returned", 0):
            raise SearchFailure("NO_VALID_CANDIDATES", "BTDig returned rows but all were rejected by the parser/policy")
        raise SearchFailure("BTDIG_NO_MATCHES", "BTDig returned no rows")
    return rank_candidates(all_candidates, policy)


_OPERATION_LOCK = threading.Lock()
_STARTED_FINGERPRINTS: set[str] = set()
_OPERATION_CANCEL_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "hermes_qbittorrent_operation_cancel_event", default=None
)


def set_operation_cancellation_event(event: threading.Event) -> object:
    """Install a cancellation signal for the synchronous worker thread."""
    return _OPERATION_CANCEL_EVENT.set(event)


def reset_operation_cancellation_event(token: object) -> None:
    _OPERATION_CANCEL_EVENT.reset(token)


def _check_operation_cancelled() -> None:
    event = _OPERATION_CANCEL_EVENT.get()
    if event is not None and event.is_set():
        raise SearchFailure("REQUEST_CANCELLED", "torrent operation cancelled")


def _info(request_fn: Callable[[str, str, dict[str, Any] | None], Any], infohash: str) -> list[dict[str, Any]]:
    payload = _decode(_request(request_fn, "GET", "/api/v2/torrents/info", {"hashes": infohash}), [])
    return payload if isinstance(payload, list) else []


def _files(request_fn: Callable[[str, str, dict[str, Any] | None], Any], infohash: str) -> list[dict[str, Any]]:
    payload = _decode(_request(request_fn, "GET", "/api/v2/torrents/files", {"hash": infohash}), [])
    return payload if isinstance(payload, list) else []


def _owned(info: dict[str, Any], tag: str) -> bool:
    return tag in {part.strip() for part in str(info.get("tags") or "").split(",")}


def _request_tag(fingerprint: str) -> str:
    return f"hermes-request-{fingerprint}"


def _all_torrents(request_fn: Callable[[str, str, dict[str, Any] | None], Any]) -> list[dict[str, Any]]:
    payload = _decode(_request(request_fn, "GET", "/api/v2/torrents/info", {"filter": "all"}), [])
    return payload if isinstance(payload, list) else []


def _is_started_torrent(info: dict[str, Any], files: list[dict[str, Any]]) -> bool:
    state = str(info.get("state") or "").casefold()
    progress = float(info.get("progress") or 0)
    downloaded = _to_int(info.get("amount_downloaded"), 0)
    wanted = any(_to_int(item.get("priority"), 0) > 0 for item in files)
    return progress > 0 or downloaded > 0 or (wanted and state in STARTED_QBIT_STATES)


def _cleanup(request_fn: Callable[[str, str, dict[str, Any] | None], Any], infohash: str, tag: str) -> bool:
    """Delete only an owned temporary torrent and verify that it disappeared."""
    try:
        info = _info(request_fn, infohash)
        if not info:
            return True
        if not _owned(info[0], tag):
            return False
        _request(request_fn, "POST", "/api/v2/torrents/stop", {"hashes": infohash})
        _request(request_fn, "POST", "/api/v2/torrents/delete", {"hashes": infohash, "deleteFiles": "false"})
        if _info(request_fn, infohash):
            raise SearchFailure("CLEANUP_FAILED", "owned temporary torrent remained after deletion")
        return True
    except SearchFailure:
        raise
    except Exception as exc:
        raise SearchFailure("CLEANUP_FAILED", f"temporary torrent cleanup failed: {type(exc).__name__}") from exc


def _reconcile_request(
    request_fn: Callable[[str, str, dict[str, Any] | None], Any],
    request_tag: str,
) -> dict[str, Any] | None:
    """Reconcile a request using qBittorrent tags after a gateway restart."""
    try:
        matches = [item for item in _all_torrents(request_fn) if _owned(item, request_tag)]
    except Exception as exc:
        raise SearchFailure("QBITTORRENT_UNAVAILABLE", f"request reconciliation failed: {type(exc).__name__}") from exc
    if not matches:
        return None
    if len(matches) > 1:
        raise SearchFailure("ALREADY_DOWNLOADING", "multiple torrents are tagged for this request")
    item = matches[0]
    infohash = str(item.get("hash") or "")
    files = _files(request_fn, infohash)
    if _is_started_torrent(item, files):
        return {"infohash": infohash, "name": item.get("name") or "unknown"}
    task_tag = next((part.strip() for part in str(item.get("tags") or "").split(",") if part.strip().startswith("hermes-task-")), "")
    if task_tag and _owned(item, "hermes-preflight"):
        _cleanup(request_fn, infohash, task_tag)
    raise SearchFailure("REQUEST_STATE_UNKNOWN", "request state was lost during gateway restart")




def _preflight(
    request_fn: Callable[[str, str, dict[str, Any] | None], Any],
    candidate: TorrentCandidate,
    tag: str,
    sleep_fn: Callable[[float], None],
    request_tag: str = "",
) -> list[dict[str, Any]]:
    _check_operation_cancelled()
    if _info(request_fn, candidate.infohash):
        raise SearchFailure("ALREADY_DOWNLOADING", "infohash already exists and ownership is not proven")
    try:
        _request(request_fn, "POST", "/api/v2/torrents/add", {
            "urls": candidate.magnet_uri,
            "category": "MEDIA",
            "tags": ",".join(filter(None, ("hermes-preflight", tag, request_tag))),
            "paused": "false",
            "stopCondition": "MetadataReceived",
        })
    except Exception:
        # A lost response after a write is ambiguous.  Verify the infohash
        # before any caller can consider a retry; never submit the magnet a
        # second time merely because the HTTP response was lost.
        info = _info(request_fn, candidate.infohash)
        if not info:
            raise
        if not _owned(info[0], tag):
            raise SearchFailure("ALREADY_DOWNLOADING", "write outcome conflicts with an unowned torrent")
    _check_operation_cancelled()
    deadline = time.monotonic() + METADATA_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        _check_operation_cancelled()
        info = _info(request_fn, candidate.infohash)
        files = _files(request_fn, candidate.infohash)
        if files:
            _check_operation_cancelled()
            _request(request_fn, "POST", "/api/v2/torrents/stop", {"hashes": candidate.infohash})
            after = _info(request_fn, candidate.infohash)
            if after and _owned(after[0], tag):
                downloaded = _to_int(after[0].get("amount_downloaded"), 0)
                total_size = _to_int(after[0].get("size"), 0)
                if downloaded > 0 or (total_size > 0 and downloaded >= total_size):
                    raise SearchFailure("SAFE_PREFLIGHT_UNAVAILABLE", "payload bytes appeared before validation")
                return files
        sleep_fn(POLL_SECONDS)
    raise MetadataTimeout()


def _set_single_priority(request_fn: Callable[[str, str, dict[str, Any] | None], Any], infohash: str, selected: int, files: list[dict[str, Any]]) -> None:
    for item in files:
        _check_operation_cancelled()
        index = _to_int(item.get("index"), -1)
        if index < 0:
            raise SearchFailure("MULTIPLE_FILES_SELECTED", "file index missing")
        _request(request_fn, "POST", "/api/v2/torrents/filePrio", {
            "hash": infohash, "id": index, "priority": 1 if index == selected else 0,
        })
    _check_operation_cancelled()
    verified = _files(request_fn, infohash)
    wanted = [_to_int(item.get("index"), -1) for item in verified if _to_int(item.get("priority"), 0) > 0]
    if wanted != [selected]:
        raise SearchFailure("MULTIPLE_FILES_SELECTED", "qBittorrent did not confirm exactly one wanted file")


def _summary(candidate: TorrentCandidate, selected: ValidatedFile | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "torrent_name": candidate.name,
        "magnet_uri": candidate.magnet_uri,
        "language": candidate.language.upper(),
        "quality": quality_label(candidate.name),
        "source": release_source_label(candidate.name),
        "engine": candidate.engine,
        "infohash": candidate.infohash,
    }
    if selected:
        result.update({
            "file_name": selected.file.name,
            "file_size_bytes": selected.file.size,
            "file_size_gib": round(selected.file.size / GIB, 3),
        })
    return result


def _rejection(candidate_number: int, candidate: TorrentCandidate, reason: str) -> dict[str, Any]:
    return {
        "candidate": candidate_number,
        "infohash": candidate.infohash,
        "reason": reason,
        "error": reason,
    }


def _failure_result(
    error: str,
    message: str,
    phase: OperationState,
    diagnostics: dict[str, Any] | None = None,
    rejections: list[dict[str, Any]] | None = None,
    started: int = 0,
) -> dict[str, Any]:
    rejected = list(rejections or [])
    result: dict[str, Any] = {
        "ok": False,
        "state": "cancelled" if error == "REQUEST_CANCELLED" else "failed",
        "error": error,
        "message": message,
        "operation_state": phase.value,
        "preflight_attempts": len(rejected),
        "rejections": rejected,
        "rejected": rejected,
        "torrent_count_started": started,
    }
    if diagnostics:
        result["search_diagnostics"] = diagnostics
    return result


def execute(request_fn: Callable[[str, str, dict[str, Any] | None], Any], args: dict[str, Any], sleep_fn: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    _check_operation_cancelled()
    query = " ".join(str(args.get("query") or "").split())
    if not query:
        return {"ok": False, "error": "NO_RESULTS", "message": "query is required"}
    media_type = str(args.get("media_type") or "video").casefold()
    if media_type != "video":
        return {"ok": False, "error": "UNSUPPORTED_MEDIA_TYPE", "message": "only media_type=video is supported"}
    try:
        min_gib = float(args.get("min_size_gib", 1))
        max_gib = float(args.get("max_size_gib", 5))
        if min_gib < 0 or max_gib < min_gib:
            raise ValueError
    except (TypeError, ValueError):
        return {"ok": False, "error": "INVALID_SIZE_RANGE", "message": "invalid GiB range"}
    preferred_resolution, preferred_extension = extract_media_preferences(query)
    policy = SelectionPolicy(
        min_size_bytes=int(min_gib * GIB),
        max_size_bytes=int(max_gib * GIB),
        language_preference=tuple(str(item).casefold() for item in (args.get("language_preference") or ["lat", "en", "unknown"])),
        prefer_recent=bool(args.get("prefer_recent", True)),
        allow_cam=bool(args.get("allow_cam", True)),
        preferred_resolution=preferred_resolution,
        preferred_extension=preferred_extension,
    )
    fingerprint = request_fingerprint(args, policy)
    if not _OPERATION_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "BUSY", "message": "another torrent-auto-download operation is active"}
    try:
        phase = OperationState.SEARCHING
        request_tag = _request_tag(fingerprint)
        diagnostics: dict[str, Any] = {}
        if not bool(args.get("dry_run", False)) and fingerprint in _STARTED_FINGERPRINTS:
            return {"ok": False, "error": "DUPLICATE_TORRENT", "message": "request fingerprint already started"}
        if not bool(args.get("dry_run", False)):
            reconciled = _reconcile_request(request_fn, request_tag)
            if reconciled:
                return {
                    "ok": False,
                    "state": "started",
                    "error": "ALREADY_DOWNLOADING",
                    "message": "request already has a reconciled torrent",
                    "torrent_count_started": 1,
                    "selected": reconciled,
                }
        candidates = search_btdig(request_fn, query, policy, sleep_fn, diagnostics)
        if not policy.allow_cam:
            candidates = [item for item in candidates if item.quality != "LOW_QUALITY_FALLBACK"]
        if not candidates:
            return _failure_result(
                "NO_VALID_CANDIDATES",
                "no candidate met language/quality policy",
                OperationState.FAILED,
                diagnostics,
            )
        if bool(args.get("dry_run", False)):
            return {
                "ok": True,
                "state": "dry_run",
                "query": query,
                "candidate": _summary(candidates[0]),
                "preferences": {
                    "resolution": policy.preferred_resolution,
                    "extension": policy.preferred_extension,
                },
                "preflight": False,
                "search_diagnostics": diagnostics,
            }
        rejected: list[dict[str, Any]] = []
        for candidate_number, candidate in enumerate(candidates[:policy.max_preflight_candidates], start=1):
            _check_operation_cancelled()
            phase = OperationState.PREFLIGHT
            tag = f"hermes-task-{uuid.uuid4().hex[:12]}"
            try:
                files = _preflight(request_fn, candidate, tag, sleep_fn, request_tag=request_tag)
                phase = OperationState.VALIDATING
                selected = select_single_file(files, query, policy, candidate.name)
                phase = OperationState.READY_TO_START
                _check_operation_cancelled()
                _set_single_priority(request_fn, candidate.infohash, selected.file.index, files)
                _check_operation_cancelled()
                phase = OperationState.STARTING
                try:
                    _request(request_fn, "POST", "/api/v2/torrents/start", {"hashes": candidate.infohash})
                except Exception as start_exc:
                    try:
                        after_info = _info(request_fn, candidate.infohash)
                        after_files = _files(request_fn, candidate.infohash)
                    except Exception as reconcile_exc:
                        raise SearchFailure("START_UNCERTAIN", "start result could not be reconciled") from reconcile_exc
                    if _is_started_torrent(after_info[0], after_files) if after_info else False:
                        phase = OperationState.STARTED
                        _STARTED_FINGERPRINTS.add(fingerprint)
                        return {
                            "ok": True, "state": "started", "query": query,
                            "selected": _summary(candidate, selected),
                            "preferences": {"resolution": policy.preferred_resolution, "extension": policy.preferred_extension},
                            "download": {"torrent_count_started": MAX_TORRENTS_STARTED_PER_REQUEST, "payload_files_selected": MAX_PAYLOAD_FILES},
                            "reconciled": True,
                            "search_diagnostics": diagnostics,
                        }
                    raise SearchFailure("START_UNCERTAIN", "qBittorrent start response was not confirmed") from start_exc
                after_info = _info(request_fn, candidate.infohash)
                after_files = _files(request_fn, candidate.infohash)
                wanted = [item for item in after_files if _to_int(item.get("priority"), 0) > 0]
                if not after_info or not wanted or len(wanted) != MAX_PAYLOAD_FILES or _to_int(wanted[0].get("index"), -1) != selected.file.index:
                    return _failure_result(
                        "STARTED_INVARIANT_FAILED",
                        "torrent started but the single-file invariant was not confirmed",
                        OperationState.STARTED,
                        diagnostics,
                        rejected,
                        started=MAX_TORRENTS_STARTED_PER_REQUEST,
                    )
                phase = OperationState.STARTED
                _STARTED_FINGERPRINTS.add(fingerprint)
                return {
                    "ok": True, "state": "started", "query": query,
                    "selected": _summary(candidate, selected),
                    "preferences": {"resolution": policy.preferred_resolution, "extension": policy.preferred_extension},
                    "download": {"torrent_count_started": MAX_TORRENTS_STARTED_PER_REQUEST, "payload_files_selected": MAX_PAYLOAD_FILES},
                    "search_diagnostics": diagnostics,
                }
            except SearchFailure as exc:
                if exc.code in FALLBACK_REJECTION_CODES:
                    rejected.append(_rejection(candidate_number, candidate, exc.code))
                    try:
                        _cleanup(request_fn, candidate.infohash, tag)
                    except SearchFailure as cleanup_exc:
                        phase = OperationState.FAILED
                        return _failure_result(cleanup_exc.code, cleanup_exc.message, phase, diagnostics, rejected)
                    continue
                if exc.code == "REQUEST_CANCELLED":
                    try:
                        _cleanup(request_fn, candidate.infohash, tag)
                    except SearchFailure as cleanup_exc:
                        return _failure_result(cleanup_exc.code, cleanup_exc.message, OperationState.CANCELLED, diagnostics, rejected)
                    return _failure_result(exc.code, exc.message, OperationState.CANCELLED, diagnostics, rejected)
                return _failure_result(exc.code, exc.message, OperationState.FAILED, diagnostics, rejected)
            except Exception as exc:
                try:
                    _cleanup(request_fn, candidate.infohash, tag)
                except SearchFailure as cleanup_exc:
                    return _failure_result(cleanup_exc.code, cleanup_exc.message, OperationState.FAILED, diagnostics, rejected)
                return _failure_result("QBITTORRENT_UNAVAILABLE", type(exc).__name__, OperationState.FAILED, diagnostics, rejected)
        return _failure_result(
            "NO_VALID_CANDIDATES",
            "all bounded preflight candidates were rejected",
            OperationState.FAILED,
            diagnostics,
            rejected,
        )
    except SearchFailure as exc:
        phase = OperationState.CANCELLED if exc.code == "REQUEST_CANCELLED" else OperationState.FAILED
        return _failure_result(exc.code, exc.message, phase, locals().get("diagnostics"), locals().get("rejected"))
    except Exception as exc:
        LOGGER.warning("qBittorrent smart download failed: %s", type(exc).__name__)
        return _failure_result("QBITTORRENT_UNAVAILABLE", type(exc).__name__, OperationState.FAILED, locals().get("diagnostics"), locals().get("rejected"))
    finally:
        _OPERATION_LOCK.release()
