#!/usr/bin/env bash
# run.sh - live integration cases for warmfold (DESIGN.md section 10).
#
# Usage:
#   run.sh <case>          run one case
#   run.sh all             run every case in sequence
#
# Cases: compact-tmux guard-cold status-local savings zellij
#
# Each case starts a real claude in a tmux pane (or a zellij pane for the
# zellij case), sends real prompts, and asserts the watcher behavior in the
# state files, the transcript, and the pane screen.
# Every case gets its own data directory under $ART/cases/<case>/data.
#
# Requirements: bash 3.2+, tmux, python3, and the claude CLI on PATH.
# See README.md for the environment overrides and the artifact layout.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

usage() {
  log "usage: run.sh <case>"
  log "cases: compact-tmux guard-cold status-local savings zellij all"
}

# prime_session <session>: accept the trust dialog, then run two turns so the
# context passes WARMFOLD_MIN_CONTEXT_TOKENS and a request time exists.
prime_session() {
  local s="$1"
  accept_trust "$s" 120 || { save_screen "$s" "prime-trust" >/dev/null; return 1; }
  send_prompt "$s" "$PROMPT1"
  wait_for_turn_end "$s" "$TURN_TIMEOUT" || { save_screen "$s" "prime-turn1" >/dev/null; return 1; }
  send_prompt "$s" "$PROMPT2"
  wait_for_turn_end "$s" "$TURN_TIMEOUT" || { save_screen "$s" "prime-turn2" >/dev/null; return 1; }
  return 0
}

# assert_state_channel <tmux|none|zellij>
assert_state_channel() {
  local want="$1" got
  got="$(json_field "$(state_file)" channel)"
  if [ "$got" = "$want" ]; then
    check "state channel is $want" 0
  else
    check "state channel is $want" 1 "got: $got"
  fi
}

# assert_compact <channel>: the watcher compacts within IDLE_TIMEOUT after the
# confirmed turn end; the transcript gains a manual compact_boundary; the
# state records the channel. Uses PROMPT_OFFSET from the last send_prompt.
assert_compact() {
  local channel="$1" base line
  base="$(json_field "$(state_file)" last_compact_at)"
  base="${base:-0}"
  if wait_for_compact_state "$base" "$IDLE_TIMEOUT"; then
    check "watcher compacted within ${IDLE_TIMEOUT}s of turn end" 0
  else
    save_screen "$CURRENT_SESSION" "compact-wait" >/dev/null
    check "watcher compacted within ${IDLE_TIMEOUT}s of turn end" 1 \
      "state: $(clip "$(cat "$(state_file)" 2>/dev/null)")"
  fi
  local t
  t="$(latest_transcript "$CURRENT_CWD")"
  # Claude Code writes the compact_boundary about 20 s after the keystrokes;
  # poll for it (up to 120 s) before the assertion.
  local deadline=$(( $(date +%s) + 120 ))
  line=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    t="$(latest_transcript "$CURRENT_CWD")"
    if [ -n "$t" ] && line="$(transcript_manual_compact_after "$t" "$PROMPT_OFFSET")"; then
      break
    fi
    line=""
    sleep 5
  done
  if [ -n "$line" ]; then
    check "transcript has compact_boundary trigger=manual after the last turn" 0 "$line"
  else
    check "transcript has compact_boundary trigger=manual after the last turn" 1 \
      "transcript: $t offset: $PROMPT_OFFSET"
  fi
  assert_state_channel "$channel"
  # Regression guard: a second compact_boundary within 90 s means the
  # re-compaction loop is back. A legitimate second compaction needs 60 s
  # idle plus a context above MIN_CONTEXT_TOKENS; the compacted context of
  # this short session stays below that gate.
  # Let the post-compaction turn settle before taking the offset mark.
  sleep 15
  t="$(latest_transcript "$CURRENT_CWD")"
  local mark
  mark="$(transcript_size "$t")"
  sleep 90
  if [ -n "$t" ] && transcript_has_no_compact_after "$t" "$mark"; then
    check "no second compact_boundary within 90 s" 0
  else
    check "no second compact_boundary within 90 s" 1 \
      "transcript: $t from offset: $mark"
  fi
}

# prime_or_fail <session> <label>: shared start of every case body.
prime_or_fail() {
  local s="$1" label="$2"
  if ! prime_session "$s"; then
    check "$label" 1 "see $CASE_DIR/screen-prime-*.txt"
    end_case
    return 1
  fi
  return 0
}

case_compact_tmux() {
  begin_case compact-tmux
  build_pane_args tmux "WARMFOLD_FORCE_TTL_SECONDS=3600"
  if ! start_claude "ccit-${RUN_ID}-compact-tmux" "$CASE_DIR/cwd"; then
    check "claude session started" 1 "tmux new-session failed"
    end_case
    return 0
  fi
  prime_or_fail "$CURRENT_SESSION" "claude accepted trust and finished two turns" || return 0
  assert_compact tmux
  end_case
}

case_guard_cold() {
  begin_case guard-cold
  # FORCE_TTL_SECONDS=30 makes the watcher defer before cache expiry; the
  # watcher exits after recording compact_deferred.
  # The guard blocks one submit, then lets the resent prompt through the
  # ack window (DESIGN.md section 6, return guard).
  build_pane_args none "WARMFOLD_GUARD=1" "WARMFOLD_GUARD_MIN_USD=0.0001" \
    "WARMFOLD_GUARD_ACK_SECONDS=60" "WARMFOLD_FORCE_TTL_SECONDS=30"
  if ! start_claude "ccit-${RUN_ID}-guard" "$CASE_DIR/cwd"; then
    check "claude session started" 1 "tmux new-session failed"
    end_case
    return 0
  fi
  prime_or_fail "$CURRENT_SESSION" "claude accepted trust and finished two turns" || return 0
  # With FORCE_TTL_SECONDS=30 the action deadline is start + 30 - min(margin,
  # 6 s). The watcher defers and exits. Wait for that state, then wait 60 s
  # from the latest turn so last_request_start + TTL is safely in the past.
  if wait_for_state_phase compact_deferred 120; then
    check "watcher deferred without fallback injection" 0
    sleep 60
  else
    check "watcher deferred without fallback injection" 1 \
      "state: $(clip "$(cat "$(state_file)" 2>/dev/null)")"
    end_case
    return 0
  fi
  local t
  t="$(latest_transcript "$CURRENT_CWD")"
  save_screen "$CURRENT_SESSION" "pre-guard" >/dev/null
  send_prompt "$CURRENT_SESSION" "$PROMPT_GUARD"
  if wait_for_new_screen_text "$CURRENT_SESSION" "$CASE_DIR/screen-pre-guard.txt" 'full cost' 60; then
    check "guard message appeared in new screen output" 0
  else
    save_screen "$CURRENT_SESSION" "guard-wait" >/dev/null
    check "guard message appeared in new screen output" 1 "no new line matched 'full cost'"
    end_case
    return 0
  fi
  if printf '%s\n' "$NEW_SCREEN_LINES" | grep -qi 'warmfold'; then
    check "guard message names warmfold" 0
  else
    check "guard message names warmfold" 1
  fi
  # The blocked submit must leave no user record and no assistant usage
  # record after the pre-submit offset.
  sleep 2
  if [ -n "$t" ] && transcript_has_no_records_after "$t" "$PROMPT_OFFSET"; then
    check "blocked submit added no user or assistant record" 0
  else
    check "blocked submit added no user or assistant record" 1 \
      "transcript: $t offset: $PROMPT_OFFSET"
  fi
  # The ack window lets the second submit through; both records now appear.
  send_prompt "$CURRENT_SESSION" "$PROMPT_GUARD"
  if wait_for_turn_end "$CURRENT_SESSION" "$TURN_TIMEOUT"; then
    check "second submit ran a full turn" 0
  else
    save_screen "$CURRENT_SESSION" "guard-resubmit" >/dev/null
    check "second submit ran a full turn" 1 "the ack window may have closed"
    end_case
    return 0
  fi
  t="$(latest_transcript "$CURRENT_CWD")"
  if [ -n "$t" ] && transcript_turn_after "$t" "$PROMPT_OFFSET"; then
    check "second submit added a user and an assistant usage record" 0
  else
    check "second submit added a user and an assistant usage record" 1 \
      "transcript: $t offset: $PROMPT_OFFSET"
  fi
  end_case
}

case_status_local() {
  begin_case status-local
  build_pane_args tmux "WARMFOLD_FORCE_TTL_SECONDS=3600"
  if ! start_claude "ccit-${RUN_ID}-status" "$CASE_DIR/cwd"; then
    check "claude session started" 1 "tmux new-session failed"
    end_case
    return 0
  fi
  prime_or_fail "$CURRENT_SESSION" "claude accepted trust and finished two turns" || return 0
  save_screen "$CURRENT_SESSION" "pre-status" >/dev/null
  send_prompt "$CURRENT_SESSION" "$STATUS_CMD"
  # The header must be a full line; the autocomplete menu also shows the
  # words "warmfold status" inside a longer line.
  if wait_for_report_screen "$CURRENT_SESSION" "$CASE_DIR/screen-pre-status.txt" \
    "warmfold status" 30 "Session:" "Model:" "TTL:" "Channel:" "Mode:" "Next action:"; then
    check "status report appeared in new screen output" 0
  else
    save_screen "$CURRENT_SESSION" "status-wait" >/dev/null
    check "status report appeared in new screen output" 1 \
      "no line is exactly 'warmfold status' or a label is missing"
    end_case
    return 0
  fi
  # Exact report labels (the report builder is the plugin contract), each at
  # the start of a line.
  local label
  for label in "Session:" "Model:" "TTL:" "Channel:" "Mode:" "Next action:"; do
    if screen_has_line_starts "$NEW_SCREEN_LINES" "$label"; then
      check "report shows $label" 0
    else
      check "report shows $label" 1
    fi
  done
  local t
  t="$(latest_transcript "$CURRENT_CWD")"
  if [ -n "$t" ] && transcript_turn_after "$t" "$PROMPT_OFFSET"; then
    check "status command completed a normal assistant turn" 0
  else
    check "status command completed a normal assistant turn" 1 \
      "transcript: $t offset: $PROMPT_OFFSET"
  fi
  end_case
}

case_savings() {
  begin_case savings
  build_pane_args tmux "WARMFOLD_FORCE_TTL_SECONDS=3600"
  if ! start_claude "ccit-${RUN_ID}-savings" "$CASE_DIR/cwd"; then
    check "claude session started" 1 "tmux new-session failed"
    end_case
    return 0
  fi
  prime_or_fail "$CURRENT_SESSION" "claude accepted trust and finished two turns" || return 0
  # The compaction writes one action record; the report reads the ledger.
  assert_compact tmux
  save_screen "$CURRENT_SESSION" "pre-savings" >/dev/null
  send_prompt "$CURRENT_SESSION" "$SAVINGS_CMD"
  # The header must be a full line; the autocomplete menu line
  # "/warmfold:savings (warmfold) Show warmfold savings: ..." also holds the
  # words, so a substring match fires before the report renders.
  if wait_for_report_screen "$CURRENT_SESSION" "$CASE_DIR/screen-pre-savings.txt" \
    "warmfold savings" 30 "realized saved" "wasted" "net" "pending actions"; then
    capture "$CURRENT_SESSION" > "$CASE_DIR/screen-savings-report.txt" 2>/dev/null
    check "savings report appeared in new screen output" 0
  else
    save_screen "$CURRENT_SESSION" "savings-wait" >/dev/null
    check "savings report appeared in new screen output" 1 \
      "no line is exactly 'warmfold savings' or a label is missing"
    end_case
    return 0
  fi
  # Exact report labels (DESIGN.md section 12), each at the start of a line.
  local label
  for label in "realized saved" "wasted" "net" "pending actions"; do
    if screen_has_line_starts "$NEW_SCREEN_LINES" "$label"; then
      check "report shows $label" 0
    else
      check "report shows $label" 1
    fi
  done
  local t
  t="$(latest_transcript "$CURRENT_CWD")"
  if [ -n "$t" ] && transcript_turn_after "$t" "$PROMPT_OFFSET"; then
    check "savings command completed a normal assistant turn" 0
  else
    check "savings command completed a normal assistant turn" 1 \
      "transcript: $t offset: $PROMPT_OFFSET"
  fi
  # The first user return after the action lands long before cold_at
  # (forced ttl 3600), so the outcome is early (DESIGN.md section 12).
  send_prompt "$CURRENT_SESSION" "$PROMPT_RETURN"
  if wait_for_turn_end "$CURRENT_SESSION" "$TURN_TIMEOUT"; then
    check "post-action return finished a turn" 0
  else
    save_screen "$CURRENT_SESSION" "savings-return" >/dev/null
    check "post-action return finished a turn" 1
    end_case
    return 0
  fi
  local ledger="$CASE_DIR/data/ledger.jsonl"
  local counts="" deadline=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    counts="$(savings_ledger_counts "$ledger")"
    [ "$counts" = "1 1" ] && break
    sleep "$POLL"
  done
  if [ "$counts" = "1 1" ]; then
    check "ledger holds one action compact and one outcome early" 0 "counts: $counts"
  else
    check "ledger holds one action compact and one outcome early" 1 \
      "ledger: $ledger counts: $counts"
  fi
  end_case
}

case_zellij() {
  begin_case zellij
  build_pane_args zellij "WARMFOLD_FORCE_TTL_SECONDS=3600"
  write_zellij_layout "$CASE_DIR/layout.kdl" "$CASE_DIR/cwd"
  # claude runs as the one pane of a new zellij session; the tmux pane only
  # hosts the zellij TUI. The trailing sleep keeps an early zellij exit
  # visible in the pane.
  local zcmd zstr
  # A zellij config without the startup tips popup: the popup would swallow
  # the trust-dialog keystrokes.
  printf 'show_startup_tips false\nshow_release_notes false\n' > "$CASE_DIR/zellij-config.kdl"
  zcmd=("$ZELLIJ_BIN" --config "$CASE_DIR/zellij-config.kdl" --session "$ZELLIJ_SESSION" --new-session-with-layout "$CASE_DIR/layout.kdl")
  zstr="$(join_quoted "${zcmd[@]}")"
  if ! start_claude "ccit-${RUN_ID}-zellij-wrap" "$CASE_DIR/cwd" "$zstr; sleep 60"; then
    check "zellij session started in tmux pane" 1 "tmux new-session failed"
    end_case
    return 0
  fi
  if ! zellij_ready 30; then
    save_screen "$CURRENT_SESSION" "zellij-ready" >/dev/null
    check "zellij session became responsive" 1 "see screen-zellij-ready.txt"
    end_case
    return 0
  fi
  prime_or_fail "$CURRENT_SESSION" "claude accepted trust and finished two turns" || return 0
  assert_compact zellij
  end_case
}

main() {
  [ $# -ge 1 ] || { usage; exit 2; }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  case "$1" in
    compact-tmux) case_compact_tmux ;;
    guard-cold) case_guard_cold ;;
    status-local) case_status_local ;;
    savings) case_savings ;;
    zellij) case_zellij ;;
    all)
      case_compact_tmux
      case_guard_cold
      case_status_local
      case_savings
      case_zellij
      ;;
    -h | --help | help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  log ""
  log "=== summary ==="
  log "failures: $FAILURES"
  log "artifacts: $ART"
  [ "$FAILURES" -eq 0 ] || exit 1
  exit 0
}

main "$@"
