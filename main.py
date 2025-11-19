# main.py — rocthinc server + web UI
#
# GET  /        → serves index.html (UI)
# POST /export  → takes {url, formats} JSON and returns a ZIP
#
# This works with:
#  - Your iOS Shortcut (POST /export)
#  - The web UI (index.html uses fetch('/export', ...))

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Literal, Optional
from io import BytesIO
from pathlib import Path
import zipfile
import requests
import time
import re

app = FastAPI(
    title="rocthinc",
    version="0.1.0",
    description="Share URL → Markdown / LaTeX (zip)."
)

ExportFormat = Literal["md", "tex", "pdf"]


class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None  # default in /export


# ---------- Parsing helpers (same as Pythonista UI) ----------

def strip_html_to_text(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html).strip()
    return html


def detect_source(url: str, html: str) -> str:
    url_l = url.lower()
    if "claude.ai" in url_l:
        return "claude"
    if "chatgpt.com" in url_l or "openai.com" in url_l:
        return "chatgpt"
    if "perplexity.ai" in url_l:
        return "perplexity"
    if "assistant" in html.lower():
        return "generic-ai"
    return "unknown"


def parse_conversation(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching URL: {e}")

    html = resp.text
    source = detect_source(url, html)
    text = strip_html_to_text(html)

    max_len = 5000
    if len(text) > max_len:
        text = text[:max_len] + " … [truncated]"

    conv = {
        "source": source,
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": [
            {"speaker": "user", "content": "Shared conversation: " + url},
            {"speaker": "assistant", "content": text},
        ],
    }
    return conv


# ---------- Export helpers ----------

def to_markdown(conv: dict) -> str:
    lines = []
    lines.append("# Conversation Export")
    lines.append("")
    lines.append(f"**Source:** {conv['source']}")
    lines.append(f"**URL:** {conv['url']}")
    lines.append(f"**Exported at:** {conv['created_at']}")
    lines.append("")
    for m in conv["messages"]:
        role = m["speaker"].capitalize()
        lines.append(f"**{role}:** {m['content']}")
        lines.append("")
    return "\n".join(lines)


def escape_latex(text: str) -> str:
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def to_latex(conv: dict) -> str:
    parts = [
        r"\documentclass{article}",
        r"\usepackage[margin=1in]{geometry}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage[utf8]{inputenc}",
        r"\begin{document}",
        r"\section*{Conversation Export}",
        "",
        r"\textbf{Source:} " + escape_latex(conv["source"]) + r"\\",
        r"\textbf{URL:} " + escape_latex(conv["url"]) + r"\\",
        r"\textbf{Exported at:} " + escape_latex(conv["created_at"]) + r"\\[1em]",
    ]
    for m in conv["messages"]:
        role = escape_latex(m["speaker"].capitalize())
        content = escape_latex(m["content"])
        parts.append(r"\textbf{" + role + r":} " + content + r"\\[0.75em]")
    parts.append(r"\end{document}")
    return "\n".join(parts)


# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
def web_ui():
    """Serve the HTML UI from index.html."""
    html_path = Path(__file__).parent / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>rocthinc</h1><p>index.html not found on server.</p>",
            status_code=500,
        )
    return html_path.read_text(encoding="utf-8")


@app.post("/export")
def export(req: ExportRequest):
    formats: List[ExportFormat] = req.formats or ["md", "tex"]

    conv = parse_conversation(req.url)

    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            md = to_markdown(conv)
            z.writestr("conversation.md", md)

        if "tex" in formats:
            tex = to_latex(conv)
            z.writestr("conversation.tex", tex)

        if "pdf" in formats:
            z.writestr(
                "README_PDF.txt",
                "PDF export not implemented yet. "
                "Use conversation.tex to compile a PDF locally.",
            )

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="conversation_export.zip"'
        },
    )

# For local dev only:
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)