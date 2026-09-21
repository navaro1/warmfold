"""Unit tests for warmfoldlib.channels and warmfoldlib.actions.

All subprocess calls go through fakes, so no tmux, zellij, wezterm, kitty,
or screen installation is needed. SequenceFake models a stateful screen:
captures return the current state and send commands advance it.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from warmfoldlib import actions, channels  # noqa: E402

RULE = "─" * 40
MARKER = "❯ "


def screen(lines):
    return "\n".join(lines)


def framed_box(content=""):
    """A framed prompt box whose input line holds content (or is empty)."""
    return screen(["claude-code v2.1.278", "", RULE, MARKER + content, RULE, ""])


PROMPT_SCREEN = framed_box()  # idle box, empty input
EMPTY_BOX = framed_box()
TYPED_SCREEN = framed_box("/compact")
CLEAN_BOX = framed_box()

DIALOG_SCREEN = screen(
    [
        " Do you want to trust this folder?",
        "",
        RULE,
        MARKER + "No, exit",
        "  Yes, I trust this folder",
        RULE,
        " Enter to confirm · Esc to cancel",
    ]
)

NO_RULE_SCREEN = screen(
    [
        "some earlier output",
        MARKER,
        "more output",
    ]
)

MARKER_MISSING_SCREEN = screen(
    [
        RULE,
        "$ plain shell prompt",
        RULE,
    ]
)

ECHO_LINE = MARKER + "/compact"
ECHO_RESULT = "  ⎿  Error: No messages to compact"

# Post-Enter screen from a live run 2026-09-21: Claude Code echoes the
# executed command in the transcript as an UNFRAMED line above the new
# empty box. The echo is history, not the prompt.
COMPACTED_SCREEN = screen(
    [
        ECHO_LINE,
        ECHO_RESULT,
        RULE,
        MARKER,
        RULE,
    ]
)


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


CAPTURE_MARKERS = ("capture-pane", "dump-screen", "get-text", "hardcopy")
SEND_MARKERS = ("send-keys", "send-text", "write-chars", "write ", "stuff")


class FakeRun:
    """Replacement for subprocess.run. Records calls, serves canned data.

    Matching is by substring on the joined command. once() entries are
    consumed in order; always() entries stay.
    """

    def __init__(self):
        self.calls = []
        self._once = []
        self._always = []
        self._raise = []

    def __call__(self, cmd, input=None, capture_output=False, timeout=None):
        cmd = [str(part) for part in cmd]
        self.calls.append({"cmd": cmd, "input": input, "timeout": timeout})
        return self.serve(cmd)

    def serve(self, cmd):
        key = " ".join(cmd)
        for substr, exc in list(self._raise):
            if substr in key:
                raise exc
        for index, (substr, proc) in enumerate(list(self._once)):
            if substr in key:
                del self._once[index]
                return proc
        for substr, proc in self._always:
            if substr in key:
                return proc
        return FakeProc()

    def once(self, substr, returncode=0, stdout=b""):
        self._once.append((substr, FakeProc(returncode, stdout)))

    def always(self, substr, returncode=0, stdout=b""):
        self._always.append((substr, FakeProc(returncode, stdout)))

    def raise_when(self, substr, exc):
        self._raise.append((substr, exc))

    def cmd_strings(self):
        return [" ".join(call["cmd"]) for call in self.calls]

    def inputs(self):
        return [call["input"] for call in self.calls]


class SequenceFake(FakeRun):
    """Stateful screen: captures return states[stage]; sends advance stage.

    A send advances the stage BEFORE a configured exception fires, because
    a timed-out command may still have reached the terminal. zellij
    dump-screen and screen hardcopy writes go to the dump file.
    """

    def __init__(self, states):
        FakeRun.__init__(self)
        self.states = list(states)
        self.stage = 0

    def serve(self, cmd):
        key = " ".join(cmd)
        if any(marker in key for marker in CAPTURE_MARKERS):
            self.fire_raises(key)
            return self.capture_response(cmd)
        if any(marker in key for marker in SEND_MARKERS):
            self.advance()
            self.fire_raises(key)
            return FakeProc()
        return FakeRun.serve(self, cmd)

    def advance(self):
        if self.stage < len(self.states) - 1:
            self.stage += 1

    def fire_raises(self, key):
        for substr, exc in list(self._raise):
            if substr in key:
                raise exc

    def capture_response(self, cmd):
        state = self.states[min(self.stage, len(self.states) - 1)]
        if "--path" in cmd:
            self.write_dump(cmd[cmd.index("--path") + 1], state)
            return FakeProc()
        if "hardcopy" in cmd:
            self.write_dump(cmd[-1], state)
            return FakeProc()
        return FakeProc(0, state.encode("utf-8"))

    @staticmethod
    def write_dump(path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


class FileFake(FakeRun):
    """FakeRun that writes dump_text into the dump file the command names.

    Serves zellij dump-screen (--path <file>) and screen hardcopy (<file>).
    """

    dump_text = PROMPT_SCREEN

    def serve(self, cmd):
        key = " ".join(cmd)
        path = None
        if "--path" in cmd:
            path = cmd[cmd.index("--path") + 1]
        elif "hardcopy" in cmd:
            path = cmd[-1]
        if path is not None:
            SequenceFake.write_dump(path, self.dump_text)
        return FakeRun.serve(self, cmd)


ALL_ENV_KEYS = [
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
]


def clear_env(monkeypatch):
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def set_all_env(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,3245,0")
    monkeypatch.setenv("TMUX_PANE", "%2")
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "warmfold-test")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    monkeypatch.setenv("WEZTERM_PANE", "4")
    monkeypatch.setenv("KITTY_WINDOW_ID", "42")
    monkeypatch.setenv("KITTY_LISTEN_ON", "unix:/tmp/kitty-0")
    monkeypatch.setenv("STY", "warmfold-sty")
    monkeypatch.setenv("WINDOW", "0")


class Cfg:
    pane_markers = [MARKER]
    force_channel = ""


class StringMarkersCfg:
    pane_markers = MARKER + ",›"
    force_channel = ""


class EmptyCfg:
    pass


def patch_run(monkeypatch, fake):
    monkeypatch.setattr("subprocess.run", fake)


def patch_sleep(monkeypatch, record):
    monkeypatch.setattr(channels.time, "sleep", record.append)


def make_tmux(monkeypatch, fake):
    clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,3245,0")
    monkeypatch.setenv("TMUX_PANE", "%2")
    patch_run(monkeypatch, fake)
    return channels.TmuxChannel([MARKER])


# --- detection -------------------------------------------------------------


def test_detect_prefers_tmux_when_all_channels_verify(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    channel = channels.detect(Cfg())
    assert channel is not None
    assert channel.name == "tmux"


def test_detect_falls_back_to_zellij(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", returncode=1)
    fake.dump_text = PROMPT_SCREEN
    patch_run(monkeypatch, fake)
    channel = channels.detect(Cfg())
    assert channel is not None
    assert channel.name == "zellij"


def test_detect_falls_back_to_wezterm(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", returncode=1)
    fake.dump_text = DIALOG_SCREEN
    fake.always("get-text", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    channel = channels.detect(Cfg())
    assert channel is not None
    assert channel.name == "wezterm"


def test_detect_returns_none_when_every_channel_fails(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", returncode=1)
    fake.dump_text = DIALOG_SCREEN
    fake.always("get-text", returncode=1)
    patch_run(monkeypatch, fake)
    assert channels.detect(Cfg()) is None


def test_detect_without_env_vars_runs_no_command(monkeypatch):
    clear_env(monkeypatch)
    fake = FileFake()
    patch_run(monkeypatch, fake)
    assert channels.detect(Cfg()) is None
    assert fake.calls == []


def test_detect_force_none_disables_all(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    cfg = Cfg()
    cfg.force_channel = "none"
    assert channels.detect(cfg) is None
    assert fake.calls == []


def test_detect_force_name_restricts_to_that_channel(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", returncode=1)
    fake.dump_text = PROMPT_SCREEN
    patch_run(monkeypatch, fake)
    cfg = Cfg()
    cfg.force_channel = "zellij"
    channel = channels.detect(cfg)
    assert channel is not None
    assert channel.name == "zellij"
    assert not any("capture-pane" in cmd for cmd in fake.cmd_strings())


def test_detect_force_unknown_name_behaves_like_auto(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    cfg = Cfg()
    cfg.force_channel = "no-such-channel"
    channel = channels.detect(cfg)
    assert channel is not None
    assert channel.name == "tmux"


def test_detect_works_with_empty_cfg_object(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    channel = channels.detect(EmptyCfg())
    assert channel is not None
    assert channel.name == "tmux"


def test_detect_honors_string_pane_markers(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,3245,0")
    monkeypatch.setenv("TMUX_PANE", "%2")
    # Prompt drawn with the U+203A marker, no U+276F anywhere.
    other = screen(["", RULE, "› ", RULE, ""])
    fake = FakeRun()
    fake.always("capture-pane", stdout=other.encode())
    patch_run(monkeypatch, fake)
    assert channels.detect(Cfg()) is None
    channel = channels.detect(StringMarkersCfg())
    assert channel is not None
    assert channel.name == "tmux"


def test_detect_accepts_mapping_cfg(monkeypatch):
    # WP1 passes the config dict, not an attribute object.
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    cfg = {"pane_markers": MARKER, "force_channel": ""}
    channel = channels.detect(cfg)
    assert channel is not None
    assert channel.name == "tmux"


def test_detect_mapping_cfg_force_none(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    assert channels.detect({"force_channel": "none"}) is None
    assert fake.calls == []


def test_detect_mapping_cfg_markers_change_fingerprint(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,3245,0")
    monkeypatch.setenv("TMUX_PANE", "%2")
    other = screen(["", RULE, "› ", RULE, ""])
    fake = FakeRun()
    fake.always("capture-pane", stdout=other.encode())
    patch_run(monkeypatch, fake)
    assert channels.detect({"pane_markers": MARKER}) is None
    channel = channels.detect({"pane_markers": "› "})
    assert channel is not None
    assert channel.name == "tmux"


def test_detect_passes_log_to_channel(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", returncode=1)
    fake.dump_text = PROMPT_SCREEN
    patch_run(monkeypatch, fake)
    lines = []
    channel = channels.detect(Cfg(), log=lines.append)
    assert channel is not None
    assert any("verify" in line for line in lines)


def test_detect_commands_carry_the_timeout(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = FileFake()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    patch_run(monkeypatch, fake)
    channels.detect(Cfg())
    assert fake.calls
    assert all(call["timeout"] == channels.TIMEOUT for call in fake.calls)
    assert all(call["timeout"] == 5.0 for call in fake.calls)


def test_detect_logs_skipped_channels_with_the_missing_var(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "warmfold-test")
    fake = FileFake()
    patch_run(monkeypatch, fake)
    lines = []
    assert channels.detect(Cfg(), log=lines.append) is None
    assert any(
        "zellij: skipped; missing env: ZELLIJ_PANE_ID" in line for line in lines
    )
    assert fake.calls == []


# --- verify / fingerprint --------------------------------------------------


def test_verify_positive(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    channel = make_tmux(monkeypatch, fake)
    assert channel.verify([MARKER]) is True
    assert channel.verify() is True


def test_verify_accepts_prompt_line_with_text(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", stdout=TYPED_SCREEN.encode())
    channel = make_tmux(monkeypatch, fake)
    assert channel.verify([MARKER]) is True


def test_verify_rejects_dialog(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", stdout=DIALOG_SCREEN.encode())
    channel = make_tmux(monkeypatch, fake)
    assert channel.verify([MARKER]) is False


def test_verify_rejects_missing_rule_lines(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", stdout=NO_RULE_SCREEN.encode())
    channel = make_tmux(monkeypatch, fake)
    assert channel.verify([MARKER]) is False


def test_verify_rejects_marker_missing(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", stdout=MARKER_MISSING_SCREEN.encode())
    channel = make_tmux(monkeypatch, fake)
    assert channel.verify([MARKER]) is False


def test_verify_rejects_failed_capture(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", returncode=1)
    channel = make_tmux(monkeypatch, fake)
    assert channel.verify([MARKER]) is False


def test_fingerprint_rule_distance_window():
    far = screen([RULE, "line1", "line2", "line3", MARKER, "after1", RULE])
    assert channels.fingerprint_ok(far, [MARKER]) is False
    near = screen([RULE, "line1", "line2", MARKER, "after1", RULE])
    assert channels.fingerprint_ok(near, [MARKER]) is True


def test_fingerprint_scans_the_whole_capture():
    deep = screen(["Enter to confirm"] + ["filler"] * 30 + [RULE, MARKER, RULE])
    assert channels.fingerprint_ok(deep, [MARKER]) is False


def test_fingerprint_short_rule_run_does_not_count():
    short = screen(["─" * 9, MARKER, RULE])
    assert channels.fingerprint_ok(short, [MARKER]) is False


def test_fingerprint_requires_the_box_in_the_bottom_window():
    top = screen([RULE, MARKER, RULE] + ["filler line %d" % i for i in range(20)])
    assert channels.fingerprint_ok(top, [MARKER]) is False
    lines = ["filler line %d" % i for i in range(20)] + [RULE, MARKER, RULE]
    assert channels.fingerprint_ok(screen(lines), [MARKER]) is True


def test_fingerprint_bottom_window_boundary():
    # Filler after the box pushes the box out of the bottom window.
    # len(lines) - index(MARKER) == 2 + filler count.
    lines_in = [RULE, MARKER, RULE] + ["f%d" % i for i in range(13)]
    assert len(lines_in) - lines_in.index(MARKER) == 15  # inclusive edge
    assert channels.fingerprint_ok(screen(lines_in), [MARKER]) is True
    lines_out = [RULE, MARKER, RULE] + ["f%d" % i for i in range(14)]
    assert len(lines_out) - lines_out.index(MARKER) == 16
    assert channels.fingerprint_ok(screen(lines_out), [MARKER]) is False


def test_fingerprint_uses_the_last_box():
    two = screen([RULE, MARKER, RULE] + ["filler"] * 20 + [RULE, MARKER, RULE])
    assert channels.fingerprint_ok(two, [MARKER]) is True
    # The last framed box sits high; nothing valid remains in the window.
    stale = screen([RULE, MARKER, RULE] + ["vim output %d" % i for i in range(20)])
    assert channels.fingerprint_ok(stale, [MARKER]) is False


def test_fingerprint_rejects_dialog_after_the_box():
    after = screen([RULE, MARKER, RULE, "", "Enter to confirm"])
    assert channels.fingerprint_ok(after, [MARKER]) is False


# --- inject: staged sequence -----------------------------------------------


def test_inject_success_tmux(monkeypatch):
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    channel = make_tmux(monkeypatch, fake)
    sleeps = []
    patch_sleep(monkeypatch, sleeps)
    assert channel.inject_compact() is True
    assert fake.cmd_strings() == [
        "tmux capture-pane -p -t %2",  # verify
        "tmux send-keys -t %2 C-u C-u C-k",
        "tmux capture-pane -p -t %2",  # prompt must be empty
        "tmux send-keys -t %2 -l /compact",
        "tmux capture-pane -p -t %2",  # prompt must hold /compact
        "tmux send-keys -t %2 Enter",
        "tmux capture-pane -p -t %2",  # /compact must be gone
    ]
    assert sleeps == [0.5, 3.0]


def test_inject_sends_nothing_when_verify_fails(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", stdout=DIALOG_SCREEN.encode())
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    assert all("send-keys" not in cmd for cmd in fake.cmd_strings())
    assert len(fake.calls) == 1


def test_inject_aborts_when_dialog_appears_after_clear(monkeypatch):
    fake = SequenceFake([PROMPT_SCREEN, DIALOG_SCREEN, TYPED_SCREEN, CLEAN_BOX])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    sends = [c for c in fake.cmd_strings() if "send-keys" in c]
    assert sends == ["tmux send-keys -t %2 C-u C-u C-k"]


def test_inject_aborts_when_dialog_appears_after_typing(monkeypatch):
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, DIALOG_SCREEN, CLEAN_BOX])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    sends = [c for c in fake.cmd_strings() if "send-keys" in c]
    assert "Enter" not in " ".join(sends)
    assert sends[-1] == "tmux send-keys -t %2 -l /compact"


def test_inject_fails_when_text_remains_after_enter(monkeypatch):
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, TYPED_SCREEN])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False


def test_inject_success_when_echo_line_remains(monkeypatch):
    # Live bug regression: the transcript echo "❯ /compact" is unframed
    # history. Only the last framed box decides success.
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, COMPACTED_SCREEN])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is True


def test_inject_success_on_compacting_output(monkeypatch):
    busy = screen(["Compacting conversation…", "", RULE, MARKER, RULE])
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, busy])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is True


def test_inject_success_without_any_box_when_compacting(monkeypatch):
    # Compaction output can scroll the prompt box out of the pane.
    spinner = screen(["Compacting conversation…", "tokens: 12.3k"])
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, spinner])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is True


def test_inject_fails_when_last_box_holds_other_text(monkeypatch):
    other = framed_box("ls")
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, other])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False


def test_gates_ignore_unframed_echo_lines(monkeypatch):
    # An old unframed echo sits above the box during both gates.
    echo_empty = screen([ECHO_LINE, ECHO_RESULT, "", RULE, MARKER, RULE])
    echo_typed = screen([ECHO_LINE, ECHO_RESULT, "", RULE, ECHO_LINE, RULE])
    fake = SequenceFake([PROMPT_SCREEN, echo_empty, echo_typed, CLEAN_BOX])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is True
    # All four captures ran; every send is in place.
    assert len(fake.calls) == 7


def test_post_enter_check_unit():
    assert channels._post_enter_success(COMPACTED_SCREEN, [MARKER]) is True
    assert channels._post_enter_success(COMPACTED_SCREEN, MARKER + ",›") is True
    # An echo line alone is no proof: no framed box, no compaction output.
    echo_only = screen([ECHO_LINE, ECHO_RESULT])
    assert channels._post_enter_success(echo_only, [MARKER]) is False
    assert channels._post_enter_success("", [MARKER]) is False
    assert channels._post_enter_success(None, [MARKER]) is False


def test_inject_requires_empty_prompt_after_clear(monkeypatch):
    # The clear keys did not clear: text still sits in the prompt.
    fake = SequenceFake([TYPED_SCREEN, TYPED_SCREEN, TYPED_SCREEN, CLEAN_BOX])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    sends = [c for c in fake.cmd_strings() if "send-keys" in c]
    assert "Enter" not in " ".join(sends)


def test_inject_requires_compact_in_prompt_before_enter(monkeypatch):
    # Typing was lost: the prompt is still empty, so Enter must not fire.
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, EMPTY_BOX, CLEAN_BOX])
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    assert not any("Enter" in c for c in fake.cmd_strings())


def test_inject_fails_when_clear_keys_fail(monkeypatch):
    fake = FakeRun()
    fake.always("capture-pane", stdout=PROMPT_SCREEN.encode())
    fake.raise_when("send-keys", RuntimeError("boom"))
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    assert any("C-u" in cmd for cmd in fake.cmd_strings())
    assert not any("-l /compact" in cmd for cmd in fake.cmd_strings())


def test_inject_fails_when_post_enter_capture_fails(monkeypatch):
    fake = FakeRun()
    fake.once("capture-pane", stdout=PROMPT_SCREEN.encode())  # verify
    fake.once("capture-pane", stdout=EMPTY_BOX.encode())  # after clear
    fake.once("capture-pane", stdout=TYPED_SCREEN.encode())  # after typing
    fake.once("capture-pane", returncode=1)  # post-enter: fails
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False


# --- inject: send timeouts leave the pane unknown ---------------------------


def test_timeout_on_clear_returns_false_and_skips_enter(monkeypatch):
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    fake.raise_when("C-u", subprocess.TimeoutExpired(cmd="tmux", timeout=5))
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    assert not any("Enter" in c for c in fake.cmd_strings())


def test_timeout_on_type_recovers_with_one_clear(monkeypatch):
    # The type command timed out, but the text landed: /compact sits in the
    # prompt. The channel clears once and never sends Enter.
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    fake.raise_when("-l /compact", subprocess.TimeoutExpired(cmd="tmux", timeout=5))
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    clears = [c for c in fake.cmd_strings() if "C-u" in c]
    assert len(clears) == 2  # the staged clear plus the recovery clear
    assert not any("Enter" in c for c in fake.cmd_strings())


def test_timeout_on_type_with_clean_prompt_skips_recovery_clear(monkeypatch):
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    fake.raise_when("-l /compact", subprocess.TimeoutExpired(cmd="tmux", timeout=5))
    # Force the recapture to see the still-empty box: nothing landed.
    fake.states[2] = EMPTY_BOX
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False
    assert len([c for c in fake.cmd_strings() if "C-u" in c]) == 1
    assert not any("Enter" in c for c in fake.cmd_strings())


def test_timeout_on_enter_returns_false(monkeypatch):
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    fake.raise_when("Enter", subprocess.TimeoutExpired(cmd="tmux", timeout=5))
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False


# --- per-channel command shapes ---------------------------------------------


def test_zellij_command_shapes(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "warmfold-test")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    patch_run(monkeypatch, fake)
    channel = channels.ZellijChannel([MARKER])
    sleeps = []
    patch_sleep(monkeypatch, sleeps)
    assert channel.inject_compact() is True
    strings = fake.cmd_strings()
    assert any(
        cmd.startswith("zellij --session warmfold-test action dump-screen --path ")
        and cmd.endswith("--pane-id 3")
        for cmd in strings
    )
    assert "zellij --session warmfold-test action write --pane-id 3 21 21 11" in strings
    assert (
        "zellij --session warmfold-test action write-chars --pane-id 3 /compact"
        in strings
    )
    assert "zellij --session warmfold-test action write --pane-id 3 13" in strings
    # The dump temp file was removed.
    for call in fake.calls:
        if "dump-screen" in call["cmd"]:
            path = call["cmd"][call["cmd"].index("--path") + 1]
            assert not os.path.exists(path)


def test_zellij_verify_refuses_dialog_in_target_pane(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "warmfold-test")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    fake = SequenceFake([DIALOG_SCREEN])
    patch_run(monkeypatch, fake)
    channel = channels.ZellijChannel([MARKER])
    patch_sleep(monkeypatch, [])
    assert channel.verify([MARKER]) is False
    assert channel.inject_compact() is False
    assert not any("write" in cmd for cmd in fake.cmd_strings())


def test_zellij_requires_pane_id(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "warmfold-test")
    assert channels.ZellijChannel.available() is False
    assert channels.ZellijChannel.missing_env() == ["ZELLIJ_PANE_ID"]


def test_wezterm_command_shapes(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("WEZTERM_PANE", "4")
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    patch_run(monkeypatch, fake)
    channel = channels.WeztermChannel([MARKER])
    patch_sleep(monkeypatch, [])
    assert channel.capture() == PROMPT_SCREEN
    assert channel.inject_compact() is True
    assert "wezterm cli get-text --pane-id 4" in fake.cmd_strings()
    sends = [call for call in fake.calls if "send-text" in call["cmd"]]
    assert [call["input"] for call in sends] == [
        b"\x15\x15\x0b",
        b"/compact",
        b"\r",
    ]
    for call in sends:
        assert "--no-paste" in call["cmd"]
        assert "--pane-id" in call["cmd"]


def test_kitty_command_shapes(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("KITTY_WINDOW_ID", "42")
    monkeypatch.setenv("KITTY_LISTEN_ON", "unix:/tmp/kitty-0")
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    patch_run(monkeypatch, fake)
    channel = channels.KittyChannel([MARKER])
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is True
    strings = fake.cmd_strings()
    assert "kitten @ --to unix:/tmp/kitty-0 get-text --match id:42" in strings
    assert (
        "kitten @ --to unix:/tmp/kitty-0 send-text --match id:42 \x15\x15\x0b" in strings
    )
    assert "kitten @ --to unix:/tmp/kitty-0 send-text --match id:42 /compact" in strings
    assert "kitten @ --to unix:/tmp/kitty-0 send-text --match id:42 \r" in strings


def test_screen_command_shapes(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("STY", "warmfold-sty")
    monkeypatch.setenv("WINDOW", "0")
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    patch_run(monkeypatch, fake)
    channel = channels.ScreenChannel([MARKER])
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is True
    strings = fake.cmd_strings()
    assert any(
        cmd.startswith("screen -S warmfold-sty -p 0 -X hardcopy ") for cmd in strings
    )
    assert "screen -S warmfold-sty -p 0 -X stuff \x15\x15\x0b" in strings
    assert "screen -S warmfold-sty -p 0 -X stuff /compact" in strings
    assert "screen -S warmfold-sty -p 0 -X stuff \r" in strings
    for call in fake.calls:
        if "hardcopy" in call["cmd"]:
            path = call["cmd"][-1]
            assert not os.path.exists(path)


def test_screen_requires_window(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("STY", "warmfold-sty")
    assert channels.ScreenChannel.available() is False
    assert channels.ScreenChannel.missing_env() == ["WINDOW"]


# --- failures, timeouts, and the no-raise rule -------------------------------


def test_capture_timeout_returns_none(monkeypatch):
    fake = FakeRun()
    fake.raise_when("capture-pane", subprocess.TimeoutExpired(cmd="tmux", timeout=5))
    channel = make_tmux(monkeypatch, fake)
    assert channel.capture() is None
    assert channel.verify([MARKER]) is False


def test_capture_missing_binary_returns_none(monkeypatch):
    fake = FakeRun()
    fake.raise_when("capture-pane", FileNotFoundError())
    channel = make_tmux(monkeypatch, fake)
    assert channel.capture() is None


def test_detect_returns_none_when_capture_raises(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,3245,0")
    monkeypatch.setenv("TMUX_PANE", "%2")
    fake = FakeRun()
    fake.raise_when("capture-pane", subprocess.TimeoutExpired(cmd="tmux", timeout=5))
    patch_run(monkeypatch, fake)
    assert channels.detect(Cfg()) is None


def test_inject_never_raises_on_any_failure(monkeypatch):
    fake = FakeRun()
    fake.raise_when("capture-pane", subprocess.TimeoutExpired(cmd="tmux", timeout=5))
    fake.raise_when("send-keys", OSError("gone"))
    channel = make_tmux(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert channel.inject_compact() is False


def test_dump_file_read_failure_returns_none(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "warmfold-test")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    fake = FakeRun()
    # The fake exits 0 but never writes the dump file.
    patch_run(monkeypatch, fake)
    channel = channels.ZellijChannel([MARKER])
    assert channel.capture() is None


def test_broken_log_callable_never_raises(monkeypatch):
    def boom(message):
        raise RuntimeError("log is broken")

    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    channel = make_tmux(monkeypatch, fake)
    channel.log = boom
    patch_sleep(monkeypatch, [])
    assert channel.verify() is True
    assert channel.inject_compact() is True


def test_detect_never_raises_with_evil_cfg(monkeypatch):
    clear_env(monkeypatch)
    fake = FileFake()
    patch_run(monkeypatch, fake)

    class EvilCfg:
        @property
        def pane_markers(self):
            raise ValueError("boom")

        def get(self, key, default=None):
            raise RuntimeError("boom")

    assert channels.detect(EvilCfg()) is None
    assert actions.inject_compact(EvilCfg(), None) is False


# --- actions -----------------------------------------------------------------


def test_actions_inject_compact_success(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    patch_run(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    lines = []
    assert actions.inject_compact(Cfg(), lines.append) is True
    assert any("/compact sent" in line for line in lines)


def test_actions_inject_compact_without_channel(monkeypatch):
    clear_env(monkeypatch)
    fake = FileFake()
    patch_run(monkeypatch, fake)
    lines = []
    assert actions.inject_compact(Cfg(), lines.append) is False
    assert any("no verified channel" in line for line in lines)
    assert fake.calls == []


def test_actions_inject_compact_accepts_none_log(monkeypatch):
    clear_env(monkeypatch)
    set_all_env(monkeypatch)
    fake = SequenceFake([PROMPT_SCREEN, EMPTY_BOX, TYPED_SCREEN, CLEAN_BOX])
    patch_run(monkeypatch, fake)
    patch_sleep(monkeypatch, [])
    assert actions.inject_compact(Cfg(), None) is True


# --- misc --------------------------------------------------------------------


def test_normalize_markers_variants():
    assert channels._normalize_markers(None) == ["❯"]
    assert channels._normalize_markers("") == ["❯"]
    assert channels._normalize_markers("❯ , › ") == ["❯", "›"]
    assert channels._normalize_markers(["❯ ", "›"]) == ["❯", "›"]
    assert channels._normalize_markers([" ", "   "]) == ["❯"]
    assert channels._normalize_markers(",") == ["❯"]


def test_channel_instances_expose_the_interface():
    for cls in channels.CHANNEL_ORDER:
        channel = cls()
        assert isinstance(channel.name, str)
        assert callable(channel.capture)
        assert callable(channel.verify)
        assert callable(channel.inject_compact)
        assert callable(channel.log)


@pytest.mark.parametrize(
    "cls,keys",
    [
        (channels.TmuxChannel, ["TMUX", "TMUX_PANE"]),
        (channels.ZellijChannel, ["ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"]),
        (channels.WeztermChannel, ["WEZTERM_PANE"]),
        (channels.KittyChannel, ["KITTY_WINDOW_ID", "KITTY_LISTEN_ON"]),
        (channels.ScreenChannel, ["STY", "WINDOW"]),
    ],
)
def test_env_gating_per_channel(monkeypatch, cls, keys):
    clear_env(monkeypatch)
    assert cls.available() is False
    for key in keys:
        monkeypatch.setenv(key, "x")
    assert cls.available() is True
