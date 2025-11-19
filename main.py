# main.py  — rocthinc URL → Markdown / LaTeX (zip)
#
# POST /export
# Body: { "url": "...", "formats": ["md","tex"] }
#
# Returns: ZIP with conversation.md and/or conversation.tex
#
# NOTE:
# - Markdown + LaTeX export WORK right away (as long as requests can fetch the URL).
# - PDF is not implemented here yet (needs extra libraries / binaries).
# - HTML parsing is deliberately simple (stub) and should be upgraded later.

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from io import BytesIO
import zipfile
import requests
import time
import re
from typing import List, Literal, Optional

app = FastAPI(title="rocthinc", version="0.1.0")


# ---------- Models ----------

ExportFormat = Literal["md", "tex", "pdf"]


class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None  # default set below


# ---------- Helpers ----------

def detect_source(url: str, html: str) -> str:
    url_lower = url.lower()
    if "claude.ai" in url_lower:
        return "claude"
    if "chatgpt.com" in url_lower or "openai.com" in url_lower:
        return "chatgpt"
    if "perplexity.ai" in url_lower:
        return "perplexity"
    # crude fallbacks
    if "assistant" in html.lower():
        return "generic-ai"
    return "unknown"


def fetch_page(url: str) -> str:
    try:
        resp = requests.get(url, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching URL: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Fetch failed with status {resp.status_code}")
    return resp.text


def strip_html_to_text(html: str) -> str:
    # Very rough: remove tags and collapse whitespace.
    text = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)       # strip tags
    text = re.sub(r"\s+", " ", text).strip()   # collapse whitespace
    return text


def parse_conversation_from_url(url: str) -> dict:
    """
    CURRENT STATE:
    - Fetches the page.
    - Detects platform (very roughly).
    - Produces a flat "conversation" with 2 messages:
        user: original URL
        assistant: stripped page text (truncated).
    TODO:
    - Replace this with real per-platform HTML parsers.
    """
    html = fetch_page(url)
    source = detect_source(url, html)
    plain = strip_html_to_text(html)

    # truncate huge pages so you at least get a file, not a 50MB blob
    max_len = 4000
    if len(plain) > max_len:
        plain = plain[:max_len] + " … [truncated]"

    messages = [
        {
            "id": "m1",
            "speaker": "user",
            "content": f"Shared conversation: {url}",
        },
        {
            "id": "m2",
            "speaker": "assistant",
            "content": plain,
        },
    ]

    return {
        "source": source,
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }


# ---------- Exporters ----------

def to_markdown(conv: dict) -> str:
    lines = []
    lines.append("# Conversation Export")
    lines.append("")
    lines.append(f"**Source:** {conv['source']}")
    lines.append(f"**URL:** {conv['url']}")
    lines.append(f"**Exported at:** {conv['created_at']}")
    lines.append("")
    for msg in conv["messages"]:
        role = msg["speaker"].capitalize()
        lines.append(f"**{role}:** {msg['content']}")
        lines.append("")
    return "\n".join(lines)


def escape_latex(text: str) -> str:
    # minimal escaping for LaTeX special chars
    replacements = {
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
    for k, v in replacements.items():
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
    for msg in conv["messages"]:
        role = escape_latex(msg["speaker"].capitalize())
        content = escape_latex(msg["content"])
        parts.append(r"\textbf{" + role + r":} " + content + r"\\[0.75em]")
    parts.append(r"\end{document}")
    return "\n".join(parts)


# (PDF intentionally not implemented yet; see note in /export)


# ---------- Routes ----------

@app.get("/")
def root():
    return JSONResponse(
        {
            "msg": "rocthinc: POST /export { url, formats }",
            "example": {
                "url": "https://share.chatgpt.com/...",
                "formats": ["md", "tex"]
            },
        }
    )


@app.post("/export")
def export(req: ExportRequest):
    formats: List[ExportFormat] = req.formats or ["md", "tex"]

    conv = parse_conversation_from_url(req.url)

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            md = to_markdown(conv)
            z.writestr("conversation.md", md)

        if "tex" in formats:
            tex = to_latex(conv)
            z.writestr("conversation.tex", tex)

        if "pdf" in formats:
            # NOT IMPLEMENTED YET
            # This placeholder keeps the API shape without failing.
            # Later you can generate a real PDF from Markdown or LaTeX.
            z.writestr(
                "README_PDF.txt",
                "PDF export is not implemented yet in this build. "
                "Use the LaTeX file to compile a PDF locally."
            )

    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="conversation_export.zip"'},
    )

# If you ever want to run this locally:
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)
