import httpx
import pymupdf

SCHEMA = {
    "name": "fetch_pdf",
    "description": (
        "Download and extract text from a PDF at a URL. "
        "Use for academic papers, policy documents, reports, constitutions, etc. "
        "Returns raw text for you to summarize."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Direct URL to the PDF file"}
        },
        "required": ["url"],
    },
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; inflightbot/1.0)"}


async def fetch_pdf(url: str, max_chars: int = 3000) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()

    doc = pymupdf.open(stream=resp.content, filetype="pdf")
    pages_text = []
    total = 0
    for page in doc:
        text = page.get_text()
        pages_text.append(text)
        total += len(text)
        if total >= max_chars:
            break
    doc.close()

    return "\n".join(pages_text)[:max_chars]


async def fetch_pdf_bytes(url: str) -> tuple[bytes, str]:
    """Download PDF and return (raw_bytes, filename)."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
    filename = url.rstrip("/").rsplit("/", 1)[-1]
    if not filename.endswith(".pdf"):
        filename += ".pdf"
    return resp.content, filename
