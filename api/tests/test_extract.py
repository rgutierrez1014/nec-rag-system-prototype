from ingestion.extract import slugify_url


def test_slugify_url_strips_protocol():
    assert slugify_url("https://example.com/page") == "example-com-page"


def test_slugify_url_truncates_long_urls():
    long_url = "https://example.com/" + "a" * 200
    assert len(slugify_url(long_url)) <= 120
