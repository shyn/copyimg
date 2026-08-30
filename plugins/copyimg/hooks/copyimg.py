#!/usr/bin/env python3
"""copyimg — Codex CLI UserPromptSubmit hook.

Typing `/copyimg` in a Codex session triggers this hook. It renders the last
assistant message from the session transcript to a PNG and copies the image
to the system clipboard (macOS / Windows), then blocks the prompt so it is
never sent to the model.

Zero third-party dependencies: pure Python stdlib plus a system browser
(Chrome / Edge / Chromium) driven via its headless CLI. Markdown is rendered
in-page by the vendored `marked.min.js` next to this script.

Non-matching prompts pass through silently (no stdout, zero token cost).
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Trigger forms. The Codex TUI rejects unknown `/`-prefixed commands locally
# ("/copyimg" never reaches the hook), so the user-facing entry point is the
# bundled skill. In the composer that shows up as `$copyimg:copyimg`
# ($<plugin>:<skill>) and is submitted verbatim — the skill body is expanded
# as a *separate* follow-up user message, so the hook must match the raw
# reference form, not just the expansion marker.
TRIGGERS = {"copyimg", "/copyimg", "$copyimg", "$copyimg:copyimg", "copyimg:copyimg"}
SKILL_MARKER = "[copyimg:copy-last-response-as-image]"
HOOK_DIR = Path(__file__).resolve().parent
MARKED_JS = HOOK_DIR / "marked.min.js"

DATA_DIR = Path(
    os.environ.get("PLUGIN_DATA") or (Path(tempfile.gettempdir()) / "copyimg")
)
OUT_PNG = DATA_DIR / "copyimg.png"
OUT_HTML = DATA_DIR / "copyimg.html"

VIEWPORT_WIDTH = 940
MAX_HEIGHT = 16000

CSS = """
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
"""

# The page renders the markdown, then reports its full height via <title> so
# the measure pass can read it back from --dump-dom output.
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>0</title>
<style>__CSS__</style>
</head>
<body class="markdown-body"><div id="content"></div>
<script>__MARKED__</script>
<script>
document.getElementById("content").innerHTML = marked.parse(__MARKDOWN_JSON__);
document.title = String(Math.ceil(document.documentElement.scrollHeight));
</script>
</body>
</html>
"""


def block(reason):
    """Stop the prompt from reaching the model and show `reason` to the user."""
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def find_browser():
    """Locate a system Chromium-family browser. Returns a path or None."""
    candidates = []
    if sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif os.name == "nt":
        for env_var in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
            base = os.environ.get(env_var)
            if base:
                candidates += [
                    os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                    os.path.join(base, r"Google\Chrome\Application\chrome.exe"),
                ]
    for name in ("chrome", "google-chrome", "msedge", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            candidates.append(path)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


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


def build_html(markdown_text):
    marked_source = MARKED_JS.read_text(encoding="utf-8")
    # Escape "</" so the embedded JSON can't terminate the script element.
    markdown_json = json.dumps(markdown_text, ensure_ascii=False).replace("</", "<\\/")
    return (
        HTML_TEMPLATE.replace("__CSS__", CSS)
        .replace("__MARKED__", marked_source)
        .replace("__MARKDOWN_JSON__", markdown_json)
    )


def tail_contains(path, needle, tail_bytes=8192):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail_bytes))
            return needle in fh.read()
    except OSError:
        return False


def file_stable(path, interval=0.3):
    try:
        size = Path(path).stat().st_size
        if size == 0:
            return False
        time.sleep(interval)
        return Path(path).stat().st_size == size
    except OSError:
        return False


def run_browser(browser, extra_args, html_uri, dom_out=None, expect=None, timeout=60):
    """Launch the browser one-shot and reap it once the artifact is ready.

    One-shot headless Chrome/Edge frequently never exits (helpers outlive the
    main process, background services delay shutdown), so instead of waiting
    for exit we poll the output artifact and kill the browser once it is
    complete. A fresh profile per run avoids singleton locks left behind by
    killed helpers.
    """
    profile = Path(tempfile.mkdtemp(prefix="copyimg-profile-", dir=DATA_DIR))
    args = [
        browser,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-breakpad",
        "--disable-field-trial-config",
        "--disable-hang-monitor",
        "--disable-sync",
        "--disable-extensions",
        "--disable-default-apps",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--user-data-dir=%s" % profile,
    ] + extra_args + [html_uri]
    stdout_fh = (
        open(dom_out, "w", encoding="utf-8")
        if dom_out is not None
        else open(os.devnull, "w")
    )
    try:
        proc = subprocess.Popen(args, stdout=stdout_fh, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if expect and expect():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    finally:
        stdout_fh.close()
        shutil.rmtree(profile, ignore_errors=True)


def render_png(markdown_text, out_path):
    browser = find_browser()
    if not browser:
        raise RuntimeError(
            "未找到 Chrome / Edge / Chromium，请先安装其中之一"
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(build_html(markdown_text), encoding="utf-8")
    html_uri = OUT_HTML.as_uri()

    # Pass 1: measure the rendered height (reported back via <title>).
    # --dump-dom writes the serialized DOM in one final write.
    # Remove stale artifacts first: the expect() polls below would otherwise
    # see the previous run's output and kill the browser before it writes.
    dom_path = DATA_DIR / "dom.html"
    dom_path.unlink(missing_ok=True)
    run_browser(
        browser,
        ["--dump-dom", "--window-size=%d,600" % VIEWPORT_WIDTH],
        html_uri,
        dom_out=dom_path,
        expect=lambda: tail_contains(dom_path, b"</html>"),
    )
    dom = dom_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"<title>(\d+)</title>", dom)
    height = int(match.group(1)) if match else 1200
    height = max(100, min(height, MAX_HEIGHT))

    # Pass 2: screenshot the page at the measured height, 2x for retina.
    out_png = Path(out_path).resolve()
    out_png.unlink(missing_ok=True)
    run_browser(
        browser,
        [
            "--force-device-scale-factor=2",
            "--screenshot=%s" % out_png,
            "--window-size=%d,%d" % (VIEWPORT_WIDTH, height),
        ],
        html_uri,
        expect=lambda: file_stable(out_png),
    )
    if not out_png.is_file() or out_png.stat().st_size == 0:
        raise RuntimeError("浏览器截图失败: %s" % browser)
    return out_png


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
        hook_input = json.load(sys.stdin)
    except Exception:
        return  # malformed input — stay silent, let the prompt through

    prompt = (hook_input.get("prompt") or "").strip()
    if prompt not in TRIGGERS and SKILL_MARKER not in prompt:
        return  # not ours — silent pass-through

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
