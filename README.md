# warmfold

warmfold is a Claude Code plugin that compacts or hands off an idle session before the prompt cache expires.

| One event on a 500,000-token Fable 5.1 session | Cost |
|---|---|
| Cold return after the cache expires (1-hour cache write) | about $10 |
| Warm compaction by the plugin (cache read + summary) | about $0.30 |

## Install

Requirements: Claude Code 2.1.271 or later, `python3`, macOS or Linux. The config `options` need 2.1.271. The remote install also needs `git` and `curl`.

### One command

```
curl -fsSL https://raw.githubusercontent.com/navaro1/warmfold/main/install.sh -o /tmp/warmfold-install.sh && bash /tmp/warmfold-install.sh
```

From a checkout, run:

```
bash /path/to/warmfold/install.sh
```

The script uses the checkout in place when the checkout holds the script. Otherwise it clones or updates `~/.claude/warmfold-src` with `git`. It validates the source before it changes the install. Re-running updates the managed clone. A checkout is used as is.

### Manual

```
claude plugin marketplace add /path/to/warmfold
claude plugin install warmfold@warmfold-local --scope user
claude plugin validate /path/to/warmfold
```

### Ask your agent

> Install the warmfold plugin: run `bash /path/to/warmfold/install.sh`, then confirm with `claude plugin list`.

### Verify

```
claude plugin list | grep warmfold
```

For T3 Code, enroll the plugin's native local dispatch credential when first
setting it up, then check its non-secret readiness report. Hooks may renew a
near-expiry enrolled bearer only after confirming the local T3 session is
active; expired, revoked, malformed, or offline credentials require this
explicit setup again.

```
python3 /path/to/warmfold/scripts/warmfold.py t3-setup
python3 /path/to/warmfold/scripts/warmfold.py t3-status
```

Open a session. Run `/warmfold:status`. Defaults work with no configuration. The line `6 userConfig options not yet set` after install is safe to ignore.

## What happens on each client

| Client | Idle action |
|---|---|
| Terminal in tmux | Compact in place |
| Terminal in zellij | Compact in place |
| Terminal in kitty | Compact in place (untested) |
| Terminal in WezTerm | Compact in place (untested) |
| Terminal in GNU screen | Compact in place (untested) |
| Apple Terminal.app | Compact in the exact Claude tab (macOS) |
| Ghostty | Compact in the exact terminal identified by a temporary title nonce (macOS) |
| Plain terminal without a supported terminal API | Handoff |
| Claude Desktop app | Handoff |
| VS Code extension | Handoff |
| T3 Code | Compact in place through its native local orchestration API (after `t3-setup`) |

Return flow: type anything. The guard message appears. Run `/clear`. The handoff loads. Continue.

### Verified hosts

Live tests on 2026-09-21 with Claude Code 2.1.278 on Linux. Each test used a real session, a 1-minute idle threshold, and a 1000-token context threshold.

| Host | Action | Result |
|---|---|---|
| tmux 3.2a | compact in place (keystrokes) | pass, one compaction, no loop |
| zellij 0.45.1 | compact in place (keystrokes, `--pane-id`) | pass |
| Plain terminal (raw pty, no multiplexer) | handoff (wake) | pass |
| T3 Code desktop 0.0.43 nightly (Agent SDK) | native `/compact` dispatch | proof reduced the context from 30940 to 6082 tokens; hooks loaded from the user-scope plugin |
| Cold return guard | block once, then pass | pass |
| `/clear` handoff loader | injects the saved handoff | pass |
| `/warmfold:status` | local report rendered by the normal Claude response path | pass |

## Configure

The plugin shows these options in `/config`:

| Key | Default | Meaning |
|---|---|---|
| `idle_minutes` | 30 | Idle minutes before the watcher acts. |
| `min_context_tokens` | 100000 | Contexts below this size are ignored. |
| `mode` | `auto` | `auto`, `compact`, `handoff`, `keepalive`, or `warn`. |
| `guard` | true | Block the first prompt on a cold return. |
| `ttl_5m_policy` | `warn` | `warn`, `compact_at_4m`, or `off`. Applies to a 5-minute cache. |
| `keepalive_hours` | 0 | Maximum keepalive hours. 0 in keepalive mode means an immediate handoff. |

Advanced keys. Set them in `~/.claude/plugins/data/warmfold-warmfold-local/config.json` or as `WARMFOLD_<KEY>` environment variables. `/warmfold:status` prints `Data dir:` with the active directory:

| Key | Default | Meaning |
|---|---|---|
| `safety_margin_minutes` | 5 | Act at least this long before cache expiry. |
| `guard_min_usd` | 1.0 | Guard only above this rebuild cost. |
| `guard_ack_seconds` | 120 | A second submit inside this window passes. |
| `handoff_autoload` | `clear` | `clear`, `clear+startup`, or `off`. |
| `handoff_max_age_hours` | 24 | Ignore older handoffs. |
| `poll_seconds` | 15 | Watcher poll interval. |
| `pane_markers` | `❯ ` | Prompt markers the watcher looks for. |
| `data_dir` | `$CLAUDE_PLUGIN_DATA`, else `~/.claude/warmfold` | State, handoffs, log. |
| `force_ttl_seconds` | 0 | Test only. Overrides the TTL. |
| `force_channel` | empty | Test only. `none` disables terminal typing. |

Precedence: `WARMFOLD_<KEY>` env > `$CLAUDE_PLUGIN_DATA/config.json` > plugin option (`/config`) > default.

## TTL detection

The plugin reads the session TTL in this order. First match wins.

| Order | Source | Value |
|---|---|---|
| 1 | Transcript evidence (`ephemeral_1h_input_tokens`, `ephemeral_5m_input_tokens`) | 3600 s or 300 s |
| 2 | `FORCE_PROMPT_CACHING_5M` set to `1`, `true`, `yes`, or `on` | 300 s |
| 3 | `CLAUDE_CODE_PROMPT_CACHE_TTL` set to exactly `5m` or `1h` | 300 s or 3600 s |
| 4 | `promptCacheTtl` in `<project>/.claude/settings.local.json`, then `<project>/.claude/settings.json`, then `~/.claude/settings.json` | 300 s or 3600 s |
| 5 | `ENABLE_PROMPT_CACHING_1H` set to `1`, `true`, `yes`, or `on` | 3600 s |
| 6 | Auth mode | API key or cloud provider: 300 s. Subscription: 3600 s. Unknown: 300 s |

Rows 2 and 5 match any letter case. To keep 1 hour on an API key, set `"promptCacheTtl": "1h"` in `~/.claude/settings.json`.

## Savings

Run `/warmfold:savings`. The hook computes the report locally and supplies it
to Claude as read-only context after the normal prompt bookkeeping. Claude
then renders it through a normal response, so the command uses the session's
usual API request and appears in the transcript.

- realized: the return came after the cache expired. The action avoided the cold rebuild.
- wasted: the return came early, or the guard passed on the old context. The action cost more than it saved.
- pending: the plugin acted, and no return happened yet.

Estimates use list prices. The ledger is `<data_dir>/ledger.jsonl`.

## Limits

- Contexts under 100,000 tokens: ignored.
- A 5-minute TTL: warn only, by default.
- No action during a permission prompt or an interrupted turn.
- zellij types into the focused pane. The fingerprint check guards it.
- kitty, WezTerm, GNU screen: untested.
- macOS Desktop app: untested. Run the Mac self-check.

The macOS terminal channels use native terminal scripting. Apple Terminal is
matched by the controlling tty of the Claude process. Ghostty receives a
short-lived title nonce on that same tty; the plugin then selects the one
Ghostty terminal whose native title contains that nonce. It captures that
terminal through Ghostty's `write_screen_file:copy` action, validates the
fresh temporary file before reading it, and restores every advertised
pasteboard type only when the native change count still matches the plugin's
capture path. The plugin
never focuses a window or sends a global keyboard event. If the target,
screen capture, or prompt fingerprint cannot be verified exactly, it falls
back to the handoff flow.

## Mac self-check

1. Start a fresh temporary workspace with the isolated native smoke command
   shown below. Set `idle_minutes` to `1` and `min_context_tokens` to `1000`.
2. Send a small prompt, wait for the turn to stop, and stay idle for about 2
   minutes. Expect exactly one `compact_boundary`, `phase=compact_sent`, and
   no handoff reminder. Repeat once in Apple Terminal and once in Ghostty.
3. Before the Ghostty run, copy rich text. Confirm the clipboard contents and
   types are unchanged after the screen capture.

The native Terminal/Ghostty path has not been GUI-live-tested by this agent;
the desktop automation policy blocks controlling those apps. macOS may ask
for Automation permission for the selected terminal in System Settings →
Privacy & Security → Automation.

```sh
# Run this snippet from the warmfold checkout.
repo="$(git rev-parse --show-toplevel)"
d="$(mktemp -d /tmp/warmfold-native.XXXXXX)"
mkdir -p "$d/cwd" "$d/data"
cd "$d/cwd" || exit 1
WARMFOLD_DATA_DIR="$d/data" \
WARMFOLD_IDLE_MINUTES=1 \
WARMFOLD_MIN_CONTEXT_TOKENS=1000 \
WARMFOLD_POLL_SECONDS=1 \
WARMFOLD_FORCE_TTL_SECONDS=3600 \
WARMFOLD_GUARD=0 \
claude --plugin-dir "$repo"
```

## Troubleshoot

- Log: `<data_dir>/log/warmfold.log`.
- Status: run `/warmfold:status`.
- Hooks loaded: run `/hooks`.
- Debug: run `claude --debug-file /tmp/cc.log`, then `grep -i warmfold /tmp/cc.log`.
- Disable: `claude plugin disable warmfold@warmfold-local`.
- Uninstall:

```
claude plugin uninstall warmfold@warmfold-local --keep-data
claude plugin marketplace remove warmfold-local --scope user
```

`--keep-data` keeps the config, state, log, and handoffs.

## Development

```
python3 -m pytest tests -q
bash tests/integration/run.sh compact-tmux
```

DESIGN.md holds the design and the test cases.
