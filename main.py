# main.py — rocthinc server + web UI + ChatGPT share parser
#
# GET  /          → serves index.html (landing)
# GET  /export    → ?url=...&formats=md,tex (no JSON needed)
# POST /export    → { "url": "...", "formats": ["md","tex"] } (JSON API)
#
# Exports a ZIP containing conversation.md / conversation.tex.

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Literal, Optional
from io import BytesIO
from pathlib import Path
import zipfile
import requests
import time
import re
import json
from bs4 import BeautifulSoup

app = FastAPI(
    title="rocthinc",
    version="0.3.0",
    description="Share URL → Markdown / LaTeX (zip)."
)

ExportFormat = Literal["md", "tex", "pdf"]


class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None  # default in handler


# ---------- Parsing helpers ----------

def strip_html_to_text(html: str) -> str:
    """Very rough fallback HTML → plain text."""
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html).strip()
    return html


def detect_source(url: str, html: str) -> str:
    url_l = url.lower()
    if "claude.ai" in url_l:
        return "claude"
    if "chatgpt.com" in url_l or "chat.openai.com" in url_l:
        return "chatgpt"
    if "perplexity.ai" in url_l:
        return "perplexity"
    if "assistant" in html.lower():
        return "generic-ai"
    return "unknown"


def parse_chatgpt_share_from_html(html: str, url: str):
    """
    Parse a ChatGPT shared conversation using the JSON embedded in
    <script id="__NEXT_DATA__">, which contains pageProps.serverResponse.data
    with a 'mapping' field of messages.  [oai_citation:1‡Greasy Fork](https://greasyfork.org/en/scripts/456055-chatgpt-exporter/code?utm_source=chatgpt.com)
    """
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None

    data = json.loads(script.string)

    # Try both Next.js + Remix shapes used on share pages.
    server_data = None
    try:
        server_data = (
            data.get("props", {})
                .get("pageProps", {})
                .get("serverResponse", {})
                .get("data")
        )
    except Exception:
        server_data = None

    if server_data is None:
        try:
            remix = (
                data.get("state", {})
                .get("loaderData", {})
                .get("routes/share.$shareId.($action)", {})
            )
            server_data = remix.get("serverResponse", {}).get("data")
        except Exception:
            server_data = None

    if server_data is None:
        return None

    mapping = server_data.get("mapping")
    if not isinstance(mapping, dict):
        return None

    # Extract messages
    items = []
    order_counter = 0
    for key, node in mapping.items():
        message = node.get("message")
        if not message:
            continue

        author = (message.get("author") or {}).get("role") or "unknown"
        content = message.get("content") or {}
        content_type = content.get("content_type")
        text = ""

        # Old style: content_type='text', parts=[...]
        if content_type == "text":
            parts = content.get("parts") or []
            text = "\n\n".join(str(p) for p in parts if p)
        # Newer style sometimes has nested fields; fall back to 'text' key.
        if not text:
            if "text" in content and isinstance(content["text"], str):
                text = content["text"]
            else:
                # Last resort: stringify content
                text = json.dumps(content, ensure_ascii=False)

        create_time = message.get("create_time") or node.get("create_time") or 0
        items.append(
            {
                "author": author,
                "text": text.strip(),
                "create_time": create_time,
                "order": order_counter,
            }
        )
        order_counter += 1

    # Filter roles and sort by time (or fallback order)
    roles_keep = {"user", "assistant", "system"}
    items = [m for m in items if m["author"] in roles_keep and m["text"]]

    items.sort(key=lambda m: (m["create_time"] or 0, m["order"]))

    if not items:
        return None

    messages = []
    for m in items:
        role = m["author"]
        messages.append(
            {
                "speaker": role,
                "content": m["text"],
            }
        )

    conv = {
        "source": "chatgpt",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }
    return conv


def fetch_html(url: str) -> str:
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching URL: {e}")
    return resp.text


def parse_conversation(url: str) -> dict:
    """
    Main entry: fetch page, detect source, dispatch to parser.
    - For ChatGPT share links: try structured parser.
    - Otherwise: fallback to crude text scrape.
    """
    html = fetch_html(url)
    source = detect_source(url, html)

    # Try ChatGPT-specific parser
    if source == "chatgpt":
        conv = parse_chatgpt_share_from_html(html, url)
        if conv is not None:
            return conv

    # Fallback: previous simple behavior
    text = strip_html_to_text(html)
    max_len = 5000
    if len(text) > max_len:
        text = text[:max_len] + " … [truncated]"

    messages = [
        {"speaker": "user", "content": "Shared conversation: " + url},
        {"speaker": "assistant", "content": text},
    ]
    return {
        "source": source,
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }


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


def make_zip_response(url: str, formats: List[ExportFormat]):
    conv = parse_conversation(url)

    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            z.writestr("conversation.md", to_markdown(conv))
        if "tex" in formats:
            z.writestr("conversation.tex", to_latex(conv))
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


# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
def web_ui():
    html_path = Path(__file__).parent / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>rocthinc</h1><p>index.html not found on server.</p>",
            status_code=500,
        )
    return html_path.read_text(encoding="utf-8")


@app.post("/export")
def export_post(req: ExportRequest):
    formats: List[ExportFormat] = req.formats or ["md", "tex"]
    return make_zip_response(req.url, formats)


@app.get("/export")
def export_get(
    url: str = Query(..., description="Shared chat URL"),
    formats: Optional[str] = Query(
        None,
        description="Comma-separated formats, e.g. md,tex or md,tex,pdf",
    ),
):
    if formats:
        fmts = [f.strip() for f in formats.split(",") if f.strip()]
        fmts = [f for f in fmts if f in ("md", "tex", "pdf")]
        if not fmts:
            fmts = ["md", "tex"]
    else:
        fmts = ["md", "tex"]
    return make_zip_response(url, fmts)

# Optional local dev:
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)