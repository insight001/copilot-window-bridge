"""Bounded, local-only connection discovery for Copilot Window Bridge."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import http.client
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
import uuid


DESCRIPTOR_LIMIT = 64 * 1024
RESPONSE_LIMIT = 1024 * 1024
PROBE_TIMEOUT = 1.0
METADATA_KEYS = ("workspace", "folders", "vscodeVersion", "bridgeVersion", "appName", "pid", "startedAt")


class ClientError(Exception):
    """An error safe to show without descriptor contents or tokens."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(value):
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
        raise ClientError("Connection URL must be a string without whitespace.")
    try:
        parts = urllib.parse.urlsplit(value)
        valid = (
            parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"}
            and parts.username is None and parts.password is None
            and parts.port is not None and 1 <= parts.port <= 65535
            and parts.path in {"", "/"} and "?" not in value and "#" not in value
        )
    except ValueError as exc:
        raise ClientError("Connection URL is invalid.") from exc
    if not valid:
        raise ClientError(
            "Connection URL must be http://127.0.0.1:PORT or http://[::1]:PORT, "
            "without credentials, paths, queries or fragments."
        )
    host = "[::1]" if parts.hostname == "::1" else "127.0.0.1"
    return "http://" + host + ":" + str(parts.port)


def redact(value, token):
    if isinstance(value, str):
        return value.replace(token, "[redacted]") if token else value
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {
            redact(key, token): "[redacted]" if str(key).lower() in {
                "token", "authorization", "password", "secret"
            } else redact(item, token)
            for key, item in value.items()
        }
    return value


def registry_directory(override=None):
    if override is not None:
        return Path(override).expanduser().absolute()
    configured = os.environ.get("COPILOT_WINDOW_BRIDGE_DIR")
    if configured is not None:
        directory = Path(configured)
        if not directory.is_absolute():
            raise ClientError("COPILOT_WINDOW_BRIDGE_DIR must be an absolute directory path.")
        return directory
    return Path.home() / ".copilot-window-bridge" / "connections"


@dataclass
class Connection:
    path: Path
    url: str
    token: str = field(repr=False)
    instance_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class Record:
    path: Path
    status: str
    detail: str
    connection: Connection = None
    health: dict = field(default_factory=dict, repr=False)

    def public(self):
        result = {"status": self.status, "detail": self.detail, "connectionFile": str(self.path)}
        if self.connection is None:
            return result
        connection = self.connection
        metadata = dict(connection.metadata)
        for key in METADATA_KEYS:
            if key in self.health and valid_metadata_value(key, self.health[key]):
                metadata[key] = self.health[key]
        result.update(metadata)
        result.update(instanceId=connection.instance_id, url=connection.url)
        return redact(result, connection.token)


def valid_metadata_value(key, value):
    if key == "folders":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if key == "pid":
        return type(value) is int and value > 0
    return isinstance(value, str)


def read_connection(path):
    """Read one regular descriptor, never following a descriptor symlink."""
    path = Path(path).expanduser().absolute()
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ClientError("Connection file is a symlink; use the original regular file.")
        if not stat.S_ISREG(before.st_mode):
            raise ClientError("Connection descriptor is not a regular file.")
        if before.st_size > DESCRIPTOR_LIMIT:
            raise ClientError("Connection descriptor exceeds the 64 KiB limit.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ClientError("Connection file changed while being opened; list windows again.")
            raw = source.read(DESCRIPTOR_LIMIT + 1)
        if len(raw) > DESCRIPTOR_LIMIT:
            raise ClientError("Connection descriptor exceeds the 64 KiB limit.")
    except OSError as exc:
        raise ClientError("Cannot read connection file; check that it exists and is readable.") from exc
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ClientError("Connection descriptor is not valid UTF-8 JSON.") from exc
    if not isinstance(data, dict):
        raise ClientError("Connection descriptor must contain a JSON object.")
    url = validate_url(data.get("url"))
    token = data.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]+", token):
        raise ClientError("Connection descriptor needs a nonempty hexadecimal token.")
    identity = data.get("instanceId")
    try:
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity.lower():
            raise ValueError()
    except ValueError as exc:
        raise ClientError("Connection descriptor needs a complete UUID instanceId.") from exc
    metadata = {}
    for key in METADATA_KEYS:
        if key in data:
            if not valid_metadata_value(key, data[key]):
                raise ClientError("Connection descriptor has invalid " + key + " metadata.")
            metadata[key] = data[key]
    if "workspace" not in metadata:
        raise ClientError("Connection descriptor needs a workspace name.")
    metadata.setdefault("folders", [])
    return Connection(path, url, token, identity, metadata)


def http_hint(status):
    if 300 <= status < 400:
        return "Redirect refused; bridge requests must stay at their recorded loopback address."
    if status in {401, 403}:
        return "Authentication rejected; obtain the current connection file from the target window."
    return "Bridge returned HTTP " + str(status) + "."


def fetch_json(connection, payload=None, timeout=PROBE_TIMEOUT):
    """Perform exactly one HTTP operation. No proxies, redirects or retries."""
    headers = {"Authorization": "Bearer " + connection.token, "Accept": "application/json"}
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        connection.url + ("/health" if payload is None else "/message"),
        data=body, headers=headers, method="GET" if payload is None else "POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.code
            raw = response.read(RESPONSE_LIMIT + 1)
        if len(raw) > RESPONSE_LIMIT:
            raise ClientError("Bridge response exceeds the 1 MiB limit.")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            if status >= 300:
                return {"status": "error", "error": http_hint(status)}, status
            raise ClientError("Bridge returned invalid JSON.") from exc
        return result, status
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise ClientError("Connection failed or timed out; the recorded bridge may have stopped.") from exc


def probe(connection, timeout=PROBE_TIMEOUT):
    try:
        health, status = fetch_json(connection, timeout=timeout)
        if status != 200:
            return Record(connection.path, "stale", http_hint(status), connection)
        if not isinstance(health, dict) or health.get("instanceId") != connection.instance_id:
            return Record(connection.path, "stale", "Instance ID mismatch; this endpoint is not the recorded window.", connection)
        return Record(connection.path, "live", "Authenticated health check passed.", connection, health)
    except ClientError as exc:
        return Record(connection.path, "stale", str(exc), connection)


def discover(directory=None, timeout=PROBE_TIMEOUT):
    directory = registry_directory(directory)
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ClientError("Cannot read the connection registry directory.") from exc
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise ClientError("Connection registry must be a directory, not a symlink or file.")
    try:
        paths = sorted(directory.glob("connection-*.json"))
    except OSError as exc:
        raise ClientError("Cannot list the connection registry directory.") from exc
    records, connections = [], []
    for path in paths:
        try:
            connections.append(read_connection(path))
        except ClientError as exc:
            records.append(Record(path, "invalid", str(exc)))
    if connections:
        with ThreadPoolExecutor(max_workers=min(8, len(connections))) as workers:
            records.extend(workers.map(lambda item: probe(item, timeout), connections))
    return sorted(records, key=lambda item: str(item.path))


def select_connection(window=None, workspace=None, connection_file=None, directory=None):
    if sum(item is not None for item in (window, workspace, connection_file)) != 1:
        raise ClientError("Choose exactly one window, workspace, or connection file.")
    if connection_file is not None:
        return read_connection(connection_file)
    if window is not None and not window.strip():
        raise ClientError("Window ID or prefix must not be empty.")
    records = discover(directory)
    matches = []
    for record in records:
        if record.connection is None:
            continue
        workspace_name = record.connection.metadata["workspace"]
        if valid_metadata_value("workspace", record.health.get("workspace")):
            workspace_name = record.health["workspace"]
        match = record.connection.instance_id.lower().startswith(window.lower()) if window is not None else workspace_name == workspace
        if match:
            matches.append(record)
    live = [record for record in matches if record.status == "live"]
    if len(live) > 1:
        identities = ", ".join(record.connection.instance_id for record in live)
        raise ClientError("Selection is ambiguous among live windows: " + identities + ". Use a complete --window ID or --connection path.")
    if not live:
        detail = " Matching connection records are stale." if matches else ""
        raise ClientError("No live window matches the selection." + detail + " Run 'list --all' to inspect records.")
    return live[0].connection


def checked_health(connection, timeout=PROBE_TIMEOUT):
    """Recheck identity immediately before a send; never dispatch from a stale record."""
    record = probe(connection, timeout)
    if record.status != "live":
        raise ClientError("Selected connection is " + record.status + ": " + record.detail)
    return record.health
