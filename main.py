import time
import json
import re
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Literal

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import requests


# -------------------------------------------------------------
# App bootstrap
# -------------------------------------------------------------

app = FastAPI(
    title="rocthinc",
    version="0.6.0",
    description="Render shared AI chats (ChatGPT, Claude, etc.) → Markdown + LaTeX."
)

ExportFormat = Literal["md", "tex", "pdf"]


class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None


# -------------------------------------------------------------
# Utility helpers
# -------------------------------------------------------------

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


# -------------------------------------------------------------
# MARKDOWN + LATEX
# -------------------------------------------------------------

def to_markdown(conv: dict) -> str:
    lines = []
    lines.append("# Conversation Export\n")
    lines.append(f"**Source:** {conv['source']}")
    lines.append(f"**URL:** {conv['url']}")
    lines.append(f"**Exported at:** {conv['created_at']}\n")

    for msg in conv["messages"]:
        lines.append(f"**{msg['speaker'].capitalize()}:** {msg['content']}\n")

    return "\n".join(lines)


def to_latex(conv: dict) -> str:
    out = [
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
        out.append(rf"\textbf{{{role}:}} {content} \\[0.75em]")

    out.append(r"\end{document}")

    return "\n".join(out)


# -------------------------------------------------------------
# PLAYWRIGHT BROWSER RENDERING
# -------------------------------------------------------------

async def render_page_html(url: str) -> str:
    """
    Launch Chromium headless, load URL, wait for the real chat content.
    """

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page()

            resp = await page.goto(url, timeout=35000)

            if resp is None:
                raise HTTPException(
                    status_code=400,
                    detail="Page did not load. Check the URL."
                )

            if resp.status >= 400:
                raise HTTPException(
                    status_code=400,
                    detail=f"Page returned HTTP {resp.status}. Cannot read this link."
                )

            # Wait for any ChatGPT message nodes if possible
            try:
                await page.wait_for_selector("[data-message-author-role]", timeout=8000)
            except:
                pass  # Try anyway

            html = await page.content()
            await browser.close()
            return html

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"rocthinc tried to render that ChatGPT page but something went wrong. ({e})"
        )


# -------------------------------------------------------------
# PARSERS (DOM → JSON → fallback text)
# -------------------------------------------------------------

def parse_dom_chat(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.find_all(attrs={"data-message-author-role": True})

    if not nodes:
        return None

    messages = []
    for node in nodes:
        role = node.get("data-message-author-role") or "assistant"
        text = node.get_text("\n", strip=True)
        if text:
            messages.append({"speaker": role, "content": text})

    if not messages:
        return None

    return {
        "source": "chatgpt",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }


def parse_next_data(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")

    if not script or not script.string:
        return None

    try:
        data = json.loads(script.string)
    except:
        return None

    # Try Next.js
    sd = (
        data.get("props", {})
        .get("pageProps", {})
        .get("serverResponse", {})
        .get("data")
    )

    # Try Remix fallback
    if sd is None:
        try:
            remix = (
                data.get("state", {})
                .get("loaderData", {})
                .get("routes/share.$shareId.($action)", {})
            )
            sd = remix.get("serverResponse", {}).get("data")
        except:
            sd = None

    if sd is None:
        return None

    mapping = sd.get("mapping")
    if not isinstance(mapping, dict):
        return None

    items = []
    counter = 0

    for _, node in mapping.items():
        msg = node.get("message")
        if not msg:
            continue

        role = (msg.get("author") or {}).get("role") or "assistant"
        content = msg.get("content") or {}
        parts = content.get("parts") or []
        text = "\n".join(parts) if parts else content.get("text", "")

        text = (text or "").strip()
        if not text:
            continue

        items.append(
            {
                "speaker": role,
                "content": text,
                "sort": counter,
            }
        )
        counter += 1

    if not items:
        return None

    items.sort(key=lambda x: x["sort"])

    return {
        "source": "chatgpt",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": [{"speaker": i["speaker"], "content": i["content"]} for i in items],
    }


def fallback_text(html: str, url: str):
    clean = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    clean = re.sub(r"<style.*?</style>", "", clean, flags=re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    if len(clean) > 20000:
        clean = clean[:20000] + "… [truncated]"

    return {
        "source": "html",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": [{"speaker": "assistant", "content": clean}],
    }


# -------------------------------------------------------------
# MAIN CONVERSATION PIPELINE
# -------------------------------------------------------------

async def process(url: str):
    html = await render_page_html(url)

    conv = parse_dom_chat(html, url)
    if conv:
        return conv

    conv = parse_next_data(html, url)
    if conv:
        return conv

    return fallback_text(html, url)


# -------------------------------------------------------------
# ZIP OUTPUT
# -------------------------------------------------------------

def zip_output(conv: dict, formats: List[str]):
    buf = BytesIO()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            z.writestr("conversation.md", to_markdown(conv))
        if "tex" in formats:
            z.writestr("conversation.tex", to_latex(conv))
        if "pdf" in formats:
            z.writestr("README_pdf.txt", "PDF is not implemented. Use conversation.tex.")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="conversation.zip"'},
    )


# -------------------------------------------------------------
# ROUTES
# -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home():
    path = Path(__file__).parent / "index.html"
    return path.read_text() if path.exists() else "<h1>rocthinc</h1>"


@app.post("/export")
async def export_post(req: ExportRequest):
    formats = req.formats or ["md", "tex"]
    conv = await process(req.url)
    return zip_output(conv, formats)


@app.get("/export")
async def export_get(
    url: str = Query(...),
    formats: Optional[str] = Query("md,tex")
):
    fmts = [f.strip() for f in formats.split(",") if f.strip()]
    conv = await process(url)
    return zip_output(conv, fmts)


# -------------------------------------------------------------
# END OF FILE
# -------------------------------------------------------------