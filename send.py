#!/usr/bin/env python3
"""List bridge windows or send a message to an explicitly selected VS Code window."""

import argparse
import json
import math
from pathlib import Path
import sys

from connections import (
    ClientError, PROBE_TIMEOUT, checked_health, discover, fetch_json,
    http_hint, read_connection, redact, select_connection, validate_url,
)


def load_connection(path):
    """Compatibility helper for callers of the original client module."""
    connection = read_connection(path)
    return connection.url, connection.token


def quoted(value):
    """Escape newlines, terminal controls and Unicode direction controls."""
    return json.dumps(str(value), ensure_ascii=True)


def show_list(records, include_all=False, as_json=False):
    rows = [record.public() for record in records if include_all or record.status == "live"]
    if as_json:
        print(json.dumps(rows, ensure_ascii=True, indent=2))
        return
    if not rows:
        print("No connection records found." if include_all else "No live bridge windows found. Run 'list --all' to include stale or invalid records.")
        return
    for row in rows:
        print(quoted(row.get("instanceId", "unknown")) + "  " + quoted(row.get("workspace", "unknown")) + "  [" + row["status"] + "]")
        print("  connection: " + quoted(row["connectionFile"]))
        if row.get("folders"):
            print("  folders: " + ", ".join(quoted(folder) for folder in row["folders"]))
        if row.get("vscodeVersion"):
            print("  VS Code: " + quoted(row["vscodeVersion"]) + "  bridge: " + quoted(row.get("bridgeVersion", "unknown")))
        if row["status"] != "live":
            print("  detail: " + quoted(row["detail"]))


def add_selector(parser):
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--window", help="Full instance ID or a unique prefix among live windows")
    selector.add_argument("--workspace", help="Exact workspace name, unique among live windows")
    selector.add_argument("--connection", type=Path, help="Exact connection JSON file")
    parser.add_argument("--directory", type=Path, help="Override the registry directory for discovery")


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list", help="List live windows and their connection files")
    listing.add_argument("--json", action="store_true", help="Print a token-free JSON array")
    listing.add_argument("--all", action="store_true", help="Include stale and invalid records")
    listing.add_argument("--directory", type=Path, help="Override the registry directory")
    listing.add_argument("--timeout", type=float, default=PROBE_TIMEOUT, help="Health probe timeout in seconds (default: 1)")
    health = subparsers.add_parser("health", help="Check one explicitly selected window without sending")
    add_selector(health)
    health.add_argument("--timeout", type=float, default=PROBE_TIMEOUT, help="Final health probe timeout in seconds (default: 1)")
    sending = subparsers.add_parser("send", help="Send one message to an explicitly selected window")
    add_selector(sending)
    sending.add_argument("message", nargs="?", help="Message text, quoted as one argument")
    source = sending.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="Read the message from a UTF-8 text file")
    source.add_argument("--stdin", action="store_true", help="Read the message from standard input")
    sending.add_argument("--new", action="store_true", help="Start a new chat; default: current chat")
    sending.add_argument("--behavior", choices=("queue", "steer", "interrupt"), default="queue")
    sending.add_argument("--composer", action="store_true", help="Replace the composer draft instead of preserving it")
    sending.add_argument("--timeout", type=float, default=20, help="POST timeout in seconds (default: 20; never retries)")
    return parser


def normalize_legacy(argv):
    if not argv or argv[0] in {"list", "send", "health", "-h", "--help"}:
        return argv
    # Legacy: send.py --connection FILE MESSAGE / --health.
    if "--health" in argv:
        return ["health"] + [arg for arg in argv if arg != "--health"]
    return ["send"] + argv


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(normalize_legacy(list(sys.argv[1:] if argv is None else argv)))
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    try:
        if args.command == "list":
            show_list(discover(args.directory, args.timeout), args.all, args.json)
            return 0
        payload = None
        if args.command == "send":
            sources = sum((args.message is not None, args.file is not None, args.stdin))
            if sources != 1:
                parser.error("provide exactly one message source: quoted text, --file or --stdin")
            message = args.file.expanduser().read_text(encoding="utf-8") if args.file else (
                sys.stdin.read() if args.stdin else args.message
            )
            if not message.strip():
                raise ClientError("Message must not be empty or whitespace only.")
            payload = {"message": message, "target": "new" if args.new else "current",
                       "behavior": args.behavior, "inputMode": "composer" if args.composer else "preserve"}
        connection = select_connection(args.window, args.workspace, args.connection, args.directory)
        health = checked_health(connection, args.timeout if args.command == "health" else PROBE_TIMEOUT)
        if args.command == "health":
            print(json.dumps(redact(health, connection.token), ensure_ascii=True, indent=2))
            return 0
        try:
            result, status = fetch_json(connection, payload, args.timeout)
        except ClientError as exc:
            raise ClientError(str(exc) + " Delivery is unconfirmed. Inspect the target chat before resending; no retry was attempted.") from exc
        print(json.dumps(redact(result, connection.token), ensure_ascii=True, indent=2))
        if not 200 <= status < 300:
            print("Error: " + http_hint(status) + " No retry was attempted.", file=sys.stderr)
            return 1
        return 0
    except (ClientError, OSError, UnicodeError) as exc:
        message = str(exc)
        if "connection" in locals():
            message = redact(message, connection.token)
        print("Error: " + quoted(message), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
