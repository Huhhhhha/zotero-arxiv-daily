"""Tests for ArxivRetriever."""

import datetime as dt
import time
from types import SimpleNamespace

import arxiv
import feedparser
import pytest

from zotero_arxiv_daily.retriever.arxiv_retriever import ArxivRetriever, _run_with_hard_timeout
import zotero_arxiv_daily.retriever.arxiv_retriever as arxiv_retriever


def _sleep_and_return(value: str, delay_seconds: float) -> str:
    time.sleep(delay_seconds)
    return value


def _raise_runtime_error() -> None:
    raise RuntimeError("boom")


def test_arxiv_retriever(config, mock_feedparser, monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)

    # The RSS fixture gives us paper IDs.  After feedparser, the code calls
    # arxiv.Client().results(search) which makes real HTTP requests.  We mock
    # the arxiv Client so the test stays offline.
    new_entries = [
        e for e in mock_feedparser.entries
        if e.get("arxiv_announce_type", "new") == "new"
    ]
    paper_ids = [e.id.removeprefix("oai:arXiv.org:") for e in new_entries]

    # Build fake ArxivResult-like objects matching each RSS entry
    fake_results = []
    for entry in new_entries:
        pid = entry.id.removeprefix("oai:arXiv.org:")
        fake_results.append(SimpleNamespace(
            title=entry.title,
            authors=[SimpleNamespace(name="Test Author")],
            summary="Test abstract",
            pdf_url=f"https://arxiv.org/pdf/{pid}",
            entry_id=f"https://arxiv.org/abs/{pid}",
            source_url=lambda pid=pid: f"https://arxiv.org/e-print/{pid}",
        ))

    class FakeClient:
        def __init__(self, **kw):
            pass
        def results(self, search):
            return iter(fake_results)

    monkeypatch.setattr(arxiv_retriever.arxiv, "Client", FakeClient)

    # Skip file downloads in convert_to_paper
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_html", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_pdf", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_tar", lambda paper: None)

    retriever = ArxivRetriever(config)
    papers = retriever.retrieve_papers()

    assert len(papers) == len(new_entries)
    assert set(p.title for p in papers) == set(e.title for e in new_entries)


def test_run_with_hard_timeout_returns_value():
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 0.01), timeout=1, operation="test op", paper_title="paper"
    )
    assert result == "done"


def test_run_with_hard_timeout_returns_none_on_timeout(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 1.0), timeout=0.01, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "timed out" in warnings[0]


def test_run_with_hard_timeout_returns_none_on_failure(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _raise_runtime_error, (), timeout=1, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "boom" in warnings[0]


def _make_arxiv_result(entry_id: str, published) -> SimpleNamespace:
    return SimpleNamespace(entry_id=entry_id, published=published)


def _test_entries(count: int) -> list[SimpleNamespace]:
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return [
        _make_arxiv_result(
            f"https://arxiv.org/abs/2609.{1000 + i}",
            now - dt.timedelta(hours=i + 1),
        )
        for i in range(count)
    ]


class _BlockThenRecoverClient:
    """arXiv API stand-in: every call yields at most `fail_after` entries and
    then 429s, until `fail_until_call` calls have been made; afterwards it
    serves the rest of the set starting at the requested offset."""

    def __init__(self, entries: list, fail_after: int, fail_until_call: int):
        self.entries = entries
        self.fail_after = fail_after
        self.fail_until_call = fail_until_call
        self.offsets: list[int] = []

    def results(self, search, offset=0):
        self.offsets.append(offset)
        if len(self.offsets) <= self.fail_until_call:
            yield from self.entries[offset:offset + self.fail_after]
            raise arxiv.HTTPError(url="https://export.arxiv.org/api/query", retry=1, status=429)
        yield from self.entries[offset:]


class _PageScriptedClient:
    """Serves a fixed list of pages in call order; all but the last page end
    with a 429. Pages are raw slices, so a later page may repeat entries that
    a real result-set shift would re-serve at a resumed offset."""

    def __init__(self, pages: list[list]):
        self.pages = pages
        self.offsets: list[int] = []

    def results(self, search, offset=0):
        self.offsets.append(offset)
        page = self.pages[len(self.offsets) - 1]
        yield from page
        if len(self.offsets) < len(self.pages):
            raise arxiv.HTTPError(url="https://export.arxiv.org/api/query", retry=1, status=429)


def _date_range_retriever(config, monkeypatch, client):
    monkeypatch.setattr(arxiv_retriever.arxiv, "Client", lambda **kwargs: client)
    config.source.arxiv.days_back = 7
    return ArxivRetriever(config)


def test_date_range_retries_resume_from_last_offset(config, monkeypatch):
    monkeypatch.setattr(arxiv_retriever, "DATE_RANGE_RETRY_WAITS", [0])
    sleeps: list[float] = []
    monkeypatch.setattr(arxiv_retriever, "sleep", sleeps.append)
    entries = _test_entries(5)
    client = _BlockThenRecoverClient(entries, fail_after=3, fail_until_call=1)

    papers = _date_range_retriever(config, monkeypatch, client)._retrieve_raw_papers_by_date_range()

    assert client.offsets == [0, 3]
    assert sleeps == [0]
    assert [p.entry_id for p in papers] == [e.entry_id for e in entries]


def test_date_range_dedupes_entries_re_served_after_set_shift(config, monkeypatch):
    monkeypatch.setattr(arxiv_retriever, "DATE_RANGE_RETRY_WAITS", [0])
    monkeypatch.setattr(arxiv_retriever, "sleep", lambda _seconds: None)
    entries = _test_entries(4)
    # While the first attempt waits out the 429, a new submission shifts the
    # sorted result set, so resuming at offset 2 re-serves entry 1.
    client = _PageScriptedClient([entries[0:2], entries[1:4]])

    papers = _date_range_retriever(config, monkeypatch, client)._retrieve_raw_papers_by_date_range()

    assert client.offsets == [0, 2]
    assert [p.entry_id for p in papers] == [e.entry_id for e in entries]


def test_date_range_falls_back_to_partial_result(config, monkeypatch):
    monkeypatch.setattr(arxiv_retriever, "DATE_RANGE_RETRY_WAITS", [0, 0])
    monkeypatch.setattr(arxiv_retriever, "sleep", lambda _seconds: None)
    entries = _test_entries(5)
    client = _BlockThenRecoverClient(entries, fail_after=3, fail_until_call=10**9)

    papers = _date_range_retriever(config, monkeypatch, client)._retrieve_raw_papers_by_date_range()

    assert client.offsets == [0, 3, 5]
    assert [p.entry_id for p in papers] == [e.entry_id for e in entries]


def test_date_range_raises_when_nothing_was_retrieved(config, monkeypatch):
    monkeypatch.setattr(arxiv_retriever, "DATE_RANGE_RETRY_WAITS", [0])
    monkeypatch.setattr(arxiv_retriever, "sleep", lambda _seconds: None)
    client = _BlockThenRecoverClient(_test_entries(3), fail_after=0, fail_until_call=10**9)

    with pytest.raises(arxiv.HTTPError):
        _date_range_retriever(config, monkeypatch, client)._retrieve_raw_papers_by_date_range()


def test_set_user_agent_overrides_package_header():
    client = arxiv.Client()
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs.get("headers") or {})
        return "feed"

    client._session.get = fake_get

    arxiv_retriever._set_user_agent(client)
    client._session.get("https://export.arxiv.org/api/query", headers={"user-agent": "arxiv.py/2.3.2"})

    assert captured["user-agent"].startswith("zotero-arxiv-daily/")
