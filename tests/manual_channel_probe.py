#!/usr/bin/env python3
"""Manual probe for the warmfold keystroke channels.

Run it inside the terminal you want to check:

    python3 tests/manual_channel_probe.py

It prints the channel environment, the detect() result, and the verify()
result for the current environment. It never types anything.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

from warmfoldlib import channels  # noqa: E402

ENV_KEYS = (
    "TMUX",
    "TMUX_PANE",
    "ZELLIJ",
    "ZELLIJ_SESSION_NAME",
    "ZELLIJ_PANE_ID",
    "WEZTERM_PANE",
    "KITTY_WINDOW_ID",
    "KITTY_LISTEN_ON",
    "STY",
    "WINDOW",
)


class Cfg:
    pane_markers = ["❯ "]
    force_channel = ""


def main():
    print("channel environment:")
    for key in ENV_KEYS:
        value = os.environ.get(key)
        if value is None:
            print("  %s: not set" % key)
        else:
            print("  %s: %r" % (key, value))
    lines = []
    channel = channels.detect(Cfg(), log=lines.append)
    for line in lines:
        print("log: %s" % line)
    if channel is None:
        print("detect: no verified channel")
        return 1
    print("detect: %s" % channel.name)
    print("verify: %s" % channel.verify(Cfg.pane_markers))
    text = channel.capture()
    if text is None:
        print("capture: failed")
        return 1
    all_lines = text.splitlines()
    print("capture: %d lines; last 5:" % len(all_lines))
    for line in all_lines[-5:]:
        print("  |%s" % line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
