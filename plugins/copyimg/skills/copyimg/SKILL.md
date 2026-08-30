---
name: copyimg
description: Copy the last assistant reply as a rendered PNG image to the clipboard. Local command, zero tokens.
---

[copyimg:copy-last-response-as-image]

This prompt is a local command handled by the copyimg plugin's
UserPromptSubmit hook: the hook renders the previous assistant reply to a
PNG, copies it to the system clipboard, and blocks this prompt before it
reaches the model.

If you (the model) are reading this, the hook did not fire — most likely it
has not been trusted yet. Do not attempt the task yourself. Tell the user to
open `/hooks`, review and trust the copyimg hook, then run `$copyimg` again.
