import time
import zipfile
import threading
from io import BytesIO
from pathlib import Path
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Vercel-safe Playwright
_pw = threading.local()
def browser():
    if not hasattr(_pw, "instance"):
        pw = sync_playwright().start()
        _pw.instance = pw.chromium.launch(headless=True)
    return _pw.instance

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).parent.joinpath("index.html").read_text(encoding="utf-8")

def fetch_page(url: str) -> str:
    page = browser().new_page()
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)  # let JS settle
    html = page.content()
    page.close()
    return html

def parse_chat(url: str):
    html = fetch_page(url)
    soup = BeautifulSoup(html, "html.parser")

    messages = []

    # ChatGPT / OpenAI
    for div in soup.find_all("div", attrs={"data-message-author-role": True}):
        role = div["data-message-author-role"]
        role = "You" if role == "user" else "Assistant"
        text = div.get_text(separator="\n", strip=True)
        messages.append({"role": role, "content": text})

    if not messages:
        raise HTTPException(status_code=400, detail="No messages found. Is this a public share link?")

    title = soup.title.string.strip() if soup.title else "Chat Export"

    return {"title": title, "url": url, "messages": messages}

def make_zip(data):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Markdown
        md = f"# {data['title']}\n\n**Source:** {data['url']}\n\n"
        for m in data["messages"]:
            md += f"### {m['role']}\n{m['content']}\n\n"
        z.writestr("conversation.md", md)

        # LaTeX
        tex = "\\documentclass{article}\n\\usepackage[margin=1in]{geometry}\n\\begin{document}\n"
        tex += f"\\title{{{data['title']}}}\n\\maketitle\n\n"
        for m in data["messages"]:
            content = m["content"].replace("&", "\\&").replace("%", "\\%").replace("$", "\\$")
            tex += f"\\section*{{{m['role']}}}\n{content}\n\n"
        tex += "\\end{document}"
        z.writestr("conversation.tex", tex)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=rocthinc.zip"}
    )

@app.post("/export")
@app.get("/export")
def export(url: str = Query(...)):
    data = parse_chat(url)
    return make_zip(data)