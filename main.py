import time, zipfile, threading
from io import BytesIO
from pathlib import Path
from typing import List, Literal
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

_pw = threading.local()
def browser():
    if not hasattr(_pw, "b"):
        pw = sync_playwright().start()
        _pw.b = pw.chromium.launch(headless=True)
    return _pw.b

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def root():
    return (Path(__file__).parent / "index.html").read_text()

def fetch(url):
    page = browser().new_page()
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("article, [data-message-author-role], [data-testid^='conversation-turn']", timeout=20000)
    html = page.content()
    page.close()
    return html

def parse(url):
    soup = BeautifulSoup(fetch(url), "html.parser")
    msgs = []
    for m in soup.select("[data-message-author-role]"):
        role = "You" if m["data-message-author-role"] == "user" else "Assistant"
        msgs.append(f"### {role}\n{m.get_text(separator='\n')}\n")
    title = soup.title.string if soup.title else "Chat"
    return {"title": title, "messages": msgs, "url": url}

@app.post("/export")
@app.get("/export")
def export(url: str = Query(...), formats: str = "md,tex"):
    data = parse(url)
    f = ["md", "tex"] if formats == "md,tex" else [f.strip() for f in formats.split(",")]
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if "md" in f:
            z.writestr("conversation.md", f"# {data['title']}\n\n" + "\n".join(data['messages']))
        if "tex" in f:
            z.writestr("conversation.tex", f"\\documentclass{{article}}\n\\begin{{document}}\n\\title{{{data['title']}}}\n\\maketitle\n" + "\n\n".join(data['messages']) + "\n\\end{{document}}")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=rocthinc.zip"})