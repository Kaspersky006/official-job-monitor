import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402

INCLUDE = monitor.compile_rx([r"werkstudent\w*", r"working[ -]student\w*", r"praktik\w*", r"\bintern\b", r"internship\w*"])
EXCLUDE = monitor.compile_rx([r"\b(hr|marketing|finance|legal)\b"])


def test_matches_working_student_and_internship_titles():
    assert monitor.is_match("Werkstudent Software Testing (m/w/d)", "/jobs/1", INCLUDE, EXCLUDE)
    assert monitor.is_match("Working Student AI Engineering", "/jobs/2", INCLUDE, EXCLUDE)
    assert monitor.is_match("Praktikum Data Science", "/jobs/3", INCLUDE, EXCLUDE)
    assert monitor.is_match("Intern, Python Developer", "/jobs/4", INCLUDE, EXCLUDE)


def test_ignores_other_words_and_excluded_fields():
    assert not monitor.is_match("Internal Tools Engineer", "/jobs/5", INCLUDE, EXCLUDE)
    assert not monitor.is_match("International Sales Manager", "/jobs/6", INCLUDE, EXCLUDE)
    assert not monitor.is_match("Werkstudent Marketing", "/jobs/7", INCLUDE, EXCLUDE)


def test_matches_on_url_slug_when_link_text_is_generic():
    assert monitor.is_match("Apply now", "https://x.com/jobs/werkstudent-embedded-12", INCLUDE, EXCLUDE)


def test_extract_links_makes_absolute_urls_and_drops_junk():
    page = '<a href="/jobs/1#top">Werkstudent A</a><a href="mailto:a@b.c">mail</a><a href="#x">x</a>'
    assert monitor.extract_links(page, "https://example.com/careers") == [("Werkstudent A", "https://example.com/jobs/1")]


def test_check_company_finds_only_new_style_matches(monkeypatch):
    page = (
        '<a href="/a">Werkstudent Python</a><a href="/b">Senior Engineer</a>'
        '<a href="/c">Praktikum Test</a><a href="/a">Werkstudent Python</a>'
    )
    monkeypatch.setattr(monitor, "fetch_html", lambda url: page)
    res = monitor.check_company({"name": "X", "type": "html", "url": "https://x.com/jobs"}, INCLUDE, EXCLUDE)
    assert res["error"] is None
    assert [u for _, u in res["matches"]] == ["https://x.com/a", "https://x.com/c"]


def test_check_company_reports_errors_instead_of_crashing(monkeypatch):
    def boom(url):
        raise RuntimeError("blocked")

    monkeypatch.setattr(monitor, "fetch_html", boom)
    res = monitor.check_company({"name": "X", "type": "html", "url": "https://x.com"}, INCLUDE, EXCLUDE)
    assert res["error"].startswith("RuntimeError")
    assert res["matches"] == []
