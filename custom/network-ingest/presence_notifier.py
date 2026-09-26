#!/usr/bin/env python3
"""Deduplicated home-arrival/departure Telegram notifier driven by Shortcut and HA."""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger("hermes.presence_notifier")
HERMES_HOME = Path("/home/example-user/hermes")
STATE_DIR = HERMES_HOME / "network-ingest/state"
DATABASE = STATE_DIR / "reports.sqlite3"
HA_API = "http://127.0.0.1:8123/api/states"
LOCAL_TZ = ZoneInfo("America/Guayaquil")
POLL_SECONDS = 20
TRACKER_MAX_AGE = timedelta(minutes=20)
OUTBOX_TTL = timedelta(hours=24)

PEOPLE = {
    "reynaldo": {
        "device_hint": "reynaldo",
        "person_entity": "person.reynaldo_amado_rodriguez_gonzalez",
        "tracker_entity": "device_tracker.iphone_6s_rey",
        "connection_entity": "sensor.iphone_6s_rey_connection_type",
        "telegram_name": "reynaldo",
        "arrival": "Bienvenido a Casa, Reynaldo",
        "departure": "Vuelve a Casa Pronto, Reynaldo",
    },
    "display_one": {
        "device_hint": "display_one",
        "person_entity": "person.display_one_sanchez_gonzalez",
        "tracker_entity": "device_tracker.iphone_14_display_one",
        "connection_entity": "sensor.iphone_14_display_one_connection_type",
        "telegram_name": "display_one",
        "notify": True,
        "arrival": "Bienvenida a casa, display_one. Te conectaste a la Wi-Fi Celerity_Gonzalez.",
        "departure": "Vuelve pronto a casa, display_one",
    },
    "device_two": {
        "device_hint": "device_two",
        "person_entity": "person.device_two_sanchez_gonzalez",
        "tracker_entity": "device_tracker.iphone_14_device_two",
        "connection_entity": "sensor.iphone_14_device_two_connection_type",
        "telegram_name": None,
        "notify": False,
        "arrival": "",
        "departure": "",
    },
}


def _local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _local_iso() -> str:
    return _local_now().isoformat(timespec="seconds")


def _read_env_key(path: Path, wanted: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith(wanted + "="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def _get_credentials() -> tuple[str, str]:
    hass = _read_env_key(HERMES_HOME / "integrations-vps.env", "HASS_TOKEN")
    telegram = _read_env_key(HERMES_HOME / "telegram-vps.env", "TELEGRAM_BOT_TOKEN")
    return hass, telegram


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DATABASE, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _init_schema() -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    with _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS presence_notifier_people (
            person_key TEXT PRIMARY KEY,
            presence TEXT NOT NULL,
            last_home_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS presence_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transition_key TEXT NOT NULL UNIQUE,
            person_key TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result_code INTEGER
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_presence_outbox_status ON presence_notification_outbox(status, created_at)")
        con.execute("""CREATE TABLE IF NOT EXISTS presence_notifier_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            last_report_rowid INTEGER NOT NULL
        )""")
    os.chmod(DATABASE, 0o600)


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _states_map(states: object) -> dict[str, dict]:
    if not isinstance(states, list):
        return {}
    return {x.get("entity_id"): x for x in states if isinstance(x, dict) and isinstance(x.get("entity_id"), str)}


def _get_ha_states(token: str) -> dict[str, dict]:
    if not token:
        return {}
    entity_ids = {"zone.home"}
    for config in PEOPLE.values():
        entity_ids.update((config["person_entity"], config["tracker_entity"], config["connection_entity"]))
    result: dict[str, dict] = {}
    headers = {"Authorization": "Bearer " + token}
    for entity_id in entity_ids:
        url = HA_API + "/" + urllib.parse.quote(entity_id, safe="")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:
                state = json.load(response)
            if isinstance(state, dict):
                result[entity_id] = state
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    return result


def _ha_age_ok(state: dict, attribute: str, max_age: timedelta) -> bool:
    changed = _parse_dt(state.get(attribute))
    return changed is not None and datetime.now(timezone.utc) - changed.astimezone(timezone.utc) <= max_age


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _coordinate(state: dict, key: str) -> float | None:
    value = state.get("attributes", {}).get(key)
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _device_person(device: str) -> str | None:
    value = device.casefold()
    for person, config in PEOPLE.items():
        if config["device_hint"] in value:
            return person
    return None


def _enqueue(con: sqlite3.Connection, person: str, transition: str, key: str) -> None:
    if not PEOPLE[person].get("notify", True):
        return
    transition_key = f"{person}:{transition}:{key}"
    con.execute(
        "INSERT OR IGNORE INTO presence_notification_outbox(transition_key, person_key, message, created_at) VALUES (?, ?, ?, ?)",
        (transition_key, person, PEOPLE[person][transition], _local_iso()),
    )


def _initialize(states: dict[str, dict]) -> None:
    """Seed from current Shortcut/HA state without announcing initial presence."""
    with _db() as con:
        cursor_exists = con.execute("SELECT 1 FROM presence_notifier_state WHERE id=1").fetchone() is not None
        max_row = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM network_reports").fetchone()[0]
        reports = con.execute("SELECT rowid, device, at_home, received_at FROM network_reports ORDER BY rowid DESC LIMIT 500").fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for report in reports:
            person = _device_person(report["device"])
            if person and person not in latest:
                latest[person] = report
        for person, config in PEOPLE.items():
            already_tracked = con.execute(
                "SELECT 1 FROM presence_notifier_people WHERE person_key=?", (person,)
            ).fetchone()
            if already_tracked:
                continue
            report = latest.get(person)
            presence = "unknown"
            home_at = ""
            if report and report["at_home"]:
                presence = "home"
                home_at = report["received_at"]
            else:
                tracker = states.get(config["tracker_entity"], {})
                person_state = states.get(config["person_entity"], {})
                if tracker.get("state") == "home" or person_state.get("state") == "home":
                    presence = "home"
                    home_at = _local_iso()
                elif tracker.get("state") == "not_home" or person_state.get("state") == "not_home":
                    presence = "away"
            con.execute(
                "INSERT OR IGNORE INTO presence_notifier_people(person_key,presence,last_home_at,updated_at) VALUES (?,?,?,?)",
                (person, presence, home_at, _local_iso()),
            )
        if not cursor_exists:
            con.execute("INSERT OR IGNORE INTO presence_notifier_state(id,last_report_rowid) VALUES(1,?)", (max_row,))


def _process_reports() -> None:
    with _db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS presence_notifier_state (id INTEGER PRIMARY KEY CHECK(id=1), last_report_rowid INTEGER NOT NULL)")
        cursor = con.execute("SELECT last_report_rowid FROM presence_notifier_state WHERE id=1").fetchone()
        if not cursor:
            return
        reports = con.execute(
            "SELECT rowid, id, device, at_home, received_at FROM network_reports WHERE rowid>? ORDER BY rowid",
            (cursor[0],),
        ).fetchall()
        for report in reports:
            person = _device_person(report["device"])
            if person and report["at_home"]:
                current = con.execute("SELECT presence FROM presence_notifier_people WHERE person_key=?", (person,)).fetchone()
                state = current["presence"] if current else "unknown"
                if state == "away":
                    _enqueue(con, person, "arrival", report["id"])
                con.execute(
                    "INSERT INTO presence_notifier_people(person_key,presence,last_home_at,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(person_key) DO UPDATE SET presence='home',last_home_at=excluded.last_home_at,updated_at=excluded.updated_at",
                    (person, "home", report["received_at"], _local_iso()),
                )
            con.execute("UPDATE presence_notifier_state SET last_report_rowid=? WHERE id=1", (report["rowid"],))


def _process_departures(states: dict[str, dict]) -> None:
    """Track home transitions from fresh GPS; mobile-app connection sensors are optional."""
    zone = states.get("zone.home", {})
    home_lat, home_lon = _coordinate(zone, "latitude"), _coordinate(zone, "longitude")
    if home_lat is None or home_lon is None:
        return
    now_utc = datetime.now(timezone.utc)
    for person, config in PEOPLE.items():
        tracker = states.get(config["tracker_entity"], {})
        if not tracker or not _ha_age_ok(tracker, "last_updated", TRACKER_MAX_AGE):
            continue
        lat, lon = _coordinate(tracker, "latitude"), _coordinate(tracker, "longitude")
        transition_at = _parse_dt(tracker.get("last_changed"))
        if lat is None or lon is None or transition_at is None:
            continue
        if transition_at.astimezone(timezone.utc) > now_utc:
            continue
        distance = _distance_m(home_lat, home_lon, lat, lon)
        tracker_state = str(tracker.get("state", "")).casefold().strip()
        transition_key = transition_at.astimezone(LOCAL_TZ).isoformat(timespec="seconds")
        with _db() as con:
            current = con.execute(
                "SELECT presence,last_home_at FROM presence_notifier_people WHERE person_key=?", (person,)
            ).fetchone()
            if not current:
                continue
            home_at = _parse_dt(current["last_home_at"])
            if home_at and transition_at.astimezone(timezone.utc) <= home_at.astimezone(timezone.utc):
                continue
            if tracker_state == "not_home" and distance > 100:
                if current["presence"] == "home":
                    _enqueue(con, person, "departure", "ha:" + transition_key)
                    con.execute(
                        "UPDATE presence_notifier_people SET presence='away',updated_at=? WHERE person_key=?",
                        (_local_iso(), person),
                    )
                elif current["presence"] == "unknown":
                    con.execute(
                        "UPDATE presence_notifier_people SET presence='away',updated_at=? WHERE person_key=?",
                        (_local_iso(), person),
                    )
            elif tracker_state == "home" and distance <= 100 and current["presence"] == "away":
                _enqueue(con, person, "arrival", "ha:" + transition_key)
                con.execute(
                    "UPDATE presence_notifier_people SET presence='home',last_home_at=?,updated_at=? WHERE person_key=?",
                    (transition_at.astimezone(LOCAL_TZ).isoformat(timespec="seconds"), _local_iso(), person),
                )

def _telegram_targets() -> dict[str, str]:
    """Resolve only unique private Telegram chats whose names match the requested people."""
    try:
        con = sqlite3.connect(f"file:{HERMES_HOME / 'state.db'}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT display_name, chat_id FROM sessions WHERE source='telegram' AND chat_type='dm' AND chat_id IS NOT NULL"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    found: dict[str, set[str]] = {key: set() for key in PEOPLE}
    for name, chat_id in rows:
        normalized = str(name or "").casefold()
        for person, config in PEOPLE.items():
            telegram_name = config.get("telegram_name")
            if telegram_name and telegram_name in normalized:
                found[person].add(str(chat_id))
    return {person: next(iter(ids)) for person, ids in found.items() if len(ids) == 1}


def _send_telegram(bot_token: str, chat_id: str, message: str) -> tuple[bool, int | None]:
    url = "https://api.telegram.org/bot" + bot_token + "/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.load(response)
            return data.get("ok") is True, response.status
    except urllib.error.HTTPError as exc:
        return False, exc.code
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False, None


def _deliver_outbox(bot_token: str) -> None:
    if not bot_token:
        return
    targets = _telegram_targets()
    cutoff = (_local_now() - OUTBOX_TTL).isoformat(timespec="seconds")
    with _db() as con:
        con.execute("UPDATE presence_notification_outbox SET status='expired' WHERE status='pending' AND created_at<?", (cutoff,))
        pending = con.execute(
            "SELECT id,person_key,message FROM presence_notification_outbox WHERE status='pending' ORDER BY id LIMIT 20"
        ).fetchall()
    for item in pending:
        chat_id = targets.get(item["person_key"])
        if not chat_id:
            continue
        # Mark before sending: a process crash cannot cause duplicate delivery after restart.
        with _db() as con:
            changed = con.execute(
                "UPDATE presence_notification_outbox SET status='sending' WHERE id=? AND status='pending'",
                (item["id"],),
            ).rowcount
        if not changed:
            continue
        ok, result_code = _send_telegram(bot_token, chat_id, item["message"])
        with _db() as con:
            con.execute(
                "UPDATE presence_notification_outbox SET status=? ,result_code=? WHERE id=? AND status='sending'",
                ("sent" if ok else "failed", result_code, item["id"]),
            )
        LOG.info("notification person=%s result=%s", item["person_key"], "sent" if ok else "failed")


def run() -> None:
    _init_schema()
    token, bot_token = _get_credentials()
    if not token or not bot_token:
        raise SystemExit("required protected credentials are unavailable")
    initialized = False
    while True:
        states = _get_ha_states(token)
        if not initialized:
            _initialize(states)
            initialized = True
        if initialized:
            _process_reports()
            _process_departures(states)
            _deliver_outbox(bot_token)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
