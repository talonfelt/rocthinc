# main.py — rocthinc server + web UI + ChatGPT renderer (Playwright) + parsers
#
# GET  /          → serves index.html (landing)
# GET  /export    → ?url=...&formats=md,tex (no JSON needed)
# POST /export    → { "url": "...", "formats": ["md","tex"] } (JSON API)
#
# Exports a ZIP containing conversation.md / conversation.tex.
# For ChatGPT URLs, uses a headless Chromium (Playwright) to render the page,
# then walks the DOM to extract actual messages.
# If no messages are visible (login/app wall/marketing page), returns a clear
# error message instead of a fake export.

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
import asyncio

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

app = FastAPI(
    title="rocthinc",
    version="0.5.0",
    description="Paste an AI chat URL you can see in a browser → get Markdown / LaTeX (zip)."
)

ExportFormat = Literal["md", "tex", "pdf"]


class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None  # default in handler


# ---------- Generic HTML helpers ----------

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
    For non-ChatGPT URLs: fetch HTML with requests and bail cleanly on error.
    """
    try:
        resp = requests.get(url, timeout=20)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"rocthinc could not reach that URL. "
                   f"Check that it’s correct and publicly reachable. (Details: {e})"
        )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The URL you pasted returned HTTP {resp.status_code}. "
                "rocthinc can only export pages that load normally in a browser without extra steps."
            ),
        )

    return resp.text


# ---------- ChatGPT-specific helpers (Playwright + parsers) ----------

async def fetch_chatgpt_rendered_html(url: str) -> str:
    """
    Use Playwright + headless Chromium to load the ChatGPT URL *as a browser*,
    including its client-side JavaScript, then return the full rendered HTML.
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # Go to the page and wait until network is mostly idle
            await page.goto(url, wait_until="networkidle")

            # Small extra delay to let React finish rendering if needed
            await page.wait_for_timeout(3000)

            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "rocthinc tried to render that ChatGPT page in a headless browser "
                f"but something went wrong. (Details: {e})"
            ),
        )


def looks_like_chatgpt_login_or_landing(html: str) -> bool:
    """
    Heuristic: distinguish a real chat DOM from the marketing/login page.
    We already know it's chatgpt.com; now we check content.
    """
    lower = html.lower()

    # Strong signals of login/landing/marketing:
    bad_snippets = [
        "by messaging chatgpt, an ai chatbot, you agree to our terms",
        "log in to chatgpt",
        "sign in to chatgpt",
        "get the app",
        "download the chatgpt app",
        "welcome to chatgpt",
    ]
    if any(s in lower for s in bad_snippets):
        return True

    # If we can't see any message containers at all, also treat as "no chat"
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.find_all(attrs={"data-message-author-role": True})
    if not nodes:
        return True

    return False


def parse_chatgpt_dom(html: str, url: str) -> Optional[dict]:
    """
    Primary ChatGPT parser: walk the DOM and extract nodes with
    data-message-author-role attributes into messages.
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

    return {
        "source": "chatgpt",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }


def parse_chatgpt_share_from_html(html: str, url: str) -> Optional[dict]:
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
        messages.append(
            {
                "speaker": m["author"],
                "content": m["text"],
            }
        )

    return {
        "source": "chatgpt",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }


# ---------- Main conversation parser (async) ----------

async def parse_conversation(url: str) -> dict:
    """
    Main entry: fetch/render page, detect source, dispatch to parser.
    - For ChatGPT URLs: use Playwright to render, then parse DOM/JSON.
    - For others: simple requests + HTML strip fallback.
    - On login/landing/app-wall: raise HTTPException with clear message
      instead of returning garbage.
    """
    url_l = url.lower()

    # ChatGPT path: full headless browser render
    if "chatgpt.com" in url_l or "chat.openai.com" in url_l:
        rendered_html = await fetch_chatgpt_rendered_html(url)

        if looks_like_chatgpt_login_or_landing(rendered_html):
            raise HTTPException(
                status_code=400,
                detail=(
                    "rocthinc couldn’t see any messages at that ChatGPT URL. "
                    "This usually means you’re still on a login / marketing / "
                    "‘open in app’ page.\n\n"
                    "Fix: open the link yourself in a browser, tap ‘Continue in browser’ "
                    "if it asks, wait until you can see the full chat, then copy the "
                    "URL from the browser’s address bar and paste THAT here."
                ),
            )

        conv = parse_chatgpt_dom(rendered_html, url)
        if conv is not None:
            return conv

        conv = parse_chatgpt_share_from_html(rendered_html, url)
        if conv is not None:
            return conv

        # At this point we DID render the page but still found no messages.
        raise HTTPException(
            status_code=400,
            detail=(
                "rocthinc rendered that ChatGPT page in a browser but still couldn’t "
                "extract any messages. The page may be using a newer layout or is not "
                "actually a conversation. If you’re sure it’s a chat, send this URL "
                "to the developer so they can update the parser."
            ),
        )

    # Non-ChatGPT: simple requests + HTML strip
    html = fetch_html_or_explain(url)
    source = detect_source(url, html)

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


async def make_zip_response(url: str, formats: List[ExportFormat]):
    conv = await parse_conversation(url)

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
            "Content-Disposition": 'attachment; filename=\"conversation_export.zip\"'
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
async def export_post(req: ExportRequest):
    formats: List[ExportFormat] = req.formats or ["md", "tex"]
    return await make_zip_response(req.url, formats)


@app.get("/export")
async def export_get(
    url: str = Query(..., description="Chat URL (must be viewable in a browser tab)"),
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
    return await make_zip_response(url, fmts)

# Optional local dev:
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)