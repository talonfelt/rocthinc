import time
import zipfile
import threading
from io import BytesIO
from pathlib import Path
from typing import List, Literal

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ------------------------------------------------------------
# Playwright (Vercel-safe, single browser instance)
# ------------------------------------------------------------
_pw = threading.local()
def browser():
    if not hasattr(_pw, "b"):
        pw = sync_playwright().start()
        _pw.b = pw.chromium.launch(headless=True)
    return _pw.b

# ------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------
app = FastAPI(title=".rocthinc — from chat to chapter")

ExportFormat = Literal["md", "tex", "pdf"]

# ------------------------------------------------------------
# Honest fetch + parse (only for public AI chats)
# ------------------------------------------------------------
def fetch(url: str) -> str:
    page = browser().new_page()
    page.goto(url, wait_until="networkidle", timeout=40000)
    page.wait_for_selector('div[data-message-author-role], article, [data-testid^="conversation-turn"], div[class*="Message"]', timeout=25000)
    html = page.content()
    page.close()
    return html

def parse_chat(url: str) -> dict:
    soup = BeautifulSoup(fetch(url), "html.parser")
    for trash in soup(["script", "style", "nav", "header", "footer", "aside"]):
        trash.decompose()

    messages = []

    # ChatGPT / OpenAI
    for el in soup.select('[data-message-author-role]'):
        role = "Assistant" if el["data-message-author-role"] == "assistant" else "You"
        text = el.get_text(separator="\n", strip=True)
        messages.append({"role": role, "content": text})

    # Claude
    if not messages:
        for el in soup.find_all("div", class_=re.compile(r"user|claude", re.I)):
            role = "You" if "user" in " ".join(el.get("class",[])).lower() else "Claude"
            messages.append({"role": role, "content": el.get_text(separator="\n", strip=True)})

    # Grok / fallback
    if not messages:
        for el in soup.find_all(string=re.compile(r"You|Grok|Assistant", re.I)):
            parent = el.find_parent()
            if parent:
                role = "You" if "you" in el.lower() else "Grok"
                messages.append({"role": role, "content": parent.get_text(separator="\n", strip=True)})

    if not messages:
        raise HTTPException(400, "No conversation found. Make sure the URL is a public share link you can open in a browser.")

    title = soup.title.string.strip() if soup.title else "AI Chat Export"

    return {
        "title": title,
        "url": url,
        "exported": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "messages": messages
    }

# ------------------------------------------------------------
# Renderers (honest and beautiful)
# ------------------------------------------------------------
def to_md(data: dict) -> str:
    lines = [f"# {data['title']}", "", f"**Source:** {data['url']}", f"**Exported:** {data['exported']}", "", "---", ""]
    for m in data["messages"]:
        lines.append(f"### {m['role']}\n")
        lines.append(m["content"])
        lines.append("\n")
    return "\n".join(lines)

def to_tex(data: dict) -> str:
    title = data["title"].replace("&", "\\&").replace("%", "\\%").replace("_", "\\_")
    lines = [
        "\\documentclass{article}",
        "\\usepackage[margin=1in]{geometry}",
        "\\usepackage{verbatim}",
        "\\usepackage{hyperref}",
        "\\title{" + title + "}",
        "\\author{rocthinc — " + data["url"].replace("&", "\\&") + "}",
        "\\date{" + data["exported"] + "}",
        "\\begin{document}",
        "\\maketitle",
        ""
    ]
    for m in data["messages"]:
        role = m["role"]
        content = m["content"].replace("&", "\\&").replace("%", "\\%").replace("$", "\\$").replace("_", "\\_")
        lines.append(f"\\section*{{{role}}}")
        lines.append(content)
        lines.append("")
    lines += ["\\end{document}"]
    return "\n".join(lines)

# ------------------------------------------------------------
# Zip response
# ------------------------------------------------------------
def zip_it(data: dict, formats: List[ExportFormat]):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if "md" in formats:
            z.writestr("conversation.md", to_md(data))
        if "tex" in formats:
            z.writestr("conversation.tex", to_tex(data))
        if "pdf" in formats:
            z.writestr("README.txt", "PDF coming soon — compile conversation.tex with pdflatex.")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                            headers={"Content-Disposition": "attachment; filename=rocthinc.zip"})

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def ui():
    return (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

@app.post("/export")
def post(url: str, formats: List[ExportFormat] = ["md", "tex"]):
    data = parse_chat(url)
    return zip_it(data, formats)

@app.get("/export")
def get(url: str = Query(...), formats: str = "md,tex"):
    fmts = [f.strip() for f in formats.split(",") if f.strip()]
    data = parse_chat(url)
    return zip_it(data, fmts)