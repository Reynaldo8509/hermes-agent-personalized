#!/usr/bin/env python3
"""Authenticated receiver for the iOS Hermes network/location Shortcut."""
from __future__ import annotations

import hmac
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LOG = logging.getLogger("hermes.network_ingest")
MAX_BODY_BYTES = 8192
STATE_LOCK = threading.Lock()
HOME_SSID = os.environ.get("HERMES_HOME_SSID", "CELERITY_GONZALEZ")
TRUSTED_PROXY = os.environ.get("HERMES_NETWORK_INGEST_TRUSTED_PROXY", "192.0.2.10")


def _configured_token() -> str:
    """Read a direct test token or the base64-encoded protected systemd value."""
    direct = os.environ.get("HERMES_NETWORK_INGEST_TOKEN", "")
    if direct:
        return direct
    encoded = os.environ.get("HERMES_NETWORK_INGEST_TOKEN_B64", "")
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


ECUADOR_TIMEZONE = ZoneInfo("America/Guayaquil")

def _ecuador_now() -> str:
    """Return an offset-aware local timestamp for Ecuador (UTC-05:00)."""
    return datetime.now(ECUADOR_TIMEZONE).isoformat(timespec="seconds")


def _clean_text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit or any(ord(c) < 32 and c not in "\t" for c in value):
        raise ValueError(f"{field} is invalid")
    return value


def _canonical_device(value: object) -> str:
    name = _clean_text(value, "device", 80, required=True)
    key = re.sub(r"\s+", " ", name).casefold().replace("iphone 6 s", "iphone 6s")
    aliases = {
        "iphone 6s de reynaldo": "iPhone 6S de Reynaldo",
        "iphone 6 s de reynaldo": "iPhone 6S de Reynaldo",
    }
    return aliases.get(key, name)


def _parse_ip(value: object) -> str:
    text = _clean_text(value, "local_ip", 64)
    if not text or text.casefold() in {"not connected", "unavailable", "unknown"}:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError as exc:
        raise ValueError("local_ip must be an IP address or empty") from exc


def _coordinate(value: object, field: str, low: float, high: float) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if not low <= result <= high:
        raise ValueError(f"{field} is out of range")
    return result


def normalize_payload(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("JSON body must be an object")
    event = _clean_text(raw.get("event"), "event", 64, required=True)
    if event != "wifi_network_report":
        raise ValueError("unsupported event")
    ssid = _clean_text(raw.get("ssid"), "ssid", 255)
    label = _clean_text(raw.get("place_label"), "place_label", 500)
    return {
        "device": _canonical_device(raw.get("device")),
        "event": event,
        "ssid": ssid,
        "local_ip": _parse_ip(raw.get("local_ip")),
        "latitude": _coordinate(raw.get("latitude"), "latitude", -90, 90),
        "longitude": _coordinate(raw.get("longitude"), "longitude", -180, 180),
        "place_label": label,
        "at_home": bool(ssid) and ssid.casefold() == HOME_SSID.casefold(),
    }


def _init_database(state_dir: Path) -> Path:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    database = state_dir / "reports.sqlite3"
    with sqlite3.connect(database, timeout=10) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("""CREATE TABLE IF NOT EXISTS network_reports (
            id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL,
            device TEXT NOT NULL,
            event TEXT NOT NULL,
            ssid TEXT NOT NULL,
            local_ip TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            place_label TEXT NOT NULL,
            at_home INTEGER NOT NULL,
            source_ip TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_network_reports_device_time ON network_reports(device, received_at)")
        con.execute("""CREATE TABLE IF NOT EXISTS additional_tokens (
            token_hash TEXT PRIMARY KEY,
            device TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL,
            source_ip TEXT NOT NULL
        )""")
    os.chmod(database, 0o600)
    return database


def _authorize_additional_token(token: str, device: str, source_ip: str, state_dir: Path) -> str | None:
    """Enroll at most two first-seen bearer tokens, storing hashes only and pinning device identity."""
    if not 32 <= len(token) <= 512:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    database = _init_database(state_dir)
    with sqlite3.connect(database, timeout=10) as con:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT device FROM additional_tokens WHERE token_hash=?", (digest,)
        ).fetchone()
        if row:
            con.rollback()
            return "known" if hmac.compare_digest(row[0], device) else None
        if con.execute("SELECT COUNT(*) FROM additional_tokens").fetchone()[0] >= 2:
            con.rollback()
            return None
        # Prevent an unknown bearer from taking over a device already reported by the primary token.
        if con.execute("SELECT 1 FROM network_reports WHERE device=? LIMIT 1", (device,)).fetchone():
            con.rollback()
            return None
        try:
            con.execute(
                "INSERT INTO additional_tokens(token_hash, device, first_seen_at, source_ip) VALUES (?, ?, ?, ?)",
                (digest, device, _ecuador_now(), source_ip),
            )
            con.commit()
            return "registered"
        except sqlite3.IntegrityError:
            con.rollback()
            return None


def _write_state(state_file: Path, report: dict) -> None:
    try:
        current = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"version": 1, "devices": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("presence state is unreadable; preserving it") from exc
    if not isinstance(current, dict) or not isinstance(current.get("devices", {}), dict):
        raise RuntimeError("presence state has an unexpected structure; preserving it")
    current.setdefault("version", 1)
    current.setdefault("devices", {})
    public_report = {key: value for key, value in report.items() if key != "id"}
    current["devices"][report["device"]] = public_report
    current["updated_at"] = report["received_at"]
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=state_file.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(current, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_file)
        os.chmod(state_file, 0o600)
        dir_fd = os.open(state_file.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def persist_report(payload: dict, source_ip: str, state_dir: Path) -> dict:
    report = dict(payload)
    report.update(id=str(uuid.uuid4()), received_at=_ecuador_now(), source_ip=source_ip)
    database = _init_database(state_dir)
    with STATE_LOCK:
        with sqlite3.connect(database, timeout=10) as con:
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("""INSERT INTO network_reports
                (id, received_at, device, event, ssid, local_ip, latitude, longitude, place_label, at_home, source_ip)
                VALUES (:id, :received_at, :device, :event, :ssid, :local_ip, :latitude, :longitude, :place_label, :at_home, :source_ip)""", report)
            con.commit()
        _write_state(state_dir / "state.json", report)
    return report


def _trusted_source(handler: BaseHTTPRequestHandler) -> str:
    peer = str(handler.client_address[0])
    try:
        if ipaddress.ip_address(peer) == ipaddress.ip_address(TRUSTED_PROXY):
            forwarded = handler.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            if forwarded:
                return str(ipaddress.ip_address(forwarded))
    except ValueError:
        pass
    return peer


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "HermesNetworkIngest/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log authorization headers, report bodies, SSIDs, or GPS values.
        LOG.info("client=%s %s", self.client_address[0], fmt % args)

    def _respond(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/healthz":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/iphone-network":
            self._respond(404, {"error": "not_found"})
            return
        token = _configured_token()
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:].strip() if auth[:7].casefold() == "bearer " else ""
        primary_token = bool(token and supplied and hmac.compare_digest(supplied, token))
        if not supplied or (not primary_token and not 32 <= len(supplied) <= 512):
            self._respond(401, {"error": "unauthorized"})
            return
        if self.headers.get_content_type() != "application/json":
            self._respond(415, {"error": "application_json_required"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._respond(411, {"error": "content_length_required"})
            return
        if length < 1 or length > MAX_BODY_BYTES:
            self._respond(413, {"error": "payload_size_invalid"})
            return
        try:
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = normalize_payload(raw)
            state_dir = Path(os.environ.get("HERMES_NETWORK_INGEST_STATE_DIR", "/var/lib/hermes-network-ingest"))
            source_ip = _trusted_source(self)
            token_status = "primary" if primary_token else _authorize_additional_token(
                supplied, payload["device"], source_ip, state_dir
            )
            if token_status is None:
                self._respond(401, {"error": "unauthorized"})
                return
            report = persist_report(payload, source_ip, state_dir)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._respond(400, {"error": str(exc)})
            return
        except Exception:
            LOG.exception("authenticated report could not be persisted")
            self._respond(500, {"error": "persistence_failed"})
            return
        self._respond(200, {
            "ok": True,
            "report_id": report["id"],
            "device": report["device"],
            "received_at": report["received_at"],
            "at_home": report["at_home"],
            "token_status": token_status,
        })


def main() -> None:
    token = _configured_token()
    if len(token) < 32:
        raise SystemExit("HERMES_NETWORK_INGEST_TOKEN is missing or too short")
    host = os.environ.get("HERMES_NETWORK_INGEST_BIND", "192.0.2.10")
    port = int(os.environ.get("HERMES_NETWORK_INGEST_PORT", "5000"))
    state_dir = Path(os.environ.get("HERMES_NETWORK_INGEST_STATE_DIR", "/var/lib/hermes-network-ingest"))
    _init_database(state_dir)
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.daemon_threads = True
    LOG.info("network ingest listening on %s:%d", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
