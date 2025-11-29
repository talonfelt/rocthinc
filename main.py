import time
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import List, Literal, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from markdownify import markdownify as md_convert
from playwright.sync_api import sync_playwright
import threading

# ------------------------------------------------------------------
# Playwright thread-local (Vercel-safe)
# ------------------------------------------------------------------
_playwright = threading.local()

def get_browser():
    if not hasattr(_playwright, "browser"):
        pw = sync_playwright().start()
        _playwright.browser = pw.chromium.launch(headless=True)
    return _playwright.browser

def fetch_with_playwright(url: str) -> str:
    browser = get_browser()
    page = browser.new_page()
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_selector("body", timeout=15000)
    html = page.content()
    page.close()
    return html

# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------
app = FastAPI(title="rocthinc", description="Any web page → clean Markdown + LaTeX")

ExportFormat = Literal["md", "tex", "pdf"]

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def escape_latex(text: str) -> str:
    repl = {"\\":"\\textbackslash{}", "&":"\\&", "%":"\\%", "$":"\\$", "#":"\\#",
            "_":"\\_", "{":"\\{", "}":"\\}", "~":"\\textasciitilde{}", "^":"\\textasciicircum{}"}
    for k, v in repl.items():
        text = text.replace(k, v)
    return text

def fetch_html(url: str) -> str:
    # Fast path for normal sites
    try:
        headers = {"User-Agent": "rocthinc-bot/1.0"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            # If it looks like an AI chat, force Playwright
            if any(x in url for x in ["chatgpt.com", "chat.openai.com", "claude.ai", "grok.x.ai"]):
                raise Exception("AI chat → use browser")
            return r.text
    except:
        pass
    # Slow but works everywhere
    return fetch_with_playwright(url)

def parse_any_page(url: str) -> dict:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    # Remove garbage
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    title = soup.find("title")
    title_text = title.get_text(strip=True) if title else "Untitled"

    body = soup.find("body") or soup
    markdown = md_convert(str(body), heading_style="ATX", strip=["img"])

    if len(markdown) > 30000:
        markdown = markdown[:30000] + "\n\n… [truncated]"

    return {
        "title": title_text,
        "url": url,
        "created_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "content": markdown.strip()
    }

def to_markdown(data: dict) -> str:
    lines = [
        f"# {data['title']}",
        "",
        f"**Source:** {data['url']}",
        f"**Exported:** {data['created_at']}",
        "",
        "---",
        "",
        data['content']
    ]
    return "\n".join(lines)

def to_latex(data: dict) -> str:
    content = data['content']
    content = re.sub(r"^### (.*?)$", r"\\subsection{\1}", content, flags=re.M)
    content = re.sub(r"^## (.*?)$", r"\\section{\1}", content, flags=re.M)
    content = re.sub(r"\*\*(.*?)\*\*", r"\\textbf{\1}", content)
    content = re.sub(r"`(.*?)`", r"\\texttt{\1}", content, flags=re.S)
    content = content.replace("```", "\\begin{verbatim}\n").replace("```", "\n\\end{verbatim}\n")

    return f"""\\documentclass{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{verbatim}
\\usepackage{hyperref}
\\title{{{escape_latex(data['title'])}}}
\\author{{rocthinc — {escape_latex(data['url'])}}}
\\date{{{data['created_at']}}}
\\begin{{document}}
\\maketitle

{content}
\\end{{document}}"""

def make_zip(data: dict, formats: List[ExportFormat]):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            z.writestr("page.md", to_markdown(data))
        if "tex" in formats:
            z.writestr("page.tex", to_latex(data))
        if "pdf" in formats:
            z.writestr("README_PDF.txt", "PDF coming soon — compile page.tex with pdflatex.")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                            headers={"Content-Disposition": 'attachment; filename="rocthinc_export.zip"'})

# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def ui():
    path = Path(__file__).parent / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))

@app.post("/export")
def export_post(url: str, formats: List[ExportFormat] = ["md", "tex"]):
    data = parse_any_page(url)
    return make_zip(data, formats)

@app.get("/export")
def export_get(url: str = Query(...), formats: Optional[str] = "md,tex"):
    fmt_list = [f.strip() for f in formats.split(",") if f.strip()]
    data = parse_any_page(url)
    return make_zip(data, fmt_list)