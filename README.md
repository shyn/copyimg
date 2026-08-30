# copyimg

A [Codex CLI](https://github.com/openai/codex) plugin that copies the last assistant reply as a **rendered image** instead of raw Markdown — like the built-in `/copy`, but you get a PNG on your clipboard.

![demo](docs/demo.png)

## Usage

In any Codex session, after a reply you want to keep:

```
/copyimg
```

That's it. The reply is rendered to a PNG and placed on the system clipboard, ready to paste into Slack, Docs, WeChat, anywhere. The success (or failure) message is shown right in the session.

## How it works

Codex CLI has no user-defined local slash commands, so `copyimg` emulates one with a `UserPromptSubmit` lifecycle hook:

1. You type `/copyimg`. The hook intercepts the prompt before it reaches the model.
2. It parses the session transcript and extracts the last assistant message.
3. The Markdown is rendered to HTML and screenshotted full-page with a headless Chromium (via Playwright, 2x scale for retina displays).
4. The PNG is written to the clipboard — `osascript` on macOS, PowerShell on Windows.
5. The hook blocks the prompt, so **no tokens are spent** and nothing is sent to the model.

Any other prompt passes through untouched.

## Requirements

- Codex CLI with plugin + hook support (v0.128.0 or later recommended)
- Python 3 on the PATH — `python3` on macOS, the `py` launcher on Windows
- macOS or Windows

On first use the plugin creates an isolated venv in its plugin data directory and downloads a Chromium build (~180 MB). Your system Python is never touched. Subsequent runs take about a second.

## Install

```sh
# Add this repo as a marketplace
codex plugin marketplace add shyn/copyimg

# Install the plugin
codex plugin add copyimg@copyimg
```

Then restart Codex, open `/hooks`, and **review + trust** the copyimg hook (Codex skips untrusted plugin hooks by design).

To update later:

```sh
codex plugin marketplace upgrade copyimg
```

## Uninstall

```sh
codex plugin remove copyimg@copyimg
codex plugin marketplace remove copyimg
```

## Development

```
├── .agents/plugins/marketplace.json   # marketplace manifest
├── plugins/copyimg/
│   ├── .codex-plugin/plugin.json      # plugin manifest
│   └── hooks/
│       ├── hooks.json                 # UserPromptSubmit hook wiring
│       └── copyimg.py                 # intercept → render → clipboard
└── tests/
    ├── fake_rollout.jsonl             # sample Codex transcript
    └── smoke_test.py                  # end-to-end test, no Codex needed
```

Run the test suite locally (macOS and Windows):

```sh
python tests/smoke_test.py             # manifest + render tests
python tests/smoke_test.py --clipboard # also verify the clipboard write (macOS)
```

## Caveats

- The transcript (rollout jsonl) format is not a stable Codex interface. The parser accepts several known shapes, but a future Codex release may require an update here.
- Linux is not supported (no clipboard backend wired up) — contributions welcome.

## License

[MIT](LICENSE)
