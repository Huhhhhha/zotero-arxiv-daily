"""Tests for main entry point.

The @hydra.main decorator makes main() hard to test directly in pytest
because config_path resolution depends on the calling context.
We test the inner logic by calling main's body with a composed config.
"""

import pytest
from hydra.core.global_hydra import GlobalHydra


@pytest.fixture(autouse=True)
def _clear_hydra():
    """Ensure GlobalHydra is clean before and after each test in this module."""
    GlobalHydra.instance().clear()
    yield
    GlobalHydra.instance().clear()


def test_main_creates_executor_and_runs(config, monkeypatch):
    """Verify that the main function creates an Executor and calls run()."""
    calls = []

    class FakeExecutor:
        def __init__(self, cfg):
            calls.append(("init", cfg))

        def run(self):
            calls.append(("run",))

    monkeypatch.setattr("zotero_arxiv_daily.main.Executor", FakeExecutor)

    # Call main's body directly, bypassing @hydra.main
    from zotero_arxiv_daily import main as main_mod

    # Simulate what @hydra.main does: calls main(config)
    main_mod.main.__wrapped__(config)

    assert ("init", config) in calls
    assert ("run",) in calls


def test_main_debug_logging(config, monkeypatch):
    """Verify debug mode sets appropriate log level."""
    from omegaconf import open_dict

    with open_dict(config):
        config.executor.debug = True

    class FakeExecutor:
        def __init__(self, cfg):
            pass
        def run(self):
            pass

    monkeypatch.setattr("zotero_arxiv_daily.main.Executor", FakeExecutor)

    from zotero_arxiv_daily import main as main_mod

    main_mod.main.__wrapped__(config)
    # If we get here without error, the debug path executed successfully


@pytest.mark.parametrize("sent", [False, None])
def test_main_does_not_record_unsent_email(config, monkeypatch, tmp_path, sent):
    import json
    from zotero_arxiv_daily import main as main_mod
    receipt = tmp_path / "receipt.json"
    monkeypatch.setenv("DIGEST_RECEIPT_PATH", str(receipt))
    monkeypatch.setenv("DIGEST_DATE", "2026-10-20")
    class FakeExecutor:
        def __init__(self, cfg):
            pass
        def run(self):
            return sent
    monkeypatch.setattr(main_mod, "Executor", FakeExecutor)
    with pytest.raises(RuntimeError, match="No email was sent"):
        main_mod.main.__wrapped__(config)
    assert not receipt.exists()


def test_main_records_smtp_success(config, monkeypatch, tmp_path):
    import json
    from zotero_arxiv_daily import main as main_mod
    receipt = tmp_path / "receipt.json"
    monkeypatch.setenv("DIGEST_RECEIPT_PATH", str(receipt))
    monkeypatch.setenv("DIGEST_DATE", "2026-10-20")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    class FakeExecutor:
        def __init__(self, cfg):
            pass
        def run(self):
            return True
    monkeypatch.setattr(main_mod, "Executor", FakeExecutor)
    main_mod.main.__wrapped__(config)
    saved = json.loads(receipt.read_text())
    assert saved["digest_date"] == "2026-10-20"
    assert saved["run_id"] == "123"
    assert saved["sent_at"].endswith("+00:00")


def test_main_smtp_failure_does_not_leave_a_receipt(config, monkeypatch, tmp_path):
    from zotero_arxiv_daily import main as main_mod
    receipt = tmp_path / "receipt.json"
    monkeypatch.setenv("DIGEST_RECEIPT_PATH", str(receipt))
    class FakeExecutor:
        def __init__(self, cfg):
            pass
        def run(self):
            raise OSError("SMTP unavailable")
    monkeypatch.setattr(main_mod, "Executor", FakeExecutor)
    with pytest.raises(OSError, match="SMTP unavailable"):
        main_mod.main.__wrapped__(config)
    assert not receipt.exists()
