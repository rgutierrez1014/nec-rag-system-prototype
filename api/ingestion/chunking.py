import re

TOKENS_PER_WORD = 1.3
MIN_CHUNK_TOKENS = 300
MAX_CHUNK_TOKENS = 600
MIN_CHUNK_WORDS = int(MIN_CHUNK_TOKENS / TOKENS_PER_WORD)  # ~230
MAX_CHUNK_WORDS = int(MAX_CHUNK_TOKENS / TOKENS_PER_WORD)  # ~461

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def chunk_content(content: str, page_title: str = "") -> list[dict]:
    """Split content into chunks with contextual headers.

    Returns list of dicts with keys: content, chunk_index, section_header.
    Each chunk targets 300-600 tokens (estimated via word count).
    Splits on section headings first, then on paragraph boundaries within sections.
    """
    sections = _split_into_sections(content)
    chunks = []

    for section_header, section_text in sections:
        header = section_header or page_title
        section_chunks = _split_section_into_chunks(section_text, header)
        for chunk_text in section_chunks:
            chunks.append({
                "content": chunk_text,
                "chunk_index": len(chunks),
                "section_header": header,
            })

    return chunks


def _split_into_sections(content: str) -> list[tuple[str, str]]:
    """Split content by markdown headings. Returns list of (header, body) tuples."""
    parts = HEADING_PATTERN.split(content)

    if not HEADING_PATTERN.search(content):
        return [("", content.strip())]

    sections = []
    i = 0
    if parts[0].strip():
        sections.append(("", parts[0].strip()))
        i = 1
    else:
        i = 1

    while i < len(parts) - 2:
        header_text = parts[i + 1].strip()
        body = parts[i + 2].strip() if i + 2 < len(parts) else ""
        sections.append((header_text, body))
        i += 3

    return [(h, b) for h, b in sections if b]


def _split_section_into_chunks(text: str, header: str) -> list[str]:
    """Split a section into chunks targeting MIN-MAX word count.

    Splits on double newlines to get paragraphs, then walks them, 
    accumulating them into a buffer. When adding the next paragraph 
    would exceed MAX_CHUNK_WORDS, the buffer is flushed as a chunk 
    and a new one begins. Paragraphs that are already over the max 
    bypass the buffer and go directly to _hard_split(). 
    
    After the loop, leftover buffer content shorter than 
    MIN_CHUNK_WORDS is merged into the previous chunk rather than 
    emitted as a standalone sliver.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks = []
    current_parts = []
    current_words = 0

    for para in paragraphs:
        para_words = len(para.split())

        if para_words > MAX_CHUNK_WORDS:
            if current_parts:
                chunks.append(_format_chunk(header, current_parts))
                current_parts = []
                current_words = 0
            for sub_chunk in _hard_split(para, MAX_CHUNK_WORDS):
                chunks.append(_format_chunk(header, [sub_chunk]))
            continue

        if current_words + para_words > MAX_CHUNK_WORDS and current_parts:
            chunks.append(_format_chunk(header, current_parts))
            current_parts = []
            current_words = 0

        current_parts.append(para)
        current_words += para_words

    if current_parts:
        if chunks and current_words < MIN_CHUNK_WORDS:
            last = chunks.pop()
            chunks.append(last + "\n\n" + "\n\n".join(current_parts))
        else:
            chunks.append(_format_chunk(header, current_parts))

    return chunks


def _format_chunk(header: str, parts: list[str]) -> str:
    """Prepend section header to chunk content."""
    body = "\n\n".join(parts)
    if header:
        return f"{header}\n\n{body}"
    return body


def _hard_split(text: str, max_words: int) -> list[str]:
    """Last-resort splitter for a single paragraph that exceeds 
    max_words.

    Splits on sentence boundaries (lookbehind on .!? + whitespace, 
    keeping punctuation attached), then accumulates sentences into 
    sub-chunks using the same "buffer and flush-on-overflow" pattern 
    as _split_section_into_chunks(). Unlike that function, no 
    minimum-size merging is applied. Small trailing sub-chunks are 
    emitted as-is since the input is already anomalously large.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current = []
    current_words = 0

    for sentence in sentences:
        s_words = len(sentence.split())
        if current_words + s_words > max_words and current:
            chunks.append(" ".join(current))
            current = []
            current_words = 0
        current.append(sentence)
        current_words += s_words

    if current:
        chunks.append(" ".join(current))

    return chunks
