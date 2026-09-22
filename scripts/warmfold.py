#!/usr/bin/env python3
"""warmfold hook entry point.

Usage: python3 warmfold.py <event>

Events: watch, prompt, session-start, session-end, notification,
pre-compact, post-compact, stop-failure, status, savings, t3-setup, t3-status.

The hook payload arrives as JSON on stdin (it may be empty for a manual
``status`` or ``savings`` run). Hook stdout is either empty, one JSON
object, or (for the manual ``status`` and ``savings`` events) a plain-text
report. Exit code 2 is reserved for the deliberate wake; every internal
error logs and exits 0, and an unknown or missing event prints its
diagnostic to stderr and exits 0.
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from warmfoldlib import config  # noqa: E402
from warmfoldlib import events  # noqa: E402
from warmfoldlib import log  # noqa: E402
from warmfoldlib import t3_auth  # noqa: E402

USAGE = (
    "usage: warmfold.py <event>\n"
    "events: watch, prompt, session-start, session-end, notification, "
    "pre-compact, post-compact, stop-failure, status, savings, t3-setup, t3-status\n"
)


def _read_payload(stream):
    try:
        data = stream.read()
    except Exception:
        return {}
    if not data or not data.strip():
        return {}
    try:
        obj = json.loads(data)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _run(event, payload):
    handler = EVENTS.get(event)
    if handler is None and event not in ("t3-setup", "t3-status"):
        sys.stderr.write("warmfold: unknown event %r\n%s" % (event, USAGE))
        return 0
    try:
        cfg = config.load()
    except Exception:
        cfg = dict(config.DEFAULTS)
        cfg["data_dir"] = config.default_data_dir()
    if event == "t3-setup":
        try:
            report = t3_auth.setup_report(cfg)
            output, code = report, 0 if report.get("status") == t3_auth.ACCEPTED else 1
        except Exception as exc:
            output, code = {"status": "rejected", "reason": type(exc).__name__}, 1
    elif event == "t3-status":
        try:
            output, code = t3_auth.status_report(cfg), 0
        except Exception as exc:
            output, code = {"status": "error", "reason": type(exc).__name__}, 0
    else:
        try:
            output, code = handler(payload, cfg)
        except Exception as exc:
            try:
                log.append(cfg["data_dir"], event, "handler error: %r" % (exc,))
            except Exception:
                pass
            return 0
    try:
        if isinstance(output, dict):
            sys.stdout.write(json.dumps(output) + "\n")
        elif isinstance(output, str) and output:
            sys.stdout.write(output if output.endswith("\n") else output + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return int(code) if code else 0


EVENTS = {
    "watch": events.watch,
    "prompt": events.prompt,
    "session-start": events.session_start,
    "session-end": events.session_end,
    "notification": events.notification,
    "pre-compact": events.pre_compact,
    "post-compact": events.post_compact,
    "stop-failure": events.stop_failure,
    "status": events.status,
    "savings": events.savings,
}


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        sys.stderr.write(USAGE)
        return 0
    event = argv[1]
    # Manual T3 enrollment/status are intentionally independent of hook
    # stdin. A caller may pipe arbitrary data while checking readiness, and
    # native setup must never treat that data as a hook payload.
    payload = {} if event in ("t3-setup", "t3-status") else _read_payload(sys.stdin)
    return _run(event, payload)


if __name__ == "__main__":
    sys.exit(main())
