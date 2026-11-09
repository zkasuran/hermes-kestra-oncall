"""Render assets/01-chat-diagnosis.png from recorded data. Nothing in the picture is typed by hand.

Inputs (all read-only):
  --state-db     a copy of the rig's Hermes state.db (the webhook session with the model's reply)
  --session      the webhook session id whose last assistant message is the reply
  --typed-output the JSON that tests/e2e/hermes_driver.py printed for `command "apply <id> <hash>"`
                 (the plugin command handler's real reply to the typed approval)
  --typed-text   the command text that was typed, exactly as passed to the driver
  --store-db     a copy of the plugin's SQLite store (the stored result of the apply)
  --incident     the incident id
The page is a plain HTML rendering of those strings in the dashboard colours. It is not a screenshot of a chat
client: the rig has no chat platform, and the command was run through the plugin's command handler by the test
driver. The image says so in its footer.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from pathlib import Path

CSS = """
:root{--bg:#041c1c;--card:#0a2a2a;--line:#1d4545;--fg:#ffe6cb;--mut:#8fb0a8;--add:#143f33;--del:#4a2424;--ok:#3fb27f;--warn:#d9a441}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 Inter,system-ui,sans-serif;width:1600px;height:1000px;overflow:hidden;position:relative}
.top{padding:14px 28px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;color:var(--mut);font-size:13px}
.wrap{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:24px;padding:22px 28px}
.col{display:flex;flex-direction:column;gap:14px}
.who{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.bubble{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px}
.bubble p{margin:0 0 10px}.bubble p:last-child{margin:0}
code{font-family:ui-monospace,monospace;font-size:13px;background:#0f3636;padding:1px 5px;border-radius:4px}
pre{margin:8px 0;padding:8px 0;background:#031414;border:1px solid var(--line);border-radius:8px;font:12.5px/1.45 ui-monospace,monospace;overflow:hidden}
pre div{padding:0 12px;white-space:pre-wrap;word-break:break-all}.a{background:var(--add)}.d{background:var(--del)}.h{color:#5b9bd5}
.prompt{border-color:var(--warn)}.prompt .who{color:var(--warn)}
.ok{border-color:var(--ok)}.ok .who{color:var(--ok)}
.foot{position:absolute;left:28px;right:28px;bottom:14px;color:var(--mut);font-size:12px;border-top:1px solid var(--line);padding-top:8px}
.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);word-break:break-all;gap:2px 14px;font-size:13px}.kv span:nth-child(odd){color:var(--mut)}
"""


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def diff_block(lines: list[str]) -> str:
    out = []
    for line in lines:
        kind = "a" if line.startswith("+") and not line.startswith("+++") else "d" if line.startswith("-") and not line.startswith("---") else "h" if line.startswith("@@") else ""
        out.append(f'<div class="{kind}">{html.escape(line) or " "}</div>')
    return "<pre>" + "".join(out) + "</pre>"


def markdown(text: str) -> str:
    parts, block, in_code = [], [], False
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            parts.append("<p>" + "<br>".join(inline(x) for x in paragraph) + "</p>")
            paragraph.clear()

    for raw in text.splitlines():
        if raw.startswith("```"):
            if in_code:
                parts.append(diff_block(block)); block = []
            else:
                flush()
            in_code = not in_code
            continue
        if in_code:
            block.append(raw)
        elif raw.strip():
            paragraph.append(raw.strip())
        else:
            flush()
    flush()
    return "".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-db", required=True); ap.add_argument("--session", required=True)
    ap.add_argument("--typed-output", required=True); ap.add_argument("--typed-text", required=True)
    ap.add_argument("--store-db", required=True)
    ap.add_argument("--incident", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{a.state_db}?mode=ro", uri=True)
    reply = db.execute("select content from messages where session_id=? and role='assistant' and content<>'' order by rowid desc limit 1", (a.session,)).fetchone()[0]
    model = db.execute("select model from sessions where id=?", (a.session,)).fetchone()[0]

    typed = json.loads(Path(a.typed_output).read_text().strip().splitlines()[-1])["output"]
    typed_lines = typed.splitlines()
    typed_html = f"<pre><div>{html.escape(a.typed_text)}</div></pre>" + "".join(f"<p>{inline(l)}</p>" for l in typed_lines[:2])

    store = sqlite3.connect(f"file:{a.store_db}?mode=ro", uri=True)
    result = json.loads(store.execute("select result from incidents where incident_id=?", (a.incident,)).fetchone()[0])
    v = result["verification"]
    kv = [("status", result["status"]), ("base revision", result["base_revision"]), ("fixed revision", result["fixed_revision"]),
          ("replay execution", result["replay_execution_id"]), ("replay state", result["replay_state"]),
          ("failed task recovered", f'{v["failed_task_id"]}: {v["failed_task_recovered"]} (attempts: {v["failed_task_attempts"]["original"]} then {v["failed_task_attempts"]["replay"]})'),
          ("reused, not re-run", ", ".join(v["reused_task_ids"])), ("outputs equal to original", v["outputs_equal"])]
    kv_html = "".join(f"<span>{html.escape(str(k))}</span><span>{html.escape(str(val))}</span>" for k, val in kv)

    page = f"""<!doctype html><meta charset=utf-8><style>{CSS}</style><body>
<div class=top><span>Hermes Agent &middot; Kestra on-call</span><span>webhook session {html.escape(a.session)} &middot; {html.escape(model)}</span></div>
<div class=wrap>
 <div class=col><div class=who>Hermes reply to the Kestra failure notification</div><div class=bubble>{markdown(reply)}</div></div>
 <div class=col>
  <div class="bubble prompt"><div class=who>You type the command Hermes printed (the approval)</div>{typed_html}</div>
  <div class="bubble ok"><div class=who>Result stored by the plugin</div><p>{inline(result["message"])}</p><div class=kv>{kv_html}</div></div>
 </div></div>
<div class=foot>Text is read from the rig: the reply from the Hermes webhook session, the command reply from the plugin's command handler and the result from the plugin store. The command was run by the end-to-end test driver, not from a chat app.</div>
"""
    Path(a.out).write_text(page)


if __name__ == "__main__":
    main()
