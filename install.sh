#!/usr/bin/env bash
# Install or update the warmfold plugin (user scope).
# Re-running updates the managed clone. A checkout is used as is.
set -euo pipefail

MIN_CLAUDE="2.1.271"
INSTALL_DIR="$HOME/.claude/warmfold-src"
# Override with WARMFOLD_REPO_URL.
REPO_URL="${WARMFOLD_REPO_URL:-https://github.com/navaro1/warmfold.git}"

die() { echo "install.sh: $*" >&2; exit 1; }

command -v claude >/dev/null 2>&1 || die "claude is not in PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not in PATH"

version="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
[ -n "$version" ] || die "cannot read the claude version"
python3 - "$MIN_CLAUDE" "$version" <<'EOF' || die "Claude Code $MIN_CLAUDE or later is required. Found $version."
import sys
need, have = ([int(part) for part in value.split(".")] for value in sys.argv[1:3])
sys.exit(0 if have >= need else 1)
EOF

# Source: this checkout when the checkout holds the script, else a managed clone.
script_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
if [ -n "$script_dir" ] && [ -f "$script_dir/.claude-plugin/plugin.json" ]; then
  src="$script_dir"
else
  command -v git >/dev/null 2>&1 || die "git is not in PATH. It is required for the managed clone."
  if [ -d "$INSTALL_DIR/.git" ]; then
    remote="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    [ "$remote" = "$REPO_URL" ] || die "$INSTALL_DIR clones '$remote', not '$REPO_URL'. Inspect the directory and keep your local changes."
    git -C "$INSTALL_DIR" pull --ff-only || die "git pull failed in $INSTALL_DIR. Inspect the directory and keep your local changes. Fix it or move it aside, then re-run."
  elif [ -e "$INSTALL_DIR" ]; then
    die "$INSTALL_DIR already exists and is not a git clone. Inspect the directory and keep your local changes."
  else
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" || die "git clone failed from $REPO_URL"
  fi
  src="$INSTALL_DIR"
fi

# Validate before any change to the live install.
claude plugin validate --strict "$src" || die "validation failed for $src. The live install is unchanged."

# Refuse to repoint an existing marketplace declaration at a different path.
known="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/known_marketplaces.json"
if [ -f "$known" ]; then
  other="$(python3 - "$known" "$src" <<'EOF'
import json, os, sys
known_file, src = sys.argv[1], sys.argv[2]
try:
    with open(known_file, encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    sys.exit(0)
entry = data.get("warmfold-local")
if not isinstance(entry, dict):
    sys.exit(0)
values = entry.get("source") if isinstance(entry.get("source"), dict) else {}
path = values.get("path") or entry.get("installLocation") or ""
if path and os.path.realpath(path) != os.path.realpath(src):
    print(path)
EOF
)"
  [ -z "$other" ] || die "the marketplace warmfold-local already points to $other. Remove it first with: claude plugin marketplace remove warmfold-local --scope user"
fi

echo "Source: $src"
claude plugin marketplace add "$src"
claude plugin install warmfold@warmfold-local --scope user
echo "Installed. Restart open Claude Code sessions."
