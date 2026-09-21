# Integration tests

Live tests for the warmfold plugin. Each case starts a real `claude`
session in a tmux pane (the `zellij` case starts claude as the one pane of a
zellij session), sends prompts, and checks the transcript, the plugin state
files, the handoff files, and the pane screen. DESIGN.md section 10 defines
the cases.

## Requirements

| Tool | Note |
|---|---|
| bash 4 or newer | runs `run.sh` |
| tmux | hosts every claude session |
| zellij 0.45+ | only for the `zellij` case; default path `~/.local/bin/zellij` |
| python3 | parses the transcript JSONL and the state JSON |
| claude | a paid login; the `claude-haiku-4-5` model must be available |

Session names are unique per run: tmux sessions get the prefix
`ccit-<pid>-<case>`, and the zellij session is `ccit-<pid>-zellij`. The EXIT
trap kills only the sessions this run created. The INT and TERM traps exit
with status 130 and 143; the EXIT trap then cleans up.

## Run

    tests/integration/run.sh all
    tests/integration/run.sh compact-tmux
    ART=/tmp/warmfold-art tests/integration/run.sh guard-cold

The script prints one `ok` or `FAIL` line per check, with evidence under each
failed check. The exit code is non-zero when any check fails. A full run takes
about 30 minutes. Most of the cost is haiku turns for the 600-word story and
the compactions.

## Cases

| Case | Setup | Checks |
|---|---|---|
| `compact-tmux` | claude in tmux | within `IDLE_TIMEOUT` of the confirmed turn end, the transcript gains a `compact_boundary` JSON line with `compactMetadata.trigger` `manual` after the last prompt offset; the state shows `phase = compact_sent` or a new `last_compact_at`; `channel` is `tmux`; no second `compact_boundary` appears within 90 s |
| `handoff-plain` | claude runs with every channel variable unset and `WARMFOLD_FORCE_CHANNEL=none` (see the channel note below) | the state phase reaches `handoff_done` within the deadline; then the handoff `.md` exists in this case's handoff tree and holds all seven headings (`Goal`, `Current state`, `Decisions made`, `Open tasks`, `Key files and paths`, `Next step`, `Things to avoid`) as heading lines in order; `channel` is `none` |
| `guard-cold` | as `handoff-plain`, plus `WARMFOLD_FORCE_TTL_SECONDS=30`, `WARMFOLD_GUARD=1`, `WARMFOLD_GUARD_MIN_USD=0.0001`, `WARMFOLD_GUARD_ACK_SECONDS=60`; the test waits for `phase = handoff_done` (the watcher acts before the forced expiry), then waits 60 s for the real expiry, then sends a prompt | the pane gains new lines with `full cost` and `warmfold`; the blocked submit adds no user record and no assistant usage record to the transcript; a second submit within the ack window runs a full turn and adds both records |
| `clear-loads-handoff` | runs the handoff flow, then `/clear`, then asks `what is the handoff goal` | `latest.json` gets a positive `consumed_at` after the `/clear` submission time and holds non-empty `cwd`, `session_id`, `saved_at`, `path`; the new assistant record contains the case's story identifier word |
| `status-local` | `/warmfold:status` at the prompt | the report appears in new screen lines only (a diff against the pre-command capture); one new line is exactly `warmfold status` (the autocomplete menu holds the words inside a longer line), and the labels `Session:`, `Model:`, `TTL:`, `Channel:`, `Mode:`, `Next action:` each start a new line; the poll runs up to 30 s; the command adds no user record and no assistant usage record |
| `savings` | as `compact-tmux`; after the compact asserts, `/warmfold:savings`, then the return prompt `Count from 1 to 3.` | the report appears in new screen lines; one new line is exactly `warmfold savings` (the autocomplete menu line also holds the words), and `realized saved`, `wasted`, `net`, `pending actions` each start a new line; the poll runs up to 30 s and the screen at the match lands in `screen-savings-report.txt`; the command adds no user record and no assistant usage record; `data/ledger.jsonl` holds exactly one `type=action` line with `action=compact` and one `type=outcome` line with `outcome=early` (the return lands long before `cold_at` under the forced 1 h ttl; DESIGN.md section 12) |
| `zellij` | claude runs as the one pane of a new zellij session; the layout command unsets `TMUX` and `TMUX_PANE`, so the channel is zellij | the same checks as `compact-tmux`, but `channel` is `zellij` |

Every case uses a fresh cwd under `$ART/cases/<case>/cwd` and a fresh data
directory under `$ART/cases/<case>/data`, so one case can never satisfy the
asserts of another. The flow is: accept the trust dialog with `Down Enter`,
send `Say the word ready and stop.`, wait for the turn, send the story prompt,
wait for the turn. The story prompt carries a random identifier word:

    Write a 600 word story about a lighthouse keeper named zebra-12345. Then stop.

The story pushes the context above 1000 tokens. The identifier word lets the
clear case prove that the handoff content, not old context, produced the
answer after `/clear`.

## Test configuration

The pane command unsets every documented `WARMFOLD_*` key first (a user
value such as `WARMFOLD_MODE` must not leak into the test), unsets the
channel variables for the channel-less cases, sets
`WARMFOLD_FORCE_CHANNEL=none` on the channel-less cases (see below), and
then sets:

    WARMFOLD_IDLE_MINUTES=1
    WARMFOLD_MIN_CONTEXT_TOKENS=1000
    WARMFOLD_POLL_SECONDS=5
    WARMFOLD_DATA_DIR=$ART/cases/<case>/data

Extra values per case:

- `WARMFOLD_FORCE_TTL_SECONDS=3600` on `compact-tmux`, `status-local`,
  `savings`, and `zellij`. This forces the 1 h policy on any credential. The
  compaction then fires at 60 s idle on a plan with a 5 min TTL too, and the
  test is deterministic.
- `WARMFOLD_FORCE_TTL_SECONDS=30` on `guard-cold`. The deadline is
  start + 30 s - min(margin, 6 s), so the watcher hands off within seconds.
  The handoff turn refreshes the cache once; 60 s later it is cold and the
  guard blocks the typed prompt.
  `WARMFOLD_GUARD_MIN_USD=0.0001` lowers the cost gate: a context just
  above 1000 tokens costs less than the default `$1` minimum.
  `WARMFOLD_GUARD_ACK_SECONDS=60` bounds the ack window that lets the
  resent prompt pass.
- `WARMFOLD_FORCE_CHANNEL=none` on `handoff-plain`, `guard-cold`, and
  `clear-loads-handoff`. A live run showed that Claude Code re-exports
  `TMUX` and `TMUX_PANE` to hook processes even when the pane command removed
  them, so `env -u` alone cannot simulate a plain terminal inside tmux. The
  force flag pins the channel decision. A real plain terminal has no such
  variables at all; the proof for that host is a separate pty-based run with
  no tmux, which the tester runs by hand.
- `WARMFOLD_TTL_5M_POLICY` stays unset. The default `warn` policy does
  not compact; the forced TTL removes the need to set it.

Env overrides for the harness itself:

| Variable | Default | Meaning |
|---|---|---|
| `ART` | a new temp directory | artifact root (made absolute) |
| `MODEL` | `claude-haiku-4-5` | model for every session |
| `TURN_TIMEOUT` | 180 | seconds for one claude turn |
| `IDLE_TIMEOUT` | 180 | seconds for one idle action, from the confirmed turn end |
| `POLL` | 3 | poll interval in seconds |
| `ZELLIJ_BIN` | `~/.local/bin/zellij` | zellij binary |
| `ZELLIJ_SESSION` | `ccit-<pid>-zellij` | zellij session name |
| `REPO` | the repo root | value passed to `--plugin-dir` |
| `PROJECTS_DIR` | `~/.claude/projects` | transcript root |

## Evidence the checks rely on

- A `turn end` means: the framed idle prompt (a `❯` line between two `─` rule
  lines, no spinner, no dialog) shows on two consecutive polls, and the
  transcript holds a user record followed by a later assistant record with
  `message.usage`, both after the byte offset recorded before the prompt went
  in.
- The transcript checks parse JSON lines with python3; they never grep for
  JSON text. Each check reads only bytes appended after the recorded offset,
  so an old boundary or an old answer cannot satisfy a check.
- The guard and status checks compare the screen after the command against a
  capture before it, so scrollback text cannot satisfy them.

## Artifacts

    $ART/cases/<case>/
      meta.txt                case name, start time, cwd, story word, model
      layout.kdl              zellij case: the one-pane layout
      zellij-probe.txt        zellij case: the dump-screen probe output
      screen-*.txt            pane captures at key points
      screen-savings-report.txt savings case: the screen with the rendered report
      warmfold.log              copy of the plugin log (from data/log/warmfold.log)
      data/sessions/          plugin state files for this case
      data/handoffs/          handoff files and latest.json for this case
      data/ledger.jsonl       savings case: the action and outcome records
      data/log/warmfold.log     the plugin log as the plugin wrote it

## Notes for test readers

- The `savings` case reads the ledger after the return turn ends and for up
  to 30 s more. The counts must be exactly one `action/compact` and one
  `outcome/early`: the case compacts once and returns once, and each action
  gets at most one outcome (DESIGN.md section 12).
- The compact cases also assert that no second `compact_boundary` appears
  within 90 s of the first check. This guards the re-compaction loop. A
  legitimate second compaction needs 60 s idle plus a context above
  `WARMFOLD_MIN_CONTEXT_TOKENS`; the compacted context of this short
  session stays below that gate.
- The state check for a compaction accepts `phase = compact_sent` or a
  `last_compact_at` greater than the pre-wait value. After a compaction,
  Claude Code fires `SessionStart(compact)`, `PostCompact`, and a `Stop`, and
  the phase returns to `idle`, so the phase alone can be missed.
- `handoff-plain` waits for `phase = handoff_done` before it reads the
  handoff file, so the writer has closed it.
- The guard case asserts that a blocked submit leaves no user record and no
  assistant usage record in the transcript. This follows the design: a
  blocked `UserPromptSubmit` produces no API call. If a claude version starts
  writing the blocked prompt as a user record, this check fails and the
  assumption needs a new look.
- The status labels come from the plugin's report builder. If the wording
  changes there, the label list in `case_status_local` changes with it.
- zellij 0.45.1 starts a new named session with
  `zellij --session NAME --new-session-with-layout FILE`. The plain
  `--layout` flag targets an existing session and fails for a new one. The
  pane process receives `ZELLIJ_SESSION_NAME`, `ZELLIJ_PANE_ID`, and
  `ZELLIJ` (the value is the string `0`), so channel detection must test the
  presence of `ZELLIJ`, not its value.
