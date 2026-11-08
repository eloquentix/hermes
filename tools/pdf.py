import pymupdf

from tools.netguard import public_client

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
    async with public_client(timeout=30, follow_redirects=True) as client:
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
    async with public_client(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
    filename = url.rstrip("/").rsplit("/", 1)[-1]
    if not filename.endswith(".pdf"):
        filename += ".pdf"
    return resp.content, filename


def text_to_pdf(text: str, title: str = "") -> bytes:
    """Generate a simple PDF from plain text."""
    doc = pymupdf.open()
    width, height = 595, 842  # A4
    margin = 50
    font_size = 11
    line_height = font_size * 1.4
    usable_w = width - 2 * margin
    usable_h = height - 2 * margin

    lines = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split()
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            if pymupdf.get_text_length(test, fontsize=font_size) <= usable_w:
                current = test
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)

    y = margin
    page = doc.new_page(width=width, height=height)

    if title:
        title_size = 14
        page.insert_text((margin, y + title_size), title, fontsize=title_size)
        y += title_size * 2.5

    for line in lines:
        if y + line_height > height - margin:
            page = doc.new_page(width=width, height=height)
            y = margin
        page.insert_text((margin, y + font_size), line, fontsize=font_size)
        y += line_height

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes
