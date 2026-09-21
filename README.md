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

Open a session. Run `/warmfold:status`. Defaults work with no configuration. The line `6 userConfig options not yet set` after install is safe to ignore.

## What happens on each client

| Client | Idle action |
|---|---|
| Terminal in tmux | Compact in place |
| Terminal in zellij | Compact in place |
| Terminal in kitty | Compact in place (untested) |
| Terminal in WezTerm | Compact in place (untested) |
| Terminal in GNU screen | Compact in place (untested) |
| Plain terminal | Handoff |
| Claude Desktop app | Handoff |
| VS Code extension | Handoff |
| T3 Code | Handoff |

Return flow: type anything. The guard message appears. Run `/clear`. The handoff loads. Continue.

### Verified hosts

Live tests on 2026-09-21 with Claude Code 2.1.278 on Linux. Each test used a real session, a 1-minute idle threshold, and a 1000-token context threshold.

| Host | Action | Result |
|---|---|---|
| tmux 3.2a | compact in place (keystrokes) | pass, one compaction, no loop |
| zellij 0.45.1 | compact in place (keystrokes, `--pane-id`) | pass |
| Plain terminal (raw pty, no multiplexer) | handoff (wake) | pass |
| T3 Code desktop 0.0.39 nightly (Agent SDK) | handoff (wake) | pass, hooks loaded from the user-scope plugin |
| Cold return guard | block once, then pass | pass |
| `/clear` handoff loader | injects the saved handoff | pass |
| `/warmfold:status` | local report, no API call | pass |

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

Run `/warmfold:savings`. The plugin answers locally, with no API call.

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

## Mac self-check

1. Run `/config`. Set `idle_minutes` to `1` and `min_context_tokens` to `1000`.
2. Start a session. Ask for a 600-word answer. Stay idle for 2 minutes. Expect a handoff and a `.md` file under `~/.claude/plugins/data/warmfold-warmfold-local/handoffs/`.
3. Type anything. Expect the guard message once.
4. Run `/clear`. Expect the handoff content to load. Set both options back.

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
