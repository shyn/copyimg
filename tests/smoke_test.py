#!/usr/bin/env python3
"""End-to-end smoke test for the copyimg hook — no Codex session needed.

Usage:
    python tests/smoke_test.py                # manifest + render tests
    python tests/smoke_test.py --clipboard    # additionally verify the real
                                              # clipboard write (macOS only)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "copyimg"
HOOK = PLUGIN / "hooks" / "copyimg.py"
TRANSCRIPT = Path(__file__).parent / "fake_rollout.jsonl"


def run_hook(payload, plugin_data, extra_env=None):
    env = dict(os.environ, PLUGIN_DATA=str(plugin_data))
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )


def base_payload(**overrides):
    payload = {
        "session_id": "smoke",
        "transcript_path": str(TRANSCRIPT),
        "cwd": str(REPO),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "$copyimg",
    }
    payload.update(overrides)
    return payload


TRIGGER_FORMS = [
    "$copyimg:copyimg",  # the actual form the TUI composer submits ($plugin:skill)
    "$copyimg",
    "copyimg",           # bare word fallback
    "/copyimg",          # legacy form
    "text [copyimg:copy-last-response-as-image] text",  # expanded skill body
]


def parse_block_json(result):
    """The hook's JSON response is the last non-empty line of its stdout."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "hook produced no stdout (stderr: %s)" % result.stderr[-500:]
    return json.loads(lines[-1])


def validate_manifests():
    plugin = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert plugin["name"] == "copyimg" and plugin["version"]

    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    handler = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert handler["type"] == "command" and "copyimg.py" in handler["command"]
    assert HOOK.is_file()

    marketplace = json.loads(
        (REPO / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    entry = marketplace["plugins"][0]
    assert entry["name"] == "copyimg"
    assert (REPO / entry["source"]["path"]).is_dir(), "source.path must resolve"

    skill = PLUGIN / "skills" / "copyimg" / "SKILL.md"
    assert skill.is_file(), "bundled skill missing"
    assert plugin.get("skills"), "plugin.json must declare the skills dir"
    assert "[copyimg:copy-last-response-as-image]" in skill.read_text(
        encoding="utf-8"
    ), "skill body must contain the trigger marker"
    print("manifest validation ok")


def main():
    with_clipboard = "--clipboard" in sys.argv
    validate_manifests()

    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)

        # Non-matching prompts must pass through silently (no stdout).
        result = run_hook(base_payload(prompt="hello"), data)
        assert result.returncode == 0 and result.stdout == "", result
        print("pass-through ok")

        # Missing transcript -> blocked with an explanation, exit 0.
        result = run_hook(base_payload(transcript_path=str(data / "nope.jsonl")), data)
        out = parse_block_json(result)
        assert result.returncode == 0 and out["decision"] == "block", result.stdout
        print("missing-transcript ok")

        # Every trigger form must be recognized. Checked against a missing
        # transcript so these run fast (no render).
        for form in TRIGGER_FORMS:
            result = run_hook(
                base_payload(prompt=form, transcript_path=str(data / "nope.jsonl")),
                data,
            )
            out = parse_block_json(result)
            assert result.returncode == 0 and out["decision"] == "block", (
                "trigger %r not recognized" % form
            )
        print("trigger forms ok: %s" % ", ".join(TRIGGER_FORMS))

        # Full render. Clipboard write is skipped unless --clipboard was given.
        env = {} if with_clipboard else {"COPYIMG_NO_CLIPBOARD": "1"}
        result = run_hook(base_payload(), data, env)
        out = parse_block_json(result)
        assert result.returncode == 0 and out["decision"] == "block", result.stdout
        png = data / "copyimg.png"
        assert png.is_file() and png.stat().st_size > 10_000, "PNG missing or empty"
        print("render ok: %d bytes" % png.stat().st_size)

        if with_clipboard:
            assert sys.platform == "darwin", "--clipboard is only supported on macOS"
            info = subprocess.run(
                ["osascript", "-e", "clipboard info"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert "PNGf" in info, info
            print("clipboard ok: %s" % info.strip())

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
