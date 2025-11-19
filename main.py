# main.py — rocthinc server
#
# POST /export
# Body: { "url": "https://...", "formats": ["md", "tex", "pdf"] }
#
# Returns: ZIP file containing:
#   - conversation.md (if "md" requested)
#   - conversation.tex (if "tex" requested)
#   - README_PDF.txt placeholder (if "pdf" requested)
#
# This mirrors the Pythonista UI behavior:
# - Fetches a shared conversation URL
# - Strips HTML to rough text
# - Wraps it in a simple {source, url, created_at, messages[]} shape
# - Exports Markdown + LaTeX from that.

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Literal, Optional
from io import BytesIO
import zipfile
import requests
import time
import re

# ---------- FastAPI setup ----------

app = FastAPI(
    title="rocthinc",
    version="0.1.0",
    description="Share URL → Markdown / LaTeX (zip)."
)

ExportFormat = Literal["md", "tex", "pdf"]


class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None  # default set in /export


# ---------- Parsing helpers (mirroring Pythonista) ----------

def strip_html_to_text(html: str) -> str:
    """Very rough HTML → plain text, same idea as Pythonista UI."""
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
    """
    Fetch the URL and build the same 'conv' shape
    as the Pythonista local UI:

    {
      'source': ...,
      'url': ...,
      'created_at': ...,
      'messages': [
        {'speaker': 'user', 'content': ...},
        {'speaker': 'assistant', 'content': ...},
      ]
    }
    """
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching URL: {e}")

    html = resp.text
    source = detect_source(url, html)
    text = strip_html_to_text(html)

    # keep from exploding on very long pages
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


# ---------- Export helpers (same logic as Pythonista) ----------

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

@app.get("/")
def root():
    return JSONResponse(
        {
            "msg": "rocthinc online",
            "hint": "POST /export { url, formats }",
            "example": {
                "url": "https://share.chatgpt.com/...",
                "formats": ["md", "tex"],
            },
        }
    )


@app.post("/export")
def export(req: ExportRequest):
    # default formats if none provided
    formats: List[ExportFormat] = req.formats or ["md", "tex"]

    conv = parse_conversation(req.url)

    # Build in-memory ZIP
    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            md = to_markdown(conv)
            z.writestr("conversation.md", md)

        if "tex" in formats:
            tex = to_latex(conv)
            z.writestr("conversation.tex", tex)

        if "pdf" in formats:
            # Placeholder to keep the API surface ready for when you add real PDF export
            z.writestr(
                "README_PDF.txt",
                "PDF export is not implemented in this build.\n"
                "Use conversation.tex to compile a PDF locally.",
            )

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename=\"conversation_export.zip\"'},
    )

# Optional: for local dev, not needed on Railway
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)