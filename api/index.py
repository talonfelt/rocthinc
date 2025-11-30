import time
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional, List, Literal

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from playwright.sync_api import sync_playwright

def fetch_with_playwright(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        html = page.content()
        browser.close()
    return html

app = FastAPI()

ExportFormat = Literal["md", "tex", "pdf"]

class ExportRequest(BaseModel):
    url: str
    formats: Optional[List[ExportFormat]] = None

def escape_latex(text: str) -> str:
    repl = {"\\":"\\textbackslash{}", "&":"\\&", "%":"\\%", "$":"\\$", "#":"\\#", "_":"\\_", "{":"\\{", "}":"\\}", "~":"\\textasciitilde{}", "^":"\\textasciicircum{}"}
    for k, v in repl.items():
        text = text.replace(k, v)
    return text

def strip_html_to_text(html: str) -> str:
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html).strip()
    return html

def fetch_html_or_explain(url: str) -> str:
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "rocthinc/1.0"})
        if resp.status_code >= 400:
            return fetch_with_playwright(url)
        return resp.text
    except:
        return fetch_with_playwright(url)

def parse_conversation(url: str) -> dict:
    html = fetch_html_or_explain(url)
    soup = BeautifulSoup(html, "html.parser")
    is_ai_chat = any(domain in url.lower() for domain in ["chatgpt.com", "claude.ai", "grok.x.ai", "chat.openai.com"])
    messages = []
    if is_ai_chat:
        for msg in soup.select("[data-message-author-role]"):
            role = "You" if msg.get("data-message-author-role") == "user" else "Assistant"
            text = msg.get_text(separator="\n", strip=True)
            messages.append({"speaker": role, "content": text})
    else:
        text = strip_html_to_text(html)
        if len(text) > 20000:
            text = text[:20000] + " … [truncated]"
        messages.append({"speaker": "Page_Content", "content": text})
    return {
        "source": "chat" if is_ai_chat else "web",
        "url": url,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }

def to_markdown(conv: dict) -> str:
    lines = ["# Page Export", "", f"**Source:** {conv['source']}", f"**URL:** {conv['url']}", f"**Exported at:** {conv['created_at']}", ""]
    for m in conv["messages"]:
        lines.append(f"**{m['speaker']}:**")
        lines.append(m["content"])
        lines.append("")
    return "\n".join(lines)

def to_latex(conv: dict) -> str:
    headline = conv["messages"][0]["content"].split("\n", 1)[0].strip()
    headline = escape_latex(headline)
    url = escape_latex(conv["url"])
    exported_date = conv["created_at"][:10]
    content = escape_latex(" ".join(m["content"] for m in conv["messages"]))
    content = content.replace("→", r"$\rightarrow$").replace("–", "--").replace("—", "---").replace("“", "``").replace("”", "''")
    return f"""\\documentclass{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{hyperref}}
\\title{{{headline}}}
\\date{{Exported {exported_date}}}
\\begin{{document}}
\\maketitle
\\url{{{url}}}
{content}
\\end{{document}}"""

def make_zip_response(url: str, formats: List[ExportFormat]):
    conv = parse_conversation(url)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            z.writestr("page.md", to_markdown(conv))
        if "tex" in formats:
            z.writestr("page.tex", to_latex(conv))
        if "pdf" in formats:
            z.writestr("README_PDF.txt", "PDF export not implemented yet.")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="page_export.zip"'})

@app.post("/export")
def export_post(req: ExportRequest):
    formats = req.formats or ["md", "tex"]
    return make_zip_response(req.url, formats)

@app.get("/export")
def export_get(url: str = Query(...), formats: Optional[str] = Query(None)):
    fmts = ["md", "tex"]
    if formats:
        fmts = [f.strip() for f in formats.split(",") if f.strip() in ("md", "tex", "pdf")]
    return make_zip_response(url, fmts)