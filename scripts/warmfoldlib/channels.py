"""Terminal multiplexer keystroke channels for warmfold.

A channel captures the pane text, fingerprints the Claude Code prompt box,
and types /compact only through a staged, verified sequence. Detection
order:

1. tmux     live-tested 2026-09-21 (tmux 3.2a, Claude Code 2.1.278).
2. zellij   flags confirmed against zellij 0.45.1 --help; live injection
            not yet tested.
3. wezterm  untested: implemented from the documented CLI only.
4. kitty    untested: implemented from the documented CLI only.
5. screen   untested: implemented from the documented CLI only.

Safety rules:
- The fingerprint requires the LAST framed prompt box, within the bottom 15
  lines of the capture, with no dialog string anywhere and none in the
  non-empty lines after that box.
- Zellij actions carry --pane-id from $ZELLIJ_PANE_ID. zellij 0.45.1
  accepts "terminal_1" or a bare number such as "3" (equivalent to
  terminal_3), so no prefix is needed. Without ZELLIJ_PANE_ID the channel
  is unavailable: zellij without --pane-id hits the focused pane, which
  can be anything.
- GNU screen carries -p "$WINDOW" on hardcopy and stuff. Without $WINDOW
  the channel is unavailable.
- inject_compact re-verifies between steps: the last prompt box empty after
  the clear keys, exactly /compact in it after typing, Enter only then.
  Success needs proof afterwards: the last framed box empty, or compaction
  output on screen. The unframed echo of the typed command is history.
- A send command that TIMES OUT leaves the pane state unknown: the code
  recaptures, sends the clear keys once when text sits in the prompt, and
  never sends Enter afterwards.
- No public entry point raises. Failures log and return None or False;
  log calls themselves are wrapped.

All subprocess calls use a 5 s timeout. Each channel instance holds a log
callable. The default is a no-op. The caller sets it, for example:
channels.detect(cfg, log=my_log).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

# Seconds for each subprocess.run call.
TIMEOUT = 5.0

# Default prompt marker of the Claude Code input box: U+276F then a space.
# Markers are compared stripped: a line "❯ " strips to "❯" and still matches.
DEFAULT_MARKERS = ["❯ "]

# A rule line holds a run of at least 10 U+2500 characters ("─").
RULE_RUN = "─" * 10

# The last framed prompt box must sit within the bottom 15 lines of the
# capture. The status line and footer may follow it.
BOTTOM_WINDOW = 15

# Lines in a capture that mean "this is a dialog, not the prompt box".
DIALOG_STRINGS = (
    "Enter to confirm",
    "Esc to cancel",
    "Do you want to",
    "Yes, I trust",
    "No, exit",
)

# Raw control bytes for byte-oriented channels: C-u, C-u, C-k clear the input.
CLEAR_KEYS = "\x15\x15\x0b"
ENTER_KEY = "\r"

COMPACT_COMMAND = "/compact"

# Screen text that proves compaction started or finished, matched without
# case. "Error: No messages to compact" contains neither word.
COMPACT_EVIDENCE = ("compacting", "compacted")

# Tri-state result of a keystroke command. UNKNOWN means the pane state is
# no longer provably known (timeout).
SEND_OK = "ok"
SEND_FAIL = "fail"
SEND_UNKNOWN = "unknown"


def _noop(message):
    """Default log callable."""
    pass


def _safe_log(log, message):
    """Call log(message). Never raises, even when log is None or broken."""
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def _normalize_markers(markers):
    """Return a clean, stripped list of markers. Fall back to the default.

    Accepts a list of strings or one comma-separated string, because the
    config stores pane_markers as a comma-separated string.
    """
    if markers is None:
        markers = list(DEFAULT_MARKERS)
    if isinstance(markers, str):
        parts = markers.split(",")
    else:
        try:
            parts = list(markers)
        except TypeError:
            parts = list(DEFAULT_MARKERS)
    cleaned = [str(part).strip() for part in parts]
    cleaned = [marker for marker in cleaned if marker]
    return cleaned if cleaned else _normalize_markers(None)


def _cfg_value(cfg, key, default=None):
    """Read key from cfg: attribute first, then mapping access.

    Callers pass either a simple object (attributes, per the WP2 brief) or
    the config dict from warmfoldlib.config (mapping access). Never raises.
    """
    if cfg is None:
        return default
    try:
        if hasattr(cfg, key):
            return getattr(cfg, key)
    except Exception:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def _run_cmd(cmd, log, input_bytes=None):
    """Run cmd with a timeout. Return the CompletedProcess, or None.

    Never raises. Logs the failure through log.
    """
    try:
        if input_bytes is None:
            return subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
        return subprocess.run(
            cmd, input=input_bytes, capture_output=True, timeout=TIMEOUT
        )
    except Exception as exc:  # TimeoutExpired, FileNotFoundError, OSError, ...
        first = cmd[0] if cmd else "?"
        _safe_log(log, f"command failed: {first}: {exc}")
        return None


def _send_cmd(cmd, log, input_bytes=None):
    """Run a keystroke command. Return SEND_OK, SEND_FAIL, or SEND_UNKNOWN.

    UNKNOWN is reserved for timeouts: the bytes may or may not have reached
    the terminal, so the caller must treat the pane state as unknown.
    """
    try:
        if input_bytes is None:
            proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
        else:
            proc = subprocess.run(
                cmd, input=input_bytes, capture_output=True, timeout=TIMEOUT
            )
    except subprocess.TimeoutExpired:
        first = cmd[0] if cmd else "?"
        _safe_log(log, f"command timed out: {first}")
        return SEND_UNKNOWN
    except Exception as exc:  # FileNotFoundError, OSError, ...
        first = cmd[0] if cmd else "?"
        _safe_log(log, f"command failed: {first}: {exc}")
        return SEND_FAIL
    if proc.returncode != 0:
        _safe_log(log, f"command exited {proc.returncode}: {cmd[0] if cmd else '?'}")
        return SEND_FAIL
    return SEND_OK


def _ok(proc):
    """Return True when proc exists and exited 0."""
    return proc is not None and proc.returncode == 0


def _capture_via_file(make_cmd, log):
    """Run a command that dumps the screen into a temp file. Return the text.

    make_cmd(path) builds the full command around the temp file path. The
    helper removes the file afterwards. Returns None on any failure,
    including a failed temp file creation.
    """
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="warmfold-", suffix=".dump")
        os.close(fd)
        try:
            os.unlink(path)  # let the dump tool create its own file
        except OSError:
            pass
        proc = _run_cmd(make_cmd(path), log)
        if proc is None:
            return None
        if proc.returncode != 0:
            _safe_log(log, f"dump command exited {proc.returncode}")
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception as exc:
        _safe_log(log, f"dump capture failed: {exc}")
        return None
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass


def _find_prompt_box(lines, norm):
    """Return the index of the LAST framed marker line, or -1.

    A framed marker line starts with one of the markers and has a rule line
    (a run of 10 or more "─") within the 3 lines above and within the 3
    lines below.
    """
    best = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not any(stripped.startswith(marker) for marker in norm):
            continue
        above = lines[max(0, i - 3):i]
        below = lines[i + 1:i + 4]
        if any(RULE_RUN in a for a in above) and any(RULE_RUN in b for b in below):
            best = i
    return best


def fingerprint_ok(text, markers):
    """Return True when text shows the Claude Code prompt box and no dialog.

    The rules:
    - A line whose stripped text starts with one of the markers exists,
      framed by rule lines as above.
    - The LAST such framed box must sit within the bottom 15 lines of the
      capture (the status line and footer may follow it).
    - No dialog string appears anywhere in the whole capture.
    - No dialog string appears in the non-empty lines after that box.
    """
    if not text:
        return False
    for dialog in DIALOG_STRINGS:
        if dialog in text:
            return False
    norm = _normalize_markers(markers)
    if not norm:
        return False
    lines = text.splitlines()
    index = _find_prompt_box(lines, norm)
    if index < 0:
        return False
    if index < len(lines) - BOTTOM_WINDOW:
        return False
    for line in lines[index + 1:]:
        if not line.strip():
            continue
        if any(dialog in line for dialog in DIALOG_STRINGS):
            return False
    return True


def _prompt_line_state(text, markers):
    """Classify the last framed prompt line.

    Returns "empty" (marker only), "compact" (exactly /compact typed),
    "other" (different text, or no framed box), or None when there is no
    text at all.
    """
    if not text:
        return None
    norm = _normalize_markers(markers)
    index = _find_prompt_box(text.splitlines(), norm)
    if index < 0:
        return None
    stripped = text.splitlines()[index].strip()
    for marker in norm:
        if stripped.startswith(marker):
            rest = stripped[len(marker):].strip()
            if rest == "":
                return "empty"
            if rest == COMPACT_COMMAND:
                return "compact"
            return "other"
    return "other"


def _post_enter_success(text, markers):
    """Return True when the pane proves that /compact ran.

    Proof is the LAST framed prompt box empty (marker only), or a screen
    that shows compaction output (a COMPACT_EVIDENCE word, any case). An
    unframed echo of the typed command, such as the transcript history
    line "❯ /compact" above the new box, is not proof and is ignored.
    """
    if not text:
        return False
    if _prompt_line_state(text, markers) == "empty":
        return True
    lowered = text.lower()
    return any(word in lowered for word in COMPACT_EVIDENCE)


class Channel:
    """Base channel. Subclasses provide _capture and the three key steps.

    Attributes:
        name: channel name, for example "tmux".
        log: callable that takes one string. Default: no-op. All internal
            log calls wrap it, so a broken log never raises.
        markers: normalized prompt markers used by verify() and inject_compact().
    """

    name = "channel"

    def __init__(self, markers=None):
        self.log = _noop
        self.markers = _normalize_markers(markers)

    def _log(self, message):
        _safe_log(self.log, message)

    @classmethod
    def missing_env(cls):
        """Env vars that are absent. Subclasses override."""
        return ["(no implementation)"]

    @classmethod
    def available(cls):
        return not cls.missing_env()

    # -- public entry points; none of them raises --

    def capture(self):
        """Return the pane text, or None on failure."""
        try:
            return self._capture()
        except Exception as exc:
            self._log(f"{self.name}: capture failed: {exc}")
            return None

    def verify(self, markers=None):
        """Capture fresh text and fingerprint it against the markers."""
        try:
            use = self.markers
            if markers:
                use = _normalize_markers(markers)
            text = self.capture()
            if text is None:
                self._log(f"{self.name}: verify: capture failed")
                return False
            result = fingerprint_ok(text, use)
            self._log(f"{self.name}: verify: {'ok' if result else 'failed'}")
            return result
        except Exception as exc:
            self._log(f"{self.name}: verify failed: {exc}")
            return False

    def inject_compact(self):
        """Type /compact through a staged, verified sequence. Returns bool.

        Steps, each verified before the next:
        1. verify the prompt box.
        2. clear keys; the last prompt box must then be empty.
        3. type /compact; the last prompt box must then hold exactly /compact.
        4. Enter; after 3 s the last prompt box must be empty, or the screen
           must show compaction output. The unframed transcript echo of the
           typed command is history and does not count.

        A timed-out step leaves the pane state unknown: the code recaptures,
        clears the prompt once when text sits in it, and never sends Enter.
        Nothing is typed when a check fails.
        """
        try:
            return self._inject_compact_impl()
        except Exception as exc:
            self._log(f"{self.name}: inject_compact failed: {exc}")
            return False

    def _inject_compact_impl(self):
        if not self.verify():
            self._log(f"{self.name}: fingerprint failed; nothing typed")
            return False

        result = self._clear_input()
        if result == SEND_UNKNOWN:
            return self._recover_after_unknown("clear keys")
        if result != SEND_OK:
            self._log(f"{self.name}: clear keys failed")
            return False
        if not self._require_prompt_state("empty"):
            return False

        result = self._type_compact()
        if result == SEND_UNKNOWN:
            return self._recover_after_unknown("typing /compact")
        if result != SEND_OK:
            self._log(f"{self.name}: typing /compact failed")
            return False
        time.sleep(0.5)
        if not self._require_prompt_state("compact"):
            return False

        result = self._press_enter()
        if result == SEND_UNKNOWN:
            return self._recover_after_unknown("Enter")
        if result != SEND_OK:
            self._log(f"{self.name}: Enter failed")
            return False
        time.sleep(3.0)

        text = self.capture()
        if text is None:
            self._log(f"{self.name}: post-inject capture failed")
            return False
        if not _post_enter_success(text, self.markers):
            self._log(
                f"{self.name}: last prompt box is not empty and no compaction "
                "output shows; /compact did not run"
            )
            return False
        self._log(f"{self.name}: /compact sent")
        return True

    def _require_prompt_state(self, expected):
        """Capture and require the last prompt box to hold the expected text.

        expected is "empty" (marker only) or "compact" (exactly /compact).
        The capture must also pass the full fingerprint (box, window, and
        no dialog strings).
        """
        text = self.capture()
        if text is None:
            self._log(f"{self.name}: intermediate capture failed")
            return False
        if not fingerprint_ok(text, self.markers):
            self._log(f"{self.name}: fingerprint failed after a keystroke")
            return False
        state = _prompt_line_state(text, self.markers)
        if state != expected:
            self._log(
                f"{self.name}: prompt holds {state!r}, expected {expected!r}"
            )
            return False
        return True

    def _recover_after_unknown(self, step):
        """A send timed out: the pane state is unknown. Never press Enter.

        Recapture. When /compact or other text sits in the prompt line,
        send the clear keys once to leave the pane clean. Returns False.
        """
        self._log(f"{self.name}: {step} timed out; pane state unknown")
        text = self.capture()
        state = _prompt_line_state(text, self.markers) if text is not None else None
        if state in ("compact", "other"):
            self._log(f"{self.name}: text sits in the prompt; clearing once")
            self._clear_input()
        return False

    # -- primitives to override --

    def _capture(self):
        """Return the pane text, or None on failure."""
        raise NotImplementedError

    def _clear_input(self):
        """Send the clear-keys sequence. Return a SEND_* tri-state."""
        raise NotImplementedError

    def _type_compact(self):
        """Type the literal /compact text. Return a SEND_* tri-state."""
        raise NotImplementedError

    def _press_enter(self):
        """Press Enter. Return a SEND_* tri-state."""
        raise NotImplementedError


class TmuxChannel(Channel):
    """tmux channel. Live-tested 2026-09-21 (tmux 3.2a, Claude Code 2.1.278)."""

    name = "tmux"

    def __init__(self, markers=None):
        Channel.__init__(self, markers)
        self.pane = os.environ.get("TMUX_PANE", "")

    @classmethod
    def missing_env(cls):
        return [key for key in ("TMUX", "TMUX_PANE") if not os.environ.get(key)]

    def _keys(self, *keys):
        cmd = ["tmux", "send-keys", "-t", self.pane]
        cmd.extend(keys)
        return _send_cmd(cmd, self.log)

    def _capture(self):
        proc = _run_cmd(["tmux", "capture-pane", "-p", "-t", self.pane], self.log)
        if not _ok(proc):
            return None
        return proc.stdout.decode("utf-8", "replace")

    def _clear_input(self):
        return self._keys("C-u", "C-u", "C-k")

    def _type_compact(self):
        return self._keys("-l", COMPACT_COMMAND)

    def _press_enter(self):
        return self._keys("Enter")


class ZellijChannel(Channel):
    """zellij channel. Flags confirmed against zellij 0.45.1 --help.

    Every action carries --pane-id from $ZELLIJ_PANE_ID. zellij 0.45.1
    accepts "terminal_1" or a bare number "3" (equivalent to terminal_3),
    so the env value passes through as-is, with no prefix needed. Without
    ZELLIJ_PANE_ID the channel is unavailable, because a zellij action
    without --pane-id hits the FOCUSED pane, which can show anything.
    """

    name = "zellij"

    def __init__(self, markers=None):
        Channel.__init__(self, markers)
        self.session = os.environ.get("ZELLIJ_SESSION_NAME", "")
        self.pane = os.environ.get("ZELLIJ_PANE_ID", "")

    @classmethod
    def missing_env(cls):
        return [
            key
            for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID")
            if not os.environ.get(key)
        ]

    def _action(self, *args):
        cmd = ["zellij", "--session", self.session, "action"]
        cmd.extend(args)
        return _send_cmd(cmd, self.log)

    def _capture(self):
        def make_cmd(path):
            return [
                "zellij",
                "--session",
                self.session,
                "action",
                "dump-screen",
                "--path",
                path,
                "--pane-id",
                self.pane,
            ]

        return _capture_via_file(make_cmd, self.log)

    def _clear_input(self):
        # Bytes 21 21 11 are C-u, C-u, C-k.
        return self._action("write", "--pane-id", self.pane, "21", "21", "11")

    def _type_compact(self):
        return self._action("write-chars", "--pane-id", self.pane, COMPACT_COMMAND)

    def _press_enter(self):
        # Byte 13 is Enter.
        return self._action("write", "--pane-id", self.pane, "13")


class WeztermChannel(Channel):
    """wezterm channel. Untested: implemented from the documented CLI only."""

    name = "wezterm"

    def __init__(self, markers=None):
        Channel.__init__(self, markers)
        self.pane = os.environ.get("WEZTERM_PANE", "")

    @classmethod
    def missing_env(cls):
        return [key for key in ("WEZTERM_PANE",) if not os.environ.get(key)]

    def _send(self, text):
        cmd = ["wezterm", "cli", "send-text", "--pane-id", self.pane, "--no-paste"]
        return _send_cmd(cmd, self.log, input_bytes=text.encode("utf-8"))

    def _capture(self):
        proc = _run_cmd(
            ["wezterm", "cli", "get-text", "--pane-id", self.pane], self.log
        )
        if not _ok(proc):
            return None
        return proc.stdout.decode("utf-8", "replace")

    def _clear_input(self):
        return self._send(CLEAR_KEYS)

    def _type_compact(self):
        return self._send(COMPACT_COMMAND)

    def _press_enter(self):
        return self._send(ENTER_KEY)


class KittyChannel(Channel):
    """kitty channel. Untested: implemented from the documented CLI only."""

    name = "kitty"

    def __init__(self, markers=None):
        Channel.__init__(self, markers)
        self.window = os.environ.get("KITTY_WINDOW_ID", "")
        self.listen = os.environ.get("KITTY_LISTEN_ON", "")

    @classmethod
    def missing_env(cls):
        return [
            key
            for key in ("KITTY_WINDOW_ID", "KITTY_LISTEN_ON")
            if not os.environ.get(key)
        ]

    def _send(self, text):
        cmd = [
            "kitten",
            "@",
            "--to",
            self.listen,
            "send-text",
            "--match",
            "id:" + self.window,
            text,
        ]
        return _send_cmd(cmd, self.log)

    def _capture(self):
        cmd = [
            "kitten",
            "@",
            "--to",
            self.listen,
            "get-text",
            "--match",
            "id:" + self.window,
        ]
        proc = _run_cmd(cmd, self.log)
        if not _ok(proc):
            return None
        return proc.stdout.decode("utf-8", "replace")

    def _clear_input(self):
        return self._send(CLEAR_KEYS)

    def _type_compact(self):
        return self._send(COMPACT_COMMAND)

    def _press_enter(self):
        return self._send(ENTER_KEY)


class ScreenChannel(Channel):
    """GNU screen channel. Untested: implemented from the documented CLI only.

    Every command carries -p "$WINDOW" so the action targets the window of
    the session instead of an arbitrary one. Without $WINDOW the channel is
    unavailable.
    """

    name = "screen"

    def __init__(self, markers=None):
        Channel.__init__(self, markers)
        self.session = os.environ.get("STY", "")
        self.window = os.environ.get("WINDOW", "")

    @classmethod
    def missing_env(cls):
        return [key for key in ("STY", "WINDOW") if not os.environ.get(key)]

    def _stuff(self, text):
        cmd = ["screen", "-S", self.session, "-p", self.window, "-X", "stuff", text]
        return _send_cmd(cmd, self.log)

    def _capture(self):
        def make_cmd(path):
            return [
                "screen",
                "-S",
                self.session,
                "-p",
                self.window,
                "-X",
                "hardcopy",
                path,
            ]

        return _capture_via_file(make_cmd, self.log)

    def _clear_input(self):
        return self._stuff(CLEAR_KEYS)

    def _type_compact(self):
        return self._stuff(COMPACT_COMMAND)

    def _press_enter(self):
        return self._stuff(ENTER_KEY)


# Detection order: first verified channel wins.
CHANNEL_ORDER = [TmuxChannel, ZellijChannel, WeztermChannel, KittyChannel, ScreenChannel]
CHANNELS_BY_NAME = {cls.name: cls for cls in CHANNEL_ORDER}


def detect(cfg, log=None):
    """Return the first verified channel, or None. Never raises.

    cfg is any object with pane_markers (a list of strings, or one
    comma-separated string) and force_channel, or a mapping with the same
    keys (the config dict from warmfoldlib.config). Both are read with
    attribute first, mapping fallback, and fall back to the defaults.
    force_channel == "none" disables all channels. A known channel name
    (for example "zellij") restricts detection to that one channel. Each
    candidate runs the fingerprint check; the first pass wins. Candidates
    with missing env vars are skipped and logged.
    """
    try:
        return _detect_impl(cfg, log)
    except Exception as exc:
        _safe_log(log, f"detect failed: {exc}")
        return None


def _markers_from_cfg(cfg):
    """Read pane_markers from cfg and normalize them. Never raises."""
    return _normalize_markers(_cfg_value(cfg, "pane_markers"))


def _detect_impl(cfg, log):
    markers = _markers_from_cfg(cfg)
    force = str(_cfg_value(cfg, "force_channel", "") or "").strip().lower()
    if force == "none":
        return None
    classes = CHANNEL_ORDER
    if force in CHANNELS_BY_NAME:
        classes = [CHANNELS_BY_NAME[force]]
    for cls in classes:
        missing = cls.missing_env()
        if missing:
            _safe_log(
                log, f"{cls.name}: skipped; missing env: {', '.join(missing)}"
            )
            continue
        channel = cls(markers)
        if log is not None:
            channel.log = log
        if channel.verify(markers):
            return channel
    return None
