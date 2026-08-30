#!/usr/bin/env python3
"""copyimg — Codex CLI UserPromptSubmit hook.

Typing `/copyimg` in a Codex session triggers this hook. It renders the last
assistant message from the session transcript to a PNG and copies the image
to the system clipboard (macOS / Windows), then blocks the prompt so it is
never sent to the model.

Non-matching prompts pass through silently (no stdout, zero token cost).

Third-party deps (playwright, markdown, chromium browser) are installed on
first use into an isolated venv under PLUGIN_DATA — nothing touches the
system Python.
"""

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

TRIGGER = "/copyimg"

DATA_DIR = Path(
    os.environ.get("PLUGIN_DATA") or (Path(tempfile.gettempdir()) / "copyimg")
)
VENV_DIR = DATA_DIR / "venv"
OUT_PNG = DATA_DIR / "copyimg.png"
INPUT_STASH = DATA_DIR / "hook-input.json"

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { background: #ffffff; }
body {
  width: 860px; margin: 0 auto; padding: 36px 44px;
  background: #ffffff; color: #1f2328;
  font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI",
        "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}
h1, h2, h3, h4 { margin: 1.1em 0 .5em; line-height: 1.3; }
h1 { font-size: 1.5em; border-bottom: 1px solid #d1d9e0; padding-bottom: .3em; }
h2 { font-size: 1.3em; border-bottom: 1px solid #eaeef2; padding-bottom: .25em; }
h3 { font-size: 1.15em; }
p { margin: .6em 0; }
code {
  font: .92em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: #eff1f3; padding: .15em .4em; border-radius: 6px;
}
pre {
  background: #f6f8fa; border-radius: 8px; padding: 14px 16px;
  overflow-x: auto;
}
pre code { background: none; padding: 0; }
blockquote {
  margin: .8em 0; padding: 0 1em; color: #59636e;
  border-left: 4px solid #d1d9e0;
}
ul, ol { padding-left: 1.6em; margin: .6em 0; }
li { margin: .25em 0; }
table { border-collapse: collapse; margin: .8em 0; }
th, td { border: 1px solid #d1d9e0; padding: 6px 12px; }
th { background: #f6f8fa; }
hr { border: none; border-top: 1px solid #d1d9e0; margin: 1.4em 0; }
a { color: #0969da; text-decoration: none; }
img { max-width: 100%; }
</style>
</head>
<body class="markdown-body"><!--BODY--></body>
</html>
"""


def block(reason):
    """Stop the prompt from reaching the model and show `reason` to the user."""
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def read_hook_input():
    """Hook payload comes from stdin, or from the stash file after a re-run."""
    stash = os.environ.get("COPYIMG_INPUT_FILE")
    if stash:
        return json.loads(Path(stash).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def venv_python():
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_runtime(hook_input):
    """Make sure playwright/markdown are importable; bootstrap a venv if not."""
    try:
        import markdown  # noqa: F401
        import playwright.sync_api  # noqa: F401

        return
    except ImportError:
        pass

    if os.environ.get("COPYIMG_BOOTSTRAPPED") == "1":
        block(
            "copyimg: 依赖安装失败。请删除目录后重试: %s" % VENV_DIR
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_STASH.write_text(
        json.dumps(hook_input, ensure_ascii=False), encoding="utf-8"
    )
    py = venv_python()
    if not py.exists():
        print(
            "copyimg: first run — creating isolated env (venv + playwright + chromium)...",
            file=sys.stderr,
        )
        try:
            subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
            subprocess.check_call(
                [str(py), "-m", "pip", "install", "-q", "playwright", "markdown"]
            )
            subprocess.check_call(
                [str(py), "-m", "playwright", "install", "chromium"]
            )
        except subprocess.CalledProcessError as exc:
            block("copyimg: 初始化失败 (%s)。请检查网络后重试。" % exc)

    env = dict(os.environ)
    env["COPYIMG_BOOTSTRAPPED"] = "1"
    env["COPYIMG_INPUT_FILE"] = str(INPUT_STASH)
    completed = subprocess.run([str(py), os.path.abspath(__file__)], env=env)
    sys.exit(completed.returncode)


def extract_text(payload):
    content = payload.get("content")
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                if part.get("text"):
                    parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
    elif isinstance(content, str):
        parts.append(content)
    return "\n".join(parts).strip()


def last_assistant_message(transcript_path):
    """Best-effort parse of the rollout jsonl for the last assistant text.

    The transcript format is not a stable interface, so several known shapes
    are accepted. Returns None when nothing usable is found.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.is_file():
        return None

    last = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        candidates = [record]
        payload = record.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload)

        for cand in candidates:
            role = cand.get("role")
            ctype = cand.get("type")
            if role == "assistant" and ctype in (None, "message"):
                text = extract_text(cand)
                if text:
                    last = text
            elif ctype == "agent_message" and isinstance(cand.get("message"), str):
                text = cand["message"].strip()
                if text:
                    last = text
    return last


def render_png(markdown_text, out_path):
    import markdown
    from playwright.sync_api import sync_playwright

    body = markdown.markdown(
        markdown_text, extensions=["fenced_code", "tables", "sane_lists"]
    )
    html = HTML_TEMPLATE.replace("<!--BODY-->", body)

    out_path = Path(out_path)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 940, "height": 600}, device_scale_factor=2
        )
        page = context.new_page()
        page.set_content(html, wait_until="load")
        page.screenshot(path=str(out_path), full_page=True)
        browser.close()
    return out_path


def copy_to_clipboard(png_path):
    png_path = Path(png_path).resolve()
    system = platform.system()
    if system == "Darwin":
        subprocess.check_call(
            [
                "osascript",
                "-e",
                'set the clipboard to (read (POSIX file "%s") as «class PNGf»)'
                % png_path,
            ]
        )
    elif system == "Windows":
        ps_path = str(png_path).replace("'", "''")
        ps_command = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$img = [System.Drawing.Image]::FromFile('%s'); "
            "[System.Windows.Forms.Clipboard]::SetImage($img); "
            "$img.Dispose()" % ps_path
        )
        subprocess.check_call(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", ps_command]
        )
    else:
        raise RuntimeError("仅支持 macOS / Windows，当前系统: %s" % system)


def main():
    try:
        hook_input = read_hook_input()
    except Exception:
        return  # malformed input — stay silent, let the prompt through

    prompt = (hook_input.get("prompt") or "").strip()
    if prompt != TRIGGER:
        return  # not ours — silent pass-through

    ensure_runtime(hook_input)

    text = last_assistant_message(hook_input.get("transcript_path"))
    if not text:
        block("copyimg: 在 transcript 中没有找到上一条 assistant 回复。")

    try:
        render_png(text, OUT_PNG)
        if os.environ.get("COPYIMG_NO_CLIPBOARD") == "1":
            block("✅ 已渲染为图片 (COPYIMG_NO_CLIPBOARD=1，未写剪贴板): %s" % OUT_PNG)
        copy_to_clipboard(OUT_PNG)
    except Exception as exc:
        block("copyimg 失败: %s" % exc)

    block("✅ 已渲染为图片并复制到剪贴板: %s" % OUT_PNG)


if __name__ == "__main__":
    main()
