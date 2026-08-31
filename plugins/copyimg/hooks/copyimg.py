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
HIGHLIGHT_JS = HOOK_DIR / "highlight.min.js"

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
html {
  background: linear-gradient(135deg, #dbeafe 0%, #ede9fe 50%, #fce7f3 100%);
}
body {
  margin: 0; padding: 48px 56px;
  color: #1f2937;
  font: 15.5px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI",
        "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.card {
  background: #ffffff;
  border-radius: 18px;
  box-shadow: 0 24px 60px rgba(15, 23, 42, .16), 0 2px 8px rgba(15, 23, 42, .08);
  padding: 44px 52px;
}
.card > :first-child { margin-top: 0; }
.card > :last-child { margin-bottom: 0; }
h1, h2, h3, h4 {
  margin: 1.25em 0 .55em; line-height: 1.3; color: #111827;
  font-weight: 700; letter-spacing: -.01em;
}
h1 { font-size: 1.6em; padding-bottom: .35em; border-bottom: 1px solid #eef0f4; }
h2 { font-size: 1.32em; padding-bottom: .3em; border-bottom: 1px solid #f2f4f7; }
h3 { font-size: 1.14em; }
p { margin: .65em 0; }
strong { color: #111827; }
code {
  font: .88em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: #f1f5f9; color: #be185d;
  padding: .15em .42em; border-radius: 6px;
  border: 1px solid #e2e8f0;
}
pre {
  margin: .9em 0;
  background: #0d1117; color: #e6edf3;
  border-radius: 12px; padding: 14px 18px 16px;
  overflow-x: auto;
  box-shadow: 0 8px 24px rgba(15, 23, 42, .18);
}
pre::before {
  content: ""; display: block; height: 12px; margin: 0 0 12px;
  background-image:
    radial-gradient(circle at 8px 6px, #ff5f57 5.5px, rgba(0,0,0,0) 6.5px),
    radial-gradient(circle at 28px 6px, #febc2e 5.5px, rgba(0,0,0,0) 6.5px),
    radial-gradient(circle at 48px 6px, #28c840 5.5px, rgba(0,0,0,0) 6.5px);
  background-repeat: no-repeat;
}
pre code { background: none; border: none; color: inherit; padding: 0; font-size: .92em; }
blockquote {
  margin: .9em 0; padding: 12px 18px;
  background: #f8fafc; border-left: 4px solid #818cf8;
  border-radius: 0 10px 10px 0; color: #475569;
}
blockquote > :first-child { margin-top: 0; }
blockquote > :last-child { margin-bottom: 0; }
ul, ol { padding-left: 1.55em; margin: .65em 0; }
li { margin: .3em 0; }
li::marker { color: #818cf8; }
table {
  border-collapse: collapse; width: 100%; margin: 1em 0; font-size: .94em;
  border-radius: 10px; overflow: hidden; box-shadow: 0 0 0 1px #e5e7eb;
}
th, td { padding: 9px 14px; text-align: left; border-bottom: 1px solid #eef0f3; }
th { background: #f8fafc; font-weight: 600; color: #111827; }
tbody tr:nth-child(even) { background: #fafbfc; }
tr:last-child td { border-bottom: none; }
hr {
  border: none; height: 1px; margin: 1.7em 0;
  background: linear-gradient(90deg, rgba(0,0,0,0), #d8dee6, rgba(0,0,0,0));
}
a { color: #4f46e5; text-decoration: none; border-bottom: 1px solid #c7d2fe; }
img { max-width: 100%; border-radius: 10px; }
/* highlight.js — github-dark palette */
.hljs-comment, .hljs-quote { color: #8b949e; font-style: italic; }
.hljs-keyword, .hljs-selector-tag, .hljs-doctag, .hljs-template-tag { color: #ff7b72; }
.hljs-string, .hljs-regexp, .hljs-meta .hljs-string { color: #a5d6ff; }
.hljs-number, .hljs-literal { color: #79c0ff; }
.hljs-title, .hljs-title.function_, .hljs-section { color: #d2a8ff; }
.hljs-title.class_, .hljs-type, .hljs-built_in { color: #ffa657; }
.hljs-attr, .hljs-attribute, .hljs-variable, .hljs-template-variable,
.hljs-selector-id, .hljs-selector-class { color: #79c0ff; }
.hljs-name, .hljs-selector-pseudo, .hljs-tag { color: #7ee787; }
.hljs-meta, .hljs-symbol, .hljs-bullet, .hljs-link { color: #f2cc60; }
.hljs-emphasis { font-style: italic; }
.hljs-strong { font-weight: bold; }
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
<body class="markdown-body"><div class="card" id="content"></div>
<script>__MARKED__</script>
<script>__HIGHLIGHT__</script>
<script>
document.getElementById("content").innerHTML = marked.parse(__MARKDOWN_JSON__);
document.querySelectorAll("#content pre code").forEach(function (el) {
  hljs.highlightElement(el);
});
// Measure the card's bottom edge rather than document scrollHeight — some
// headless builds (Edge on Windows) enforce a minimum viewport height and
// would report dead space below the card. +48 matches body's bottom padding.
var bottom = document.getElementById("content").getBoundingClientRect().bottom;
document.title = String(Math.ceil(bottom + 48));
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
    highlight_source = HIGHLIGHT_JS.read_text(encoding="utf-8")
    # Escape "</" so the embedded JSON can't terminate the script element.
    markdown_json = json.dumps(markdown_text, ensure_ascii=False).replace("</", "<\\/")
    return (
        HTML_TEMPLATE.replace("__CSS__", CSS)
        .replace("__MARKED__", marked_source)
        .replace("__HIGHLIGHT__", highlight_source)
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


def force_unlink(path, attempts=10):
    """Unlink, tolerating a briefly lingering handle from a killed browser."""
    for _ in range(attempts):
        try:
            Path(path).unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.3)
    Path(path).unlink(missing_ok=True)


def kill_process_tree(proc):
    """Kill the browser and all its helper processes.

    proc.kill() only terminates the root process; on Windows the Chromium
    helpers survive and keep inherited handles (e.g. the --dump-dom stdout
    file) open, which makes the next run's unlink fail with WinError 32.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.kill()


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
            kill_process_tree(proc)
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
    force_unlink(dom_path)
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
    force_unlink(out_png)
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
