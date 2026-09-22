#!/usr/bin/env bash
# lib.sh - helpers for the warmfold integration tests.
# run.sh sources this file. Do not execute it directly.
#
# Facts used here (verified in live probes; see DESIGN.md section 10):
#   tmux new-session -d -s NAME -x 120 -y 40 -c DIR "claude ..."  starts a session
#   a new directory shows a trust dialog; Down + Enter accepts it
#   tmux send-keys -t NAME -l "text" types text; Enter submits
#   tmux capture-pane -p -t NAME reads the screen
#   transcripts live in ~/.claude/projects/<cwd with / replaced by ->/<id>.jsonl
#   a manual compaction writes a JSON line with subtype compact_boundary and
#     compactMetadata.trigger "manual"
#   zellij 0.45.1: "zellij --session NAME --new-session-with-layout FILE" starts
#     a new named session from a layout file (--layout alone targets an existing
#     session). A layout pane block supports cwd. The pane process gets
#     ZELLIJ_SESSION_NAME, ZELLIJ_PANE_ID, and ZELLIJ (value "0"; presence is
#     what matters for channel detection).

# shellcheck shell=bash
# shellcheck disable=SC2034  # run.sh uses these after it sources this file
set -u

# --- output (defined first; the defaults below call die) ---------------------
log() { printf '%s\n' "$*"; }

die() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

clip() { # <text> -> first 240 chars, newlines become spaces
  printf '%s' "$1" | tr '\n' ' ' | cut -c1-240
}

RUN_ID="$$"

# --- defaults; every value accepts an env override ---------------------------
mkdir -p "${TMPDIR:-/tmp}" 2>/dev/null || true
ART="${ART:-$(mktemp -d "${TMPDIR:-/tmp}/warmfold-itest.XXXXXX")}"
mkdir -p "$ART" 2>/dev/null || true
ART="$(cd "$ART" 2>/dev/null && pwd)" || die "cannot use ART: $ART"
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MODEL="${MODEL:-claude-haiku-4-5}"
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/.claude/projects}"
ZELLIJ_BIN="${ZELLIJ_BIN:-$HOME/.local/bin/zellij}"
ZELLIJ_SESSION="${ZELLIJ_SESSION:-ccit-${RUN_ID}-zellij}"
TURN_TIMEOUT="${TURN_TIMEOUT:-180}"   # seconds for one claude turn
IDLE_TIMEOUT="${IDLE_TIMEOUT:-180}"   # seconds for one idle action, from the confirmed turn end
POLL="${POLL:-3}"                     # poll interval in seconds

PROMPT1='Say the word ready and stop.'
# PROMPT2 gains a random identifier word per case; see begin_case.
PROMPT2=""
PROMPT_GUARD='Count from 1 to 5, then stop.'
# PROMPT_RETURN is the first user return after a compaction; with
# WARMFOLD_FORCE_TTL_SECONDS=3600 it lands long before cold_at, so the
# ledger outcome is early (DESIGN.md section 12).
PROMPT_RETURN='Count from 1 to 3.'
STATUS_CMD='/warmfold:status'
SAVINGS_CMD='/warmfold:savings'

# every documented WARMFOLD_* key (DESIGN.md section 3); env_prefix unsets all
WARMFOLD_KEYS=(
  WARMFOLD_IDLE_MINUTES WARMFOLD_MIN_CONTEXT_TOKENS
  WARMFOLD_SAFETY_MARGIN_MINUTES WARMFOLD_MODE WARMFOLD_KEEPALIVE_HOURS
  WARMFOLD_TTL_5M_POLICY WARMFOLD_GUARD WARMFOLD_GUARD_MIN_USD
  WARMFOLD_GUARD_ACK_SECONDS WARMFOLD_POLL_SECONDS
  WARMFOLD_PANE_MARKERS WARMFOLD_DATA_DIR WARMFOLD_FORCE_TTL_SECONDS
  WARMFOLD_FORCE_CHANNEL
)
# every channel env var; the "none" cases unset all of them (DESIGN.md section 7)
CHANNEL_UNSETS=(-u TMUX -u TMUX_PANE -u ZELLIJ -u ZELLIJ_SESSION_NAME -u ZELLIJ_PANE_ID
  -u WEZTERM_PANE -u KITTY_WINDOW_ID -u KITTY_LISTEN_ON -u STY -u WINDOW)

# --- state shared between helpers and the case functions ---------------------
TMUX_SESSIONS=()
CURRENT_SESSION=""
CURRENT_CWD=""
CASE_DIR=""
CASE_NAME=""
CASE_START=0
CASE_STORY_WORD=""
PROMPT2=""
PROMPT_OFFSET=0
FAILURES=0
PANE_ARGS=()
NEW_SCREEN_LINES=""

# --- output ------------------------------------------------------------------
# check <label> <ok-0-or-1> [evidence...]
# Count one failed check. Evidence lines go to the log for the tester.
check() {
  local label="$1" ok="$2" e
  shift 2
  if [ "$ok" -eq 0 ]; then
    log "  ok   $label"
  else
    log "  FAIL $label"
    FAILURES=$((FAILURES + 1))
  fi
  for e in "$@"; do
    [ -n "$e" ] && log "       evidence: $(clip "$e")"
  done
  return 0
}

# --- screen helpers -----------------------------------------------------------
capture() { # <session> -> pane text on stdout
  tmux capture-pane -p -t "$1"
}

read_screen() { # <session> -> pane text, empty on error
  capture "$1" 2>/dev/null || true
}

save_screen() { # <session> <label> -> writes $CASE_DIR/screen-<label>.txt
  local s="$1" label="$2"
  [ -n "$CASE_DIR" ] || return 0
  capture "$s" > "$CASE_DIR/screen-${label}.txt" 2>/dev/null || true
}

pane_idle() { # <screen-text> -> 0 when a marker line framed by rule lines shows,
  # with no spinner, no "esc to interrupt", and no dialog (DESIGN.md sections 3, 7)
  python3 -c '
import re, sys
text = sys.argv[1]
lines = text.splitlines()
framed = False
for i, ln in enumerate(lines):
    if ln.strip().startswith("❯"):
        prev = lines[i - 1] if i > 0 else ""
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "──" in prev and "──" in nxt:
            framed = True
            break
if not framed:
    sys.exit(1)
low = text.lower()
for bad in ("esc to interrupt", "yes, i trust", "no, exit",
            "esc to cancel", "enter to confirm", "do you want"):
    if bad in low:
        sys.exit(1)
if re.search("[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", text):
    sys.exit(1)
sys.exit(0)
' "$1"
}

wait_pane_idle() { # <session> <timeout>
  local s="$1" timeout="$2"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if pane_idle "$(read_screen "$s")"; then
      save_screen "$s" "pane-idle" >/dev/null
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_screen_text() { # <session> <grep-ERE> <timeout>
  local s="$1" pat="$2" timeout="$3"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if printf '%s\n' "$(read_screen "$s")" | grep -qiE "$pat"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

screen_new_lines() { # <before-file> <after-file> -> lines that only the after file has
  python3 -c '
import sys
def load(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return set(f.read().splitlines())
    except OSError:
        return set()
for ln in sorted(load(sys.argv[2]) - load(sys.argv[1])):
    print(ln)
' "$1" "$2"
}

# --- transcript helpers --------------------------------------------------------
latest_transcript() { # <cwd> -> newest transcript path, or empty
  local key
  key="$(printf '%s' "$1" | sed 's|/|-|g')"
  # shellcheck disable=SC2012  # file names are uuids, ls -t is safe here
  ls -t "$PROJECTS_DIR/$key"/*.jsonl 2>/dev/null | head -n 1
}

transcript_size() { # <transcript> -> byte size, 0 when absent
  # BSD/macOS stat has no GNU -c%s.  Python is already a test requirement
  # and gives the same byte offset on both platforms.
  python3 -c 'import os, sys
try:
    print(os.path.getsize(sys.argv[1]))
except OSError:
    print(0)' "$1" 2>/dev/null || printf '0'
}

# transcript_turn_after <transcript> <byte-offset>
# 0 when a user record appears after the offset and a later assistant record
# carries message.usage. Parses JSON, never greps.
transcript_turn_after() {
  python3 -c '
import json, sys
path, off = sys.argv[1], int(sys.argv[2])
try:
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read().decode("utf-8", "replace")
except OSError:
    sys.exit(1)
saw_user = False
for line in data.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    t = d.get("type")
    if t == "user":
        saw_user = True
    elif t == "assistant" and saw_user:
        if (d.get("message") or {}).get("usage"):
            sys.exit(0)
sys.exit(1)
' "$1" "$2"
}

# transcript_has_no_records_after <transcript> <byte-offset>
# 0 when no user and no assistant record was appended after the offset.
transcript_has_no_records_after() {
  python3 -c '
import json, sys
path, off = sys.argv[1], int(sys.argv[2])
try:
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read().decode("utf-8", "replace")
except OSError:
    sys.exit(1)
for line in data.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") in ("user", "assistant"):
        sys.exit(1)
sys.exit(0)
' "$1" "$2"
}

# transcript_has_no_compact_after <transcript> <byte-offset>
# 0 when no system record with subtype compact_boundary was appended after
# the offset.
transcript_has_no_compact_after() {
  python3 -c '
import json, sys
path, off = sys.argv[1], int(sys.argv[2])
try:
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read().decode("utf-8", "replace")
except OSError:
    sys.exit(1)
for line in data.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
        sys.exit(1)
sys.exit(0)
' "$1" "$2"
}

# transcript_manual_compact_after <transcript> <byte-offset>
# Prints the first appended line with subtype compact_boundary and
# compactMetadata.trigger "manual"; exit 1 when none.
transcript_manual_compact_after() {
  python3 -c '
import json, sys
path, off = sys.argv[1], int(sys.argv[2])
try:
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read().decode("utf-8", "replace")
except OSError:
    sys.exit(1)
for line in data.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
        if (d.get("compactMetadata") or {}).get("trigger") == "manual":
            print(line[:240])
            sys.exit(0)
sys.exit(1)
' "$1" "$2"
}

# transcript_assistant_contains_after <transcript> <byte-offset> <needle>
# Prints the first appended assistant record whose message text holds needle.
transcript_assistant_contains_after() {
  python3 -c '
import json, sys
path, off, needle = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read().decode("utf-8", "replace")
except OSError:
    sys.exit(1)
for line in data.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") == "assistant":
        text = json.dumps(d.get("message", {}), ensure_ascii=False)
        if needle in text:
            print(line[:240])
            sys.exit(0)
sys.exit(1)
' "$1" "$2" "$3"
}

# savings_ledger_counts <ledger-file>
# Prints "<action-compact-count> <outcome-early-count>" for the savings
# ledger (DESIGN.md section 12); prints "0 0" on any problem.
savings_ledger_counts() {
  python3 -c '
import json, sys
a = o = 0
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "action" and d.get("action") == "compact":
                a += 1
            if d.get("type") == "outcome" and d.get("outcome") == "early":
                o += 1
except OSError:
    pass
print(a, o)
' "$1"
}

# --- state helpers ---------------------------------------------------------------
state_file() { # -> state json path for the current session, or empty
  local t sid
  t="$(latest_transcript "$CURRENT_CWD")"
  [ -n "$t" ] || return 0
  sid="$(basename "$t" .jsonl)"
  printf '%s\n' "$CASE_DIR/data/sessions/$sid.json"
}

json_field() { # <json-file> <key> -> value, or empty on any problem
  python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        v = json.load(f).get(sys.argv[2], "")
except Exception:
    v = ""
print("" if v is None else v)
' "$1" "$2"
}

wait_for_file() { # <path> <timeout>
  local path="$1" timeout="$2"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ -s "$path" ] && return 0
    sleep "$POLL"
  done
  [ -s "$path" ]
}

wait_for_state_phase() { # <wanted-phase> <timeout>
  local want="$1" timeout="$2"
  local deadline=$(( $(date +%s) + timeout ))
  local sf p
  while [ "$(date +%s)" -lt "$deadline" ]; do
    sf="$(state_file)"
    if [ -n "$sf" ] && [ -f "$sf" ]; then
      p="$(json_field "$sf" phase)"
      [ "$p" = "$want" ] && return 0
    fi
    sleep "$POLL"
  done
  return 1
}

# wait_for_compact_state <baseline-last_compact_at> <timeout>
# 0 when the phase reaches compact_sent or last_compact_at grows past baseline.
wait_for_compact_state() {
  local baseline="$1" timeout="$2"
  local deadline=$(( $(date +%s) + timeout ))
  local sf phase lastc
  while [ "$(date +%s)" -lt "$deadline" ]; do
    sf="$(state_file)"
    if [ -n "$sf" ] && [ -f "$sf" ]; then
      phase="$(json_field "$sf" phase)"
      lastc="$(json_field "$sf" last_compact_at)"
      if [ "$phase" = "compact_sent" ]; then
        return 0
      fi
      if [ -n "$lastc" ] && python3 -c 'import sys
try:
    sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)
except Exception:
    sys.exit(1)' "$lastc" "$baseline" 2>/dev/null; then
        return 0
      fi
    fi
    sleep "$POLL"
  done
  return 1
}

# --- input -----------------------------------------------------------------------
send_prompt() { # <session> <text>; records the transcript byte offset as baseline
  local s="$1" text="$2" t
  t="$(latest_transcript "$CURRENT_CWD")"
  PROMPT_OFFSET=0
  [ -n "$t" ] && PROMPT_OFFSET="$(transcript_size "$t")"
  tmux send-keys -t "$s" -l "$text"
  sleep 0.3
  tmux send-keys -t "$s" Enter
  log "  sent prompt: $text"
}

accept_trust() { # <session> [timeout] -> 0 when the framed idle prompt shows
  local s="$1" timeout="${2:-120}"
  local deadline=$(( $(date +%s) + timeout ))
  local screen
  while [ "$(date +%s)" -lt "$deadline" ]; do
    screen="$(read_screen "$s")"
    if printf '%s\n' "$screen" | grep -qE 'Yes, I trust|No, exit'; then
      log "  trust dialog found; sending Down Enter"
      tmux send-keys -t "$s" Down
      sleep 0.5
      tmux send-keys -t "$s" Enter
      sleep 2
      continue
    fi
    if printf '%s\n' "$screen" | grep -qiE 'Esc to cancel|Enter to confirm|Do you want'; then
      log "  dialog found; sending Enter"
      tmux send-keys -t "$s" Enter
      sleep 2
      continue
    fi
    if pane_idle "$screen"; then
      save_screen "$s" "at-prompt" >/dev/null
      return 0
    fi
    sleep "$POLL"
  done
  save_screen "$s" "accept-trust-timeout" >/dev/null
  return 1
}

wait_for_turn_end() { # <session> <timeout>; needs a send_prompt offset first.
  # The framed idle prompt must show on two consecutive polls, and the
  # transcript must hold a user record followed by an assistant record with
  # usage, both after PROMPT_OFFSET.
  local s="$1" timeout="$2"
  local deadline=$(( $(date +%s) + timeout ))
  local screen t streak=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    screen="$(read_screen "$s")"
    if pane_idle "$screen"; then
      streak=$((streak + 1))
    else
      streak=0
    fi
    t="$(latest_transcript "$CURRENT_CWD")"
    if [ "$streak" -ge 2 ] && [ -n "$t" ] && [ -f "$t" ] &&
      transcript_turn_after "$t" "$PROMPT_OFFSET"; then
      save_screen "$s" "turn-end" >/dev/null
      return 0
    fi
    sleep "$POLL"
  done
  save_screen "$s" "turn-end-timeout" >/dev/null
  return 1
}

# --- pane command build ------------------------------------------------------------
# build_pane_args <tmux|none|zellij> [extra KEY=VALUE...]
# Fills the global PANE_ARGS array: env unsets every documented plugin key,
# unsets the channel keys for the "none" channel, then sets the test values.
build_pane_args() {
  local channel="$1"
  shift
  PANE_ARGS=(env)
  local k extra
  for k in "${WARMFOLD_KEYS[@]}"; do
    PANE_ARGS+=(-u "$k")
  done
  # A harness started from inside a Claude Code session inherits CLAUDE* markers
  # (CLAUDE_CODE_CHILD_SESSION turns transcript saving off). Scrub them all.
  while IFS= read -r k; do
    [ -n "$k" ] && PANE_ARGS+=(-u "$k")
  done < <(env | grep -o '^CLAUDE[A-Za-z0-9_]*=' | tr -d '=')
  case "$channel" in
    # Claude Code re-exports TMUX and TMUX_PANE to hook processes, so the
    # env -u list alone cannot hide tmux from the hooks. FORCE_CHANNEL=none
    # pins the channel; the real plain-terminal proof is a separate pty run.
    none) PANE_ARGS+=("${CHANNEL_UNSETS[@]}" "WARMFOLD_FORCE_CHANNEL=none") ;;
    zellij) PANE_ARGS+=(-u TMUX -u TMUX_PANE) ;;
  esac
  for extra in "$@"; do
    PANE_ARGS+=("$extra")
  done
  PANE_ARGS+=(WARMFOLD_IDLE_MINUTES=1 WARMFOLD_MIN_CONTEXT_TOKENS=1000
    WARMFOLD_POLL_SECONDS=5 "WARMFOLD_DATA_DIR=$CASE_DIR/data")
  PANE_ARGS+=(claude --model "$MODEL" --plugin-dir "$REPO")
}

# build_pane_command -> the PANE_ARGS array as one sh command string.
# %q quotes each element; this string is what the pane shell runs.
build_pane_command() {
  join_quoted "${PANE_ARGS[@]}"
}

# join_quoted <elements...> -> the elements as one sh command string.
# Each element gets printf %q; the pane shell evaluates the result.
join_quoted() {
  local out="" part
  for part in "$@"; do
    out="$out $(printf '%q' "$part")"
  done
  printf '%s' "${out# }"
}

# wait_for_new_screen_text <session> <before-file> <grep-ERE> <timeout>
# Polls until a line that the pane did not show before matches the pattern.
# Leaves the new lines in NEW_SCREEN_LINES.
wait_for_new_screen_text() {
  local s="$1" before="$2" pat="$3" timeout="$4"
  local deadline=$(( $(date +%s) + timeout ))
  local after="$CASE_DIR/screen-new-poll.txt"
  NEW_SCREEN_LINES=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    capture "$s" > "$after" 2>/dev/null || true
    NEW_SCREEN_LINES="$(screen_new_lines "$before" "$after")"
    if printf '%s\n' "$NEW_SCREEN_LINES" | grep -qiE "$pat"; then
      return 0
    fi
    sleep "$POLL"
  done
  return 1
}

# screen_has_exact_line <text> <needle>
# 0 when one line of text strips to exactly needle. A substring inside a
# longer line (the command menu, for example) does not match.
screen_has_exact_line() {
  python3 -c '
import sys
needle = sys.argv[2].strip().lower()
for ln in sys.argv[1].splitlines():
    if ln.strip().lower() == needle:
        sys.exit(0)
sys.exit(1)
' "$1" "$2"
}

# screen_has_line_starts <text> <needle...>
# 0 when every needle starts at least one line of text (leading spaces
# ignored, case-insensitive).
screen_has_line_starts() {
  python3 -c '
import sys
lines = [ln.lstrip().lower() for ln in sys.argv[1].splitlines()]
for needle in sys.argv[2:]:
    n = needle.strip().lower()
    if not any(ln.startswith(n) for ln in lines):
        sys.exit(1)
sys.exit(0)
' "$@"
}

# wait_for_report_screen <session> <before-file> <header> <timeout> <label...>
# Polls the new screen output until one new line strips exactly to the
# header and every label starts some new line. The exact-line rule keeps the
# autocomplete menu (the header appears inside a longer menu line) from
# satisfying the check before the report renders.
wait_for_report_screen() {
  local s="$1" before="$2" header="$3" timeout="$4"
  shift 4
  local deadline=$(( $(date +%s) + timeout ))
  local after="$CASE_DIR/screen-new-poll.txt"
  NEW_SCREEN_LINES=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    capture "$s" > "$after" 2>/dev/null || true
    NEW_SCREEN_LINES="$(screen_new_lines "$before" "$after")"
    if screen_has_exact_line "$NEW_SCREEN_LINES" "$header" &&
      screen_has_line_starts "$NEW_SCREEN_LINES" "$@"; then
      return 0
    fi
    sleep "$POLL"
  done
  return 1
}

# write_zellij_layout <file> <cwd>
# Writes a one-pane layout that runs PANE_ARGS with the given cwd.
write_zellij_layout() {
  python3 - "$1" "$2" "${PANE_ARGS[@]}" <<'PY'
import sys
path, cwd = sys.argv[1], sys.argv[2]
args = sys.argv[3:]

def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

lines = [
    "layout {",
    "    pane cwd=%s command=%s {" % (q(cwd), q(args[0])),
    "        args " + " ".join(q(a) for a in args[1:]),
    "    }",
    "}",
]
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PY
}

start_claude() { # <tmux-session> <cwd> [pane-command]
  local session="$1" cwd="$2" cmd="${3:-}"
  # Reuse PANE_ARGS from the caller's build_pane_args; rebuild only when empty.
  if [ -z "$cmd" ]; then
    [ "${#PANE_ARGS[@]}" -gt 0 ] || build_pane_args tmux
    cmd="$(build_pane_command)"
  fi
  mkdir -p "$cwd"
  tmux new-session -d -s "$session" -x 120 -y 40 -c "$cwd" "$cmd" || return 1
  TMUX_SESSIONS+=("$session")
  CURRENT_SESSION="$session"
  CURRENT_CWD="$cwd"
  sleep 1
}

zellij_ready() { # <timeout> -> 0 when a zellij action answers for our session
  local timeout="$1"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if "$ZELLIJ_BIN" --session "$ZELLIJ_SESSION" action dump-screen \
      --path "$CASE_DIR/zellij-probe.txt" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# --- case frame ---------------------------------------------------------------------
begin_case() { # <case-name>
  CASE_NAME="$1"
  CASE_DIR="$ART/cases/$1"
  CASE_START="$(date +%s)"
  CASE_STORY_WORD="zebra-$RANDOM"
  # The identifier word ties the story to this case; the clear case requires
  # it in the post-/clear assistant record.
  PROMPT2="Write a 600 word story about a lighthouse keeper named $CASE_STORY_WORD. Then stop."
  mkdir -p "$CASE_DIR/cwd" "$CASE_DIR/data/log"
  TMUX_SESSIONS=()
  PROMPT_OFFSET=0
  log ""
  log "=== case $CASE_NAME ==="
  {
    printf 'case=%s\n' "$CASE_NAME"
    printf 'started=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'cwd=%s\n' "$CASE_DIR/cwd"
    printf 'data_dir=%s\n' "$CASE_DIR/data"
    printf 'repo=%s\n' "$REPO"
    printf 'model=%s\n' "$MODEL"
    printf 'story_word=%s\n' "$CASE_STORY_WORD"
  } > "$CASE_DIR/meta.txt"
}

end_case() { # kill the sessions of this case, keep the evidence in place
  local s
  for s in ${TMUX_SESSIONS[@]+"${TMUX_SESSIONS[@]}"}; do
    tmux kill-session -t "$s" >/dev/null 2>&1 || true
  done
  TMUX_SESSIONS=()
  if [ -n "$CASE_DIR" ] && [ -d "$CASE_DIR" ]; then
    [ -f "$CASE_DIR/data/log/warmfold.log" ] && cp "$CASE_DIR/data/log/warmfold.log" "$CASE_DIR/warmfold.log"
    printf 'finished=%s\nfailures_so_far=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$FAILURES" >> "$CASE_DIR/meta.txt"
  fi
  return 0
}

cleanup() { # EXIT trap; kills only the sessions this run created
  local s
  for s in ${TMUX_SESSIONS[@]+"${TMUX_SESSIONS[@]}"}; do
    tmux kill-session -t "$s" >/dev/null 2>&1 || true
  done
  [ -x "$ZELLIJ_BIN" ] && "$ZELLIJ_BIN" kill-session "$ZELLIJ_SESSION" >/dev/null 2>&1
  return 0
}
