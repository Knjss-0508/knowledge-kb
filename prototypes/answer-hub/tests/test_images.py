from __future__ import annotations

from answer_hub.images import split_image_urls


def test_split_image_urls_parses_json_array_string() -> None:
    value = '["https://example.com/a.jpg", "https://example.com/b.jpg"]'

    assert split_image_urls(value) == [
        "https://example.com/a.jpg",
        "https://example.com/b.jpg",
    ]


def test_split_image_urls_keeps_newline_format() -> None:
    value = "https://example.com/a.jpg\nhttps://example.com/b.jpg"

    assert split_image_urls(value) == [
        "https://example.com/a.jpg",
        "https://example.com/b.jpg",
    ]


def test_split_image_urls_falls_back_to_lines_for_invalid_json() -> None:
    value = "[not-json]\nhttps://example.com/b.jpg"

    assert split_image_urls(value) == [
        "[not-json]",
        "https://example.com/b.jpg",
    ]
