from .base import BaseRetriever, register_retriever
import arxiv
from arxiv import Result as ArxivResult
from ..protocol import Paper
from ..utils import extract_markdown_from_pdf, extract_tex_code_from_tar
from tempfile import TemporaryDirectory
import feedparser
from tqdm import tqdm
import multiprocessing
import os
from queue import Empty
from time import sleep
from typing import Any, Callable, TypeVar
from loguru import logger
import requests
from datetime import datetime, timedelta, timezone

T = TypeVar("T")

DOWNLOAD_TIMEOUT = (10, 60)
PDF_EXTRACT_TIMEOUT = 180
TAR_EXTRACT_TIMEOUT = 180

# arXiv 429-blocks on shared GitHub runner IPs commonly last an hour or more,
# so same-IP retries only pay off if the job can outlast the block. These waits
# spread retries over ~1.8h; pagination resumes from where the previous attempt
# stopped, so each retry costs only the not-yet-fetched pages.
DATE_RANGE_RETRY_WAITS = [60, 300, 600, 900, 900, 1200, 1200, 1200]

# arXiv API etiquette (https://info.arxiv.org/help/api/tou.html): identify your
# client. The arxiv package hardcodes a generic per-request user agent; a
# descriptive one lets arXiv admins tell polite low-rate traffic apart when
# reviewing shared-IP blocks.
USER_AGENT = "zotero-arxiv-daily/1.0 (+https://github.com/Huhhhhha/zotero-arxiv-daily)"


def _set_user_agent(client: arxiv.Client) -> None:
    """Best-effort swap of the package's hardcoded user agent for USER_AGENT.

    The arxiv package passes its own "user-agent" header on every call, so the
    wrapper must override rather than merge.
    """
    try:
        original_get = client._session.get

        def get(url, **kwargs):
            headers = dict(kwargs.pop("headers", None) or {})
            headers["user-agent"] = USER_AGENT
            return original_get(url, headers=headers, **kwargs)

        client._session.get = get
    except Exception:
        pass


def _download_file(url: str, path: str) -> None:
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)


def _run_in_subprocess(
    result_queue: Any,
    func: Callable[..., T | None],
    args: tuple[Any, ...],
) -> None:
    try:
        result_queue.put(("ok", func(*args)))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _run_with_hard_timeout(
    func: Callable[..., T | None],
    args: tuple[Any, ...],
    *,
    timeout: float,
    operation: str,
    paper_title: str,
) -> T | None:
    start_methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in start_methods else start_methods[0])
    result_queue = context.Queue()
    process = context.Process(target=_run_in_subprocess, args=(result_queue, func, args))
    process.start()

    try:
        status, payload = result_queue.get(timeout=timeout)
    except Empty:
        if process.is_alive():
            process.kill()
        process.join(5)
        result_queue.close()
        result_queue.join_thread()
        logger.warning(f"{operation} timed out for {paper_title} after {timeout} seconds")
        return None

    process.join(5)
    result_queue.close()
    result_queue.join_thread()

    if status == "ok":
        return payload

    logger.warning(f"{operation} failed for {paper_title}: {payload}")
    return None


def _extract_text_from_pdf_worker(pdf_url: str) -> str:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.pdf")
        _download_file(pdf_url, path)
        return extract_markdown_from_pdf(path)


def _extract_text_from_html_worker(html_url: str) -> str | None:
    import trafilatura

    downloaded = trafilatura.fetch_url(html_url)
    if downloaded is None:
        raise ValueError(f"Failed to download HTML from {html_url}")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    if not text:
        raise ValueError(f"No text extracted from {html_url}")
    return text


def _extract_text_from_tar_worker(source_url: str, paper_id: str, paper_title: str | None = None) -> str | None:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.tar.gz")
        _download_file(source_url, path)
        file_contents = extract_tex_code_from_tar(path, paper_id, paper_title=paper_title)
        if not file_contents or "all" not in file_contents:
            raise ValueError("Main tex file not found.")
        return file_contents["all"]


@register_retriever("arxiv")
class ArxivRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        if self.config.source.arxiv.category is None:
            raise ValueError("category must be specified for arxiv.")
        self.days_back = int(self.config.source.arxiv.get("days_back", 1))
        # With multi-day retrieval there can be thousands of candidates, so
        # full text is only fetched for the top-ranked papers after reranking.
        self.defer_full_text = self.days_back > 1

    def _retrieve_raw_papers(self) -> list[ArxivResult]:
        if self.days_back > 1:
            return self._retrieve_raw_papers_by_date_range()
        client = arxiv.Client(num_retries=10, delay_seconds=10)
        query = '+'.join(self.config.source.arxiv.category)
        include_cross_list = self.config.source.arxiv.get("include_cross_list", False)
        # Get the latest paper from arxiv rss feed
        feed = feedparser.parse(f"https://rss.arxiv.org/atom/{query}")
        if 'Feed error for query' in feed.feed.title:
            raise Exception(f"Invalid ARXIV_QUERY: {query}.")
        raw_papers = []
        allowed_announce_types = {"new", "cross"} if include_cross_list else {"new"}
        all_paper_ids = [
            i.id.removeprefix("oai:arXiv.org:")
            for i in feed.entries
            if i.get("arxiv_announce_type", "new") in allowed_announce_types
        ]
        if self.config.executor.debug:
            all_paper_ids = all_paper_ids[:10]

        # Get full information of each paper from arxiv api
        bar = tqdm(total=len(all_paper_ids))
        max_batch_retries = 5
        batch_retry_delay = 30
        for i in range(0, len(all_paper_ids), 20):
            search = arxiv.Search(id_list=all_paper_ids[i:i + 20])
            for attempt in range(max_batch_retries):
                try:
                    batch = list(client.results(search))
                    bar.update(len(batch))
                    raw_papers.extend(batch)
                    break
                except arxiv.HTTPError as exc:
                    if exc.status == 429 and attempt < max_batch_retries - 1:
                        wait = batch_retry_delay * (attempt + 1)
                        logger.warning(f"arXiv API 429 on batch {i // 20}, retry {attempt + 1}/{max_batch_retries} in {wait}s")
                        sleep(wait)
                    else:
                        raise
            if i + 20 < len(all_paper_ids):
                sleep(3)
        bar.close()

        return raw_papers

    def _retrieve_raw_papers_by_date_range(self) -> list[ArxivResult]:
        client = arxiv.Client(num_retries=2, delay_seconds=15, page_size=100)
        _set_user_agent(client)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Widen the query window to tolerate the moderation lag between
        # submission and announcement; the first-version filter below keeps
        # the effective window exact.
        query_start = (now - timedelta(days=self.days_back + 3)).strftime("%Y%m%d%H%M")
        query_end = now.strftime("%Y%m%d%H%M")
        categories = " OR ".join(f"cat:{c}" for c in self.config.source.arxiv.category)
        query = f"({categories}) AND submittedDate:[{query_start} TO {query_end}]"
        search = arxiv.Search(
            query=query,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        cutoff = now - timedelta(days=self.days_back)
        # export.arxiv.org intermittently 429s GitHub-hosted runners for an
        # hour or more -- they share IPs with other users' traffic, so the
        # limiter can be tripped by someone else. Retries span DATE_RANGE_RETRY_WAITS
        # (~1.8h) to outlast a block; each attempt resumes pagination at the
        # offset where the previous one stopped (Client.results(offset=...) is
        # public API) instead of restarting from page one, so partial progress
        # is never wasted and no page is requested twice. Entries that shifted
        # into earlier positions while waiting re-hit the seen_ids dedupe.
        # If the API never recovers but some pages succeeded, fall back to the
        # partial result -- the newest papers are on the first pages.
        raw_papers: list[ArxivResult] = []
        seen_ids: set[str] = set()
        raw_seen = 0  # raw entries consumed from the paged result set
        for attempt, wait_on_error in enumerate(DATE_RANGE_RETRY_WAITS + [None]):
            try:
                for result in tqdm(client.results(search, offset=raw_seen), desc="Fetching arxiv papers by date range"):
                    raw_seen += 1
                    published = result.published
                    if published.tzinfo is not None:
                        published = published.astimezone(timezone.utc).replace(tzinfo=None)
                    # Skip revised versions of papers first submitted before the window.
                    if published < cutoff or result.entry_id in seen_ids:
                        continue
                    seen_ids.add(result.entry_id)
                    raw_papers.append(result)
                    if self.config.executor.debug and len(raw_papers) >= 10:
                        break
                return raw_papers
            except (arxiv.HTTPError, ConnectionError, requests.exceptions.RequestException) as exc:
                if wait_on_error is None:
                    if raw_papers:
                        logger.warning(
                            f"arXiv API still failing after {len(DATE_RANGE_RETRY_WAITS)} retries; "
                            f"falling back to partial result ({len(raw_papers)} papers)"
                        )
                        return raw_papers
                    raise
                status = getattr(exc, "status", type(exc).__name__)
                logger.warning(
                    f"arXiv API error ({status}) during date-range retrieval, "
                    f"retry {attempt + 1}/{len(DATE_RANGE_RETRY_WAITS) + 1} in {wait_on_error}s "
                    f"({len(raw_papers)} papers kept, resuming from offset {raw_seen})"
                )
                sleep(wait_on_error)
        raise RuntimeError("arXiv retrieval failed with no partial results")

    def convert_to_paper(self, raw_paper: ArxivResult) -> Paper:
        title = raw_paper.title
        authors = [a.name for a in raw_paper.authors]
        abstract = raw_paper.summary
        pdf_url = raw_paper.pdf_url
        def extract_full_text():
            full_text = extract_text_from_tar(raw_paper)
            if full_text is None:
                full_text = extract_text_from_html(raw_paper)
            if full_text is None:
                full_text = extract_text_from_pdf(raw_paper)
            return full_text
        if self.defer_full_text:
            full_text = None
            full_text_fetcher = extract_full_text
        else:
            full_text = extract_full_text()
            full_text_fetcher = None
        return Paper(
            source=self.name,
            title=title,
            authors=authors,
            abstract=abstract,
            url=raw_paper.entry_id,
            pdf_url=pdf_url,
            full_text=full_text,
            full_text_fetcher=full_text_fetcher,
        )


def extract_text_from_html(paper: ArxivResult) -> str | None:
    html_url = paper.entry_id.replace("/abs/", "/html/")
    try:
        return _extract_text_from_html_worker(html_url)
    except Exception as exc:
        logger.warning(f"HTML extraction failed for {paper.title}: {exc}")
        return None


def extract_text_from_pdf(paper: ArxivResult) -> str | None:
    if paper.pdf_url is None:
        logger.warning(f"No PDF URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_pdf_worker,
        (paper.pdf_url,),
        timeout=PDF_EXTRACT_TIMEOUT,
        operation="PDF extraction",
        paper_title=paper.title,
    )


def extract_text_from_tar(paper: ArxivResult) -> str | None:
    source_url = paper.source_url()
    if source_url is None:
        logger.warning(f"No source URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_tar_worker,
        (source_url, paper.entry_id, paper.title),
        timeout=TAR_EXTRACT_TIMEOUT,
        operation="Tar extraction",
        paper_title=paper.title,
    )
