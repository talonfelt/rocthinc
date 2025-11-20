# main.py — rocthinc server + web UI + ChatGPT share parser (DOM-first + clear errors)
#
# GET  /          → serves index.html (landing)
# GET  /export    → ?url=...&formats=md,tex (no JSON needed)
# POST /export    → { "url": "...", "formats": ["md","tex"] } (JSON API)
#
# Exports a ZIP containing conversation.md / conversation.tex.
# If the URL is login/forbidden/app-wall, returns a clear error message instead of a zip.

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
    version="0.4.0",
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


def fetch_html_or_explain(url: str) -> str:
    """
    Fetch HTML and raise a clear HTTPException if we hit
    login/forbidden/app walls instead of real content.
    """
    try:
        resp = requests.get(url, timeout=20)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"rocthinc could not reach that URL. "
                   f"Check that it’s correct and publicly reachable. (Details: {e})"
        )

    # Status code handling
    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=400,
            detail="The URL you pasted is returning a 'login required' or 'forbidden' page. "
                   "rocthinc can only read chats that are visible without signing in. "
                   "Try using that platform’s share button to generate a public link, then paste THAT URL here."
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=f"The URL you pasted returned HTTP {resp.status_code}. "
                   "rocthinc can only export pages that load normally in a browser without extra steps."
        )

    html = resp.text
    #lower = html.lower()

    # Heuristic login-page detection
    #if any(w in lower for w in ("login", "log in", "sign in", "signin", "authentication")) and \
    #   ("password" in lower or "forgot password" in lower):
    #    raise HTTPException(
    #        status_code=400,
    #        detail="This looks like a login page, not the chat itself. "
    #               "rocthinc can only export chats that are visible to anyone with the link. "
    #               "If your AI platform has a 'Share' button, tap that and paste the resulting URL instead."
    #    )
        # Smarter login-wall detection — ignore harmless "Login" button in header
    lower = html.lower()
    if ("enter your password" in lower or
        "sign in to continue" in lower or
        "email address" in lower and "password" in lower or
        "authentication required" in lower or
        "you need to log in" in lower or
        "create an account" in lower or
        "open in the chatgpt app" in lower):
        raise HTTPException(
            status_code=400,
            detail="This is a real login / app-wall page. Open the share link in a browser, "
                   "click 'Continue in browser' if it asks, wait for the full chat to load, "
                   "then copy that new URL and paste it here."
        )
    # Heuristic "open in app" / interstitial detection
    if "open in app" in lower or "download our app" in lower:
        raise HTTPException(
            status_code=400,
            detail="The URL you pasted looks like an 'open in app' or app install screen, not the chat itself. "
                   "Open the conversation in a normal browser tab so you can see the messages, then copy THAT tab’s URL "
                   "and paste it into rocthinc."
        )

    return html


def parse_chatgpt_dom(html: str, url: str):
    """
    First attempt for ChatGPT-style pages: read the DOM and extract
    message blocks marked with data-message-author-role.
    """
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.find_all(attrs={"data-message-author-role": True})
    if not nodes:
        return None

    messages = []
    for node in nodes:
        role = node.get("data-message-author-role") or "assistant"
        text = node.get_text("\n", strip=True)
        if not text:
            continue
        messages.append(
            {
                "speaker": role,
                "content": text,
            }
        )

    if not messages:
        return None

    conv = {
        "source": "chatgpt",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }
    return conv


def parse_chatgpt_share_from_html(html: str, url: str):
    """
    Backup: parse a ChatGPT shared conversation using the JSON embedded in
    <script id="__NEXT_DATA__">, which contains a 'mapping' field of messages.
    """
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None

    try:
        data = json.loads(script.string)
    except Exception:
        return None

    # Try both Next.js + Remix-like shapes used on share pages.
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
    for _, node in mapping.items():
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

    if not items:
        return None

    items.sort(key=lambda m: (m["create_time"] or 0, m["order"]))

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


def parse_conversation(url: str) -> dict:
    """
    Main entry: fetch page, detect source, dispatch to parser.
    - For ChatGPT-style links: try DOM parser, then JSON parser.
    - Otherwise: fallback to crude text scrape.
    - If the URL is login/forbidden/app wall, fetch_html_or_explain()
      raises HTTPException with a human-friendly error string.
    """
    html = fetch_html_or_explain(url)
    source = detect_source(url, html)

    if source == "chatgpt":
        conv = parse_chatgpt_dom(html, url)
        if conv is not None:
            return conv

        conv = parse_chatgpt_share_from_html(html, url)
        if conv is not None:
            return conv

    # Generic fallback: treat whole page as one assistant message
    text = strip_html_to_text(html)
    max_len = 20000
    if len(text) > max_len:
        text = text[:max_len] + " … [truncated]"

    messages = [
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