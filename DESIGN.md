# warmfold — implementer brief

Plugin for Claude Code. It compacts (or hands off) an idle session before the
prompt cache expires, so the first request after a long break is cheap.

This file is the shared contract for the work packages. Keep it in sync with the
code. Prose in this repo follows ASD-STE100 Simplified Technical English.

## 1. Facts that drive the design (verified 2026-09-21 against the docs)

| Fact | Consequence |
|---|---|
| The cache timer restarts on each request that reads the cache. The timer starts at the request start. Generation time counts. | Expiry = last request start + TTL. Do not use the turn end. |
| TTL is 1 h on a subscription inside plan usage. It is 5 min on usage credits, API key, Bedrock, Vertex. `promptCacheTtl: "1h"` forces 1 h. | Read the TTL per session from the transcript (`usage.cache_creation.ephemeral_1h_input_tokens` vs `ephemeral_5m_input_tokens`). |
| `/compact` sends the full history in one request. Warm: it reads the prefix from cache. Cold: it pays full price. | Never compact a cold session. Compact only while warm. |
| Hooks cannot run slash commands. Scheduled tasks (cron, `/loop`) deliver built-in commands as plain text. | Only a keystroke in a terminal, or the user, can run `/compact`. |
| An `async: true, asyncRewake: true` command hook that exits 2 wakes Claude even while the session is idle. Its stderr reaches Claude as a system reminder. | This is the universal idle trigger. It works in the CLI, Desktop app, VS Code, and SDK hosts such as T3 Code. |
| `Stop` input has `last_assistant_message`, `background_tasks`, `session_crons`. `Stop` does not fire on user interrupt. `StopFailure` fires on API errors. | The watcher saves the handoff text from `last_assistant_message`. |
| `UserPromptSubmit` can block a prompt (`decision: "block"`, `reason`, `suppressOriginalPrompt`). The prompt is erased. No API call happens. | The return guard and the local `/warmfold:status` command cost zero tokens. |
| `SessionStart` matchers: `startup`, `resume`, `clear`, `compact`. Its `additionalContext` reaches Claude. | The handoff loader injects the saved handoff after `/clear`. |
| `Notification/idle_prompt` fires ~60 s after Claude stops, only if the user typed nothing. | Hint that the input box is empty. |
| Hook processes inherit the environment of the `claude` process (`TMUX_PANE`, `ZELLIJ_SESSION_NAME`, `KITTY_LISTEN_ON`, `WEZTERM_PANE`, `STY`). Hooks run without a controlling tty. | Channel detection uses env vars. Injection uses the multiplexer CLI, never `/dev/tty`. |
| Exec-form hooks (`command` + `args`) spawn no shell. `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PROJECT_DIR` are exported. Plugin `userConfig` values arrive as `CLAUDE_PLUGIN_OPTION_<KEY>`. | Use exec form. Parent PID of the hook process is the `claude` process. |
| Async hooks are not deduplicated. Each `Stop` spawns a new process. `timeout` is enforced on `asyncRewake` hooks. | Each watcher writes a token to the state file. Older watchers exit when superseded. |
| Transcript lines: `{"type":"assistant","timestamp":"...","message":{"model":"...","usage":{"input_tokens","cache_creation_input_tokens","cache_read_input_tokens","cache_creation":{"ephemeral_5m_input_tokens","ephemeral_1h_input_tokens"}}}}`. Also `{"type":"user","timestamp":...}` and `{"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger":"auto|manual","preTokens":N}}`. Format is unstable between versions. | Parse defensively. Missing fields mean "unknown", never a crash. |
| Live tmux test: the prompt line is `❯ ` framed by `────` rule lines. `C-u C-u C-k` clears typed text. Typing `/compact` then Enter runs the command. A trust dialog or a permission dialog shows other text. | Fingerprint the pane before typing. |
| Prices ($/MTok input, cache read, 1h cache write): fable-5-1 and fable-5: 10 / 0.25 / 20. opus-5, 4.8, 4.7, 4.6: 5 / 0.50 / 10. sonnet-5: 2 / 0.20 / 4. sonnet-4-6: 3 / 0.30 / 6. haiku-4-5: 1 / 0.10 / 2. | Cold return cost ≈ tokens × 1h write price (cache is rebuilt). Warm compaction ≈ tokens × read price + summary. |

## 2. Repository layout

```
warmfold/
  .claude-plugin/plugin.json      manifest, userConfig, hooks path
  .claude-plugin/marketplace.json local marketplace so `claude plugin marketplace add <dir>` works
  hooks/hooks.json                all hook wiring, exec form
  commands/status.md              /warmfold:status (intercepted locally by the guard)
  scripts/warmfold.py               single CLI entry: python3 warmfold.py <event>
  scripts/warmfoldlib/              package: config.py transcript.py state.py cost.py
                                  channels.py actions.py events.py log.py
  tests/                          pytest unit tests + fixtures/ (transcripts)
  tests/integration/              tmux-driven live tests (real claude, cheap model)
  settings-snippet.json           the same hooks for hosts that do not load plugins
  README.md
  DESIGN.md                       this file
```

Python 3.8+ standard library only. No third-party imports at runtime. Tests may use pytest.

## 3. Configuration

Precedence, first match wins:

1. Environment `WARMFOLD_<KEY>` (tests and power users).
2. `$CLAUDE_PLUGIN_DATA/config.json` (optional).
3. Plugin option env `CLAUDE_PLUGIN_OPTION_<KEY>` (from `userConfig`).
4. Default.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `idle_minutes` | number | 30 | Idle time after a turn end before the action. |
| `min_context_tokens` | number | 100000 | Below this, do nothing. |
| `safety_margin_minutes` | number | 5 | Act at least this long before cache expiry. |
| `mode` | string | `auto` | `auto`, `compact`, `handoff`, `keepalive`, `warn`. `auto` = `compact` when a verified keystroke channel exists, else `handoff`. |
| `keepalive_hours` | number | 0 | In `keepalive` mode: max hours of keep-alive wakes, then handoff. |
| `ttl_5m_policy` | string | `warn` | `warn`, `compact_at_4m`, `off`. Applies when the session TTL is 5 min. |
| `guard` | boolean | true | Block the first prompt once when the cache is cold and the context is large. |
| `guard_min_usd` | number | 1.0 | Guard only when the estimated cold cost is at least this. |
| `guard_ack_seconds` | number | 120 | After a block, the next submit inside this window passes. |
| `handoff_autoload` | string | `clear` | `clear`: inject the handoff after `/clear`. `clear+startup`: also on a new session in the same directory. `off`. |
| `handoff_max_age_hours` | number | 24 | Older handoffs are ignored. |
| `poll_seconds` | number | 15 | Watcher poll interval. |
| `pane_markers` | string | `❯ ` | Comma-separated substrings. The captured screen must contain one, on a line framed by rule lines, before typing. |
| `data_dir` | string | `$CLAUDE_PLUGIN_DATA` or `~/.claude/warmfold` | State, handoffs, log. |
| `force_ttl_seconds` | number | 0 | Test only. Overrides the TTL read from the transcript. |
| `force_channel` | string | `` | Test only. `none` disables keystroke channels. |

`userConfig` in `plugin.json` exposes: `idle_minutes` (min 1, max 55), `min_context_tokens`, `mode` (with `options`), `guard`, `ttl_5m_policy` (with `options`), `keepalive_hours` (min 0, max 12). Every key has a `default`. The `options` field needs Claude Code 2.1.271 or later.

## 4. State

`<data_dir>/sessions/<session_id>.json`, written atomically (temp file + rename). Fields:

```
armed_token      float  epoch of the Stop that owns the current watcher
phase            str    idle | handoff_pending | handoff_done | keepalive_pending | compact_sent | cold | done
activity_at      float  last UserPromptSubmit
idle_confirmed_at float last Notification idle_prompt
guard_ack_until  float
last_action_at   float
wake_count       int    keep-alive wakes since last user activity
last_compact_at  float  from PostCompact
channel          str    detected channel name or "none"
cwd, transcript_path, model, context_tokens, ttl_seconds, expires_at  (last known, for /status)
```

Handoffs: `<data_dir>/handoffs/<sha1(cwd)[:12]>/<YYYYmmdd-HHMMSS>-<session_id>.md` plus `latest.json` `{path, session_id, cwd, saved_at, consumed_at}`.

Log: `<data_dir>/log/warmfold.log`, one line per event, truncated to 1 MB when it grows past 2 MB.

## 5. Transcript reading (`transcript.py`)

Function `read_transcript(path) -> TranscriptInfo`:

- Read from the end. Stop after the last assistant message with `message.usage` and the last `user` line before it. Do not load the whole file on each poll; cache by `(size, mtime)`.
- `context_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens` of the last assistant usage. If a `compact_boundary` line appears after that assistant line, `context_tokens = compactMetadata.postTokens` if present, else `None` (unknown, treat as small).
- `model` from the assistant `message.model`.
- `ttl_seconds`: 3600 if the last usage with a nonzero `cache_creation` has `ephemeral_1h_input_tokens > 0`, 300 if only `ephemeral_5m_input_tokens > 0`, else `None`. Search back up to 20 assistant lines for a nonzero write.
- `last_request_start`: timestamp of the last `user` line (tool result or prompt) before the last assistant line. Fallback: last assistant timestamp minus 0.
- `last_assistant_at`: timestamp of the last assistant line.
- `caching_observed`: any assistant usage with cache tokens.
- Timestamps are ISO 8601 with `Z`. Parse to epoch. Bad lines are skipped.

## 6. Events (`events.py`) — one function per hook, `python3 warmfold.py <event>`

All read JSON from stdin. All exit 0 with optional JSON on stdout unless stated. Never raise; log and exit 0 on internal errors (exit 0 must never block Claude).

- `watch` (Stop, async + asyncRewake, timeout 7200):
  1. Parse input. If `state.phase == handoff_pending` and `last_assistant_message` is non-empty: save it as the handoff, set `phase = handoff_done`, log, exit 0. Do not arm.
  2. If `phase == keepalive_pending`: set `phase = idle`, `wake_count += 1`, continue to arm.
  3. Arm: `armed_token = now`, `phase = idle`, record channel name. Write state.
  4. Loop every `poll_seconds`:
     - exit 0 if `os.getppid()` changed (claude died), or `state.armed_token != my token` (superseded), or `state.activity_at > my token` (user typed).
     - Re-read transcript. If `last_assistant_at > my token + 2 s`: another turn happened (background task, cron). Exit 0; that turn's Stop re-arms.
     - `idle = now - my token`. `expires_at = last_request_start + ttl - margin`. If `ttl` unknown: assume 3600 and log.
     - If `now >= expires_at`: set `phase = cold`, exit 0. The guard handles the return.
     - If `context_tokens` unknown or `< min_context_tokens`: exit 0.
     - `due = idle >= idle_minutes*60 or (expires_at - now) <= 60`.
     - If `ttl == 300`: apply `ttl_5m_policy` (`warn` and `off` → exit 0; `compact_at_4m` → `due = idle >= 240`).
     - If not due: continue.
     - Resolve mode. `auto` → `compact` if `channels.detect()` returns a verified channel, else `handoff`.
     - `compact`: `channels.inject_compact()`. On success `phase = compact_sent`, `last_action_at`, exit 0. On failure fall back to `handoff`.
     - `handoff`: `phase = handoff_pending`, write state, print the HANDOFF_REMINDER to stderr, exit 2.
     - `keepalive`: if `wake_count * 55 min < keepalive_hours*3600`: `phase = keepalive_pending`, print KEEPALIVE_REMINDER to stderr, exit 2. Else `handoff`.
     - `warn`: exit 0.
- `prompt` (UserPromptSubmit, sync, timeout 10):
  1. If `prompt.strip()` starts with `/warmfold:status`: build the status report, output `{"decision":"block","reason":<report>,"suppressOriginalPrompt":true}`, exit 0.
  2. `activity_at = now`. If `phase` in `(handoff_done, keepalive_pending, compact_sent, cold, done)`: `phase = idle`, `wake_count = 0`.
  3. Guard: read transcript. `cold = now > last_request_start + ttl`. If `guard` and cold and `context_tokens >= min_context_tokens` and `cold_cost_usd >= guard_min_usd` and `now > guard_ack_until`: set `guard_ack_until = now + guard_ack_seconds`, output block JSON with the GUARD_MESSAGE (idle duration, context tokens, model, estimated cost, handoff hint if a fresh handoff exists, "resend to continue at full cost"). `suppressOriginalPrompt: false`.
- `session-start` (SessionStart, matchers `startup|resume|clear|compact`): reset watcher state for the new session id. On `clear` (and `startup` when configured): if `latest.json` for this cwd is fresh and unconsumed, output `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":<handoff with a header>},"systemMessage":"warmfold: loaded handoff from <time>"}` and mark consumed. On `startup` with `handoff_autoload = clear`: inject one line only: "A warmfold handoff from <time> exists at <path>. Read it only if the user continues that work."
- `session-end` (SessionEnd, must finish in < 1 s): set `phase = done`. Nothing else.
- `notification` (Notification, matcher `idle_prompt`): `idle_confirmed_at = now`.
- `pre-compact`: log trigger. `post-compact`: `last_compact_at = now`, `phase = idle`.
- `stop-failure` (StopFailure): `phase = idle`, do not arm.
- `status` (also used by `prompt`): print the report to stdout for manual use: session, model, context tokens, TTL, warm/cold, expires in, idle, channel, mode, next action, estimated cold cost, estimated warm compaction cost, handoff path.

HANDOFF_REMINDER (stderr, exit 2):

```
warmfold: the user has been idle for {idle_min} minutes. The prompt cache expires in {left_min} minutes. Context is {tokens} tokens.
Write a handoff summary for a fresh session. Use these headings: Goal, Current state, Decisions made, Open tasks, Key files and paths, Next step, Things to avoid.
Rules: plain text under 1500 words, no tool calls, no questions, no preamble. Start with the first heading. Stop after the last section.
```

KEEPALIVE_REMINDER: `warmfold keep-alive. Reply with exactly: ok`

## 7. Channels (`channels.py`)

`detect() -> Channel | None`, in this order, first verified wins:

| Channel | Env | Capture command | Inject |
|---|---|---|---|
| tmux | `TMUX_PANE` and `TMUX` | `tmux capture-pane -p -t $TMUX_PANE` | `tmux send-keys -t $TMUX_PANE C-u C-u C-k`, then `send-keys -t $TMUX_PANE -l "/compact"`, sleep 0.5, `send-keys -t $TMUX_PANE Enter` |
| zellij | `ZELLIJ` and `ZELLIJ_SESSION_NAME` | `zellij --session $ZELLIJ_SESSION_NAME action dump-screen <tmpfile>` | `action write 21 21 11` (C-u C-u C-k), `action write-chars "/compact"`, sleep 0.5, `action write 13` |
| wezterm | `WEZTERM_PANE` | `wezterm cli get-text --pane-id $WEZTERM_PANE` | `wezterm cli send-text --pane-id $WEZTERM_PANE --no-paste` with the same bytes |
| kitty | `KITTY_WINDOW_ID` and `KITTY_LISTEN_ON` | `kitten @ --to $KITTY_LISTEN_ON get-text --match id:$KITTY_WINDOW_ID` | `kitten @ --to ... send-text --match id:... <bytes>` |
| screen | `STY` | `screen -S $STY -X hardcopy <tmpfile>` | `screen -S $STY -X stuff <bytes>` |

`force_channel = none` disables all. Verification before typing, on every attempt:

1. The capture must succeed and contain a line whose stripped text starts with one of `pane_markers`, with a rule line (`───`) above and below it (Claude Code prompt box).
2. The capture must not contain `Enter to confirm`, `Esc to cancel`, `Do you want to`, `Yes, I trust` (dialogs).
3. Zellij dumps the focused pane. If the fingerprint fails, do not type. Log it.

After injecting, wait 3 s, capture again. Success = the prompt line no longer contains `/compact`, or the screen contains `Compacted` / `compact`. Return `True`/`False`. On the first tmux capture failure treat the channel as absent.

`inject_compact()` never sends anything if the fingerprint fails.

## 8. Cost (`cost.py`)

`price(model) -> (input, cache_read, write_1h, write_5m)` with prefix matching on the model id (`claude-fable-5-1`, `claude-opus-5`, ...). Unknown → opus-5 prices and `assumed=True`.
`cold_return_usd(tokens, model, ttl)` = tokens/1e6 × (write_1h if ttl == 3600 else write_5m).
`warm_compact_usd(tokens, model)` = tokens/1e6 × cache_read + 3000/1e6 × output price (fable 50, opus 25, sonnet-5 10, sonnet-4-6 15, haiku 5).

## 9. Plugin manifest and hooks

`plugin.json`: `name: "warmfold"`, `version: "0.1.0"`, `description`, `author`, `hooks: "./hooks/hooks.json"`, `commands: "./commands"`, `userConfig` as in section 3.

`hooks/hooks.json` uses exec form: `{"type":"command","command":"python3","args":["${CLAUDE_PLUGIN_ROOT}/scripts/warmfold.py","<event>"],"timeout":N}`. Verify in the docs that `${CLAUDE_PLUGIN_ROOT}` substitutes inside `args`. If it does not, use `command: "${CLAUDE_PLUGIN_ROOT}/scripts/warmfold.py"` with a `#!/usr/bin/env python3` shebang and `args: ["<event>"]`, and make the file executable.

Events wired: `Stop` → watch (async, asyncRewake, timeout 7200). `UserPromptSubmit` → prompt (timeout 10). `SessionStart` (matcher `startup|resume|clear|compact`) → session-start. `SessionEnd` → session-end (timeout 1). `Notification` (matcher `idle_prompt`) → notification. `PreCompact`, `PostCompact` → pre-compact, post-compact. `StopFailure` → stop-failure.

`settings-snippet.json`: the same `hooks` object with absolute paths replaced by `$HOME/.claude/plugins/...`? No: use `${CLAUDE_PLUGIN_ROOT}`-free paths documented in README as "replace `/path/to/warmfold`".

## 10. Integration tests (`tests/integration/`)

Real `claude` sessions in tmux, model `claude-haiku-4-5`, `--plugin-dir <repo>`, env overrides `WARMFOLD_IDLE_MINUTES=1 WARMFOLD_MIN_CONTEXT_TOKENS=1000 WARMFOLD_POLL_SECONDS=5 WARMFOLD_DATA_DIR=<tmp>`. A run script `run.sh <case>` with cases:

| Case | Setup | Assert |
|---|---|---|
| `compact-tmux` | claude inside tmux | transcript gets a `compact_boundary` with `trigger: manual` within 3 min after the turn end; state `phase = compact_sent` |
| `handoff-plain` | claude inside tmux started with `env -u TMUX -u TMUX_PANE`, so no channel | handoff file exists within 3 min; content has the headings; state `handoff_done` |
| `guard-cold` | as handoff-plain plus `WARMFOLD_FORCE_TTL_SECONDS=30`; wait 90 s; type a prompt | the pane shows the guard message; the prompt was not sent; a second submit passes |
| `clear-loads-handoff` | after handoff-plain, type `/clear`, then ask "what is the handoff goal" | the answer quotes the handoff |
| `status-local` | type `/warmfold:status` | the pane shows the report; no assistant message appears in the transcript |
| `zellij` | claude inside a zellij session (`zellij --session warmfold-test`) | same assertion as compact-tmux |

Each case creates the trust in the temp cwd first (the trust dialog appears on a new directory: send `Down Enter`). The first prompt of each session is `Say the word ready and stop.` to get a turn. Add a second prompt that makes the context larger than 1000 tokens (ask for a 600-word text).

## 11. Work packages

| WP | Owner | Scope | Depends on |
|---|---|---|---|
| WP1 core | implementer-core | `scripts/warmfold.py`, `warmfoldlib/{config,transcript,state,cost,log,events}.py`, unit tests with transcript fixtures | — |
| WP2 channels | implementer-channels | `warmfoldlib/channels.py` + `actions.py` (inject_compact, fingerprint), unit tests with fake captures | interface in section 7 |
| WP3 packaging | implementer-packaging | `plugin.json`, `marketplace.json`, `hooks.json`, `commands/status.md`, `settings-snippet.json`, `README.md` | sections 3, 9 |
| WP4 integration | implementer-integration | `tests/integration/run.sh` + helpers; runs the cases once WP1-3 land | sections 10 |

Interfaces between packages are the function names in sections 5-8. Each package must run `python3 -m pytest tests -q` green for its own tests.

## 12. Savings ledger and `/warmfold:savings`

Ledger file: `<data_dir>/ledger.jsonl`, one JSON object per line, append-only, written under the session lock.

Action record (written when the watcher acts):
```
{"type":"action","id":"<uuid>","ts":<epoch>,"action":"compact|handoff|keepalive","session_id":..,"cwd":..,
 "model":..,"context_tokens":N,"ttl_seconds":N,"cold_at":<epoch>,
 "paid_usd":<warm cost of the action>,"avoid_usd":<cold return cost of the context>}
```
`paid_usd` = `cost.warm_compact_usd` for compact and handoff (both are one warm read plus a short output);
keepalive = tokens × cache read price. `avoid_usd` = `cost.cold_return_usd`.

Outcome record (written once per action, at the first user return after the action):
```
{"type":"outcome","ref":"<action id>","ts":<epoch>,"session_id":..,"outcome":"realized|early|paid_cold","saved_usd":<float>}
```
Rules, evaluated in `prompt` (UserPromptSubmit) and `session-start` (matcher `clear`, when a handoff is consumed):
- `realized`: the return happened after `cold_at` of the action. `saved_usd = avoid_usd - paid_usd - rebuild`, where `rebuild` is the cold cost of the current (compacted or cleared) context when the return is also cold, else 0.
- `early`: the return happened before `cold_at`. The action was not needed. `saved_usd = -paid_usd`.
- `paid_cold` (handoff only): the user passed the guard and continued on the old context. `saved_usd = -paid_usd`.
Each action gets at most one outcome. Actions without an outcome are `pending`.

Report (`/warmfold:savings`, intercepted locally by the prompt hook, zero API cost; also `python3 warmfold.py savings`):
```
warmfold savings
                 today   last 7 days   all time
realized saved   $x.xx   $x.xx         $x.xx   (n actions)
wasted           $x.xx   $x.xx         $x.xx   (n actions)
net              $x.xx   $x.xx         $x.xx
pending actions  n       n             n
sessions         n       n             n
This session: <one line: last action, outcome, saved>
Estimates use list prices. A cold return is priced at the 1h or 5m cache-write rate.
```
"today" and "last 7 days" use local time. Amounts round to cents. The ledger is shared by all sessions on the machine (the data dir is global), so the report is cross-session.
