from ingestion.chunking import chunk_content, _split_into_sections, MIN_CHUNK_WORDS, MAX_CHUNK_WORDS


def test_chunk_content_basic():
    content = "This is a paragraph. " * 100
    chunks = chunk_content(content, page_title="Test Page")
    assert len(chunks) >= 1
    for chunk in chunks:
        assert "content" in chunk
        assert "chunk_index" in chunk
        assert "section_header" in chunk


def test_chunk_content_preserves_section_headers():
    content = "# Section One\n\nParagraph one content. " * 50
    content += "\n\n# Section Two\n\nParagraph two content. " * 50
    chunks = chunk_content(content)
    headers = {c["section_header"] for c in chunks}
    assert "Section One" in headers
    assert "Section Two" in headers


def test_chunk_content_sequential_indexes():
    content = "Some content paragraph. " * 200
    chunks = chunk_content(content)
    for i, chunk in enumerate(chunks):
        assert chunk["chunk_index"] == i


def test_chunk_content_word_count_within_bounds():
    content = "A moderately long sentence with several words in it. " * 300
    chunks = chunk_content(content)
    for chunk in chunks:
        words = len(chunk["content"].split())
        # Allow some slack — header adds words, merging short tails is allowed
        assert words <= MAX_CHUNK_WORDS * 1.5, f"Chunk too large: {words} words"


def test_chunk_content_empty_input():
    assert chunk_content("") == []
    assert chunk_content("   ") == []


def test_split_into_sections_no_headings():
    sections = _split_into_sections("Just a plain paragraph.")
    assert len(sections) == 1
    assert sections[0][0] == ""


def test_split_into_sections_with_headings():
    content = "# First\n\nBody one.\n\n## Second\n\nBody two."
    sections = _split_into_sections(content)
    assert len(sections) == 2
    assert sections[0][0] == "First"
    assert sections[1][0] == "Second"


def test_page_title_used_when_no_section_header():
    content = "Paragraph without any headings. " * 50
    chunks = chunk_content(content, page_title="My Page Title")
    assert chunks[0]["section_header"] == "My Page Title"
