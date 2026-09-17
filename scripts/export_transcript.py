"""Render a Claude Code session transcript into readable Markdown.

The raw .jsonl is the authoritative record and is committed alongside this
output, but it is 2.7MB of nested JSON and nobody reads that. The brief says
"We read these", so the transcript should be legible: who asked for what, what
the tool did, what came back, and where the direction changed.

Tool output is truncated per call -- full output is in the .jsonl if a specific
result matters. Thinking blocks are kept: they are where the reasoning that
produced a decision is visible, which is most of what makes a log worth reading.

    python scripts/export_transcript.py <session.jsonl> logs/session.md
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

MAX_TOOL_OUT = 1600      # chars of any single tool result
MAX_TOOL_IN = 1200       # chars of any single tool input


def clip(text: str, n: int) -> str:
    text = text.rstrip()
    if len(text) <= n:
        return text
    return text[:n] + f"\n… [{len(text) - n:,} more chars — see the .jsonl]"


def ts(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return ""


def blocks(content) -> list:
    """Message content is either a bare string or a list of typed blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def render(src: Path, dst: Path) -> None:
    out: list[str] = [
        "# Session transcript",
        "",
        f"Rendered from `{src.name}` by `scripts/export_transcript.py`. "
        "The raw `.jsonl` in this directory is the complete record; this file "
        "is the same conversation made readable. Tool output is truncated per "
        "call.",
        "",
        "---",
        "",
    ]
    turn = 0

    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue

        msg = rec.get("message") or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        bs = blocks(msg.get("content"))
        stamp = ts(rec.get("timestamp"))

        # Tool results arrive as "user" messages; they belong with the call.
        results = [b for b in bs if isinstance(b, dict)
                   and b.get("type") == "tool_result"]
        if role == "user" and results:
            for b in results:
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c
                                  if isinstance(x, dict))
                out += ["**Result:**", "", "```",
                        clip(str(c or "").strip(), MAX_TOOL_OUT), "```", ""]
            continue

        texts, thinks, calls = [], [], []
        for b in bs:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text", "").strip():
                texts.append(b["text"].strip())
            elif t == "thinking" and b.get("thinking", "").strip():
                thinks.append(b["thinking"].strip())
            elif t == "tool_use":
                calls.append(b)

        if not (texts or thinks or calls):
            continue

        if role == "user":
            turn += 1
            out += [f"## Turn {turn}  ·  Lawrance  ·  {stamp}", ""]
            out += [t for t in texts] + [""]
            continue

        if thinks:
            out += ["<details><summary>Claude — reasoning</summary>", ""]
            for t in thinks:
                out += ["> " + t.replace("\n", "\n> "), ""]
            out += ["</details>", ""]
        for t in texts:
            out += [f"**Claude:** {t}", ""]
        for c in calls:
            name = c.get("name", "?")
            inp = c.get("input") or {}
            shown = inp.get("command") or inp.get("file_path") or json.dumps(
                inp, indent=2)[:MAX_TOOL_IN]
            desc = inp.get("description")
            out += [f"**Tool — `{name}`**" + (f" — {desc}" if desc else ""), "",
                    "```", clip(str(shown), MAX_TOOL_IN), "```", ""]

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out), encoding="utf-8")
    print(f"{dst}  ({dst.stat().st_size / 1024:.0f} KB, {turn} user turns)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    render(Path(sys.argv[1]), Path(sys.argv[2]))
