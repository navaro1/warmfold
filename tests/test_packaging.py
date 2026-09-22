import json
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_json(relative_path):
    path = os.path.join(ROOT, relative_path)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_manifest_uses_implicit_default_hooks_file():
    manifest = read_json(".claude-plugin/plugin.json")
    assert "hooks" not in manifest
    assert os.path.isfile(os.path.join(ROOT, "hooks", "hooks.json"))


def test_hook_commands_use_plugin_root_and_keep_exec_form():
    hooks = read_json("hooks/hooks.json")["hooks"]
    seen = []
    for entries in hooks.values():
        for entry in entries:
            for hook in entry["hooks"]:
                seen.append(hook)
                assert hook["type"] == "command"
                assert hook["command"] == "python3"
                assert hook["args"][:1] == [
                    "${CLAUDE_PLUGIN_ROOT}/scripts/warmfold.py"
                ]
    assert len(seen) == 8


def test_hook_entrypoint_is_executable():
    path = os.path.join(ROOT, "scripts", "warmfold.py")
    assert os.access(path, os.X_OK)
