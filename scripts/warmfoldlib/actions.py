"""High-level actions built on the channels package."""

from __future__ import annotations

from . import channels


def inject_compact(cfg, log) -> bool:
    """Detect a verified channel and type /compact into the session pane.

    cfg is any object with pane_markers and force_channel, or a mapping
    with the same keys. log is a callable that takes one string; each step
    logs through it, and a broken log never raises. Returns True only when
    every staged check passed: the last prompt box held /compact before
    Enter, and the last box is empty after Enter, or the screen shows
    compaction output. The unframed echo of the typed command is history
    and does not count. Returns False when no channel verifies, when a
    check fails, or when a send times out. Never raises.
    """
    try:
        channel = channels.detect(cfg, log=log)
        if channel is None:
            channels._safe_log(log, "actions: no verified channel; /compact not sent")
            return False
        return channel.inject_compact()
    except Exception as exc:
        channels._safe_log(log, f"actions: inject_compact failed: {exc}")
        return False
