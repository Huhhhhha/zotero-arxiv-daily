"""Regression coverage for missed schedules and weekly SMTP receipts."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "weekly_digest", Path(__file__).parents[1] / ".github/scripts/weekly_digest.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "now, expected",
    [
        ("2026-10-19T22:16:00+00:00", "2026-10-13"),
        ("2026-10-19T22:17:00+00:00", "2026-10-20"),
        ("2026-10-21T02:23:00+00:00", "2026-10-20"),
        ("2026-10-25T15:00:00+00:00", "2026-10-20"),
        ("2026-10-26T22:17:00+00:00", "2026-10-27"),
    ],
)
def test_delayed_slots_keep_the_tuesday_digest(now, expected):
    day, end = guard.digest_window(datetime.fromisoformat(now))
    assert day == expected
    assert end == expected + "T06:17:00+08:00"


def test_manual_recovery_uses_requested_week():
    day, _ = guard.digest_window(
        datetime(2026, 10, 25, tzinfo=timezone.utc), "2026-10-13"
    )
    assert day == "2026-10-13"


@pytest.mark.parametrize("day", ["2026-10-21", "2026-10-27", "invalid"])
def test_invalid_or_future_recovery_date_is_rejected(day):
    with pytest.raises(ValueError):
        guard.digest_window(datetime(2026, 10, 25, tzinfo=timezone.utc), day)


def test_successful_runs_without_receipts_do_not_block_delivery():
    assert not guard.delivered(
        "weekly-digest-delivered-2026-10-20", "main",
        lambda path: {"artifacts": []},
    )


def test_only_receipt_from_the_sender_workflow_counts():
    name = "weekly-digest-delivered-2026-10-20"
    artifacts = [
        {"name": name, "expired": True, "workflow_run": {"id": 1, "head_branch": "main"}},
        {"name": name, "expired": False, "workflow_run": {"id": 2, "head_branch": "other"}},
        {"name": name, "expired": False, "workflow_run": {"id": 3, "head_branch": "main"}},
        {"name": name, "expired": False, "workflow_run": {"id": 4, "head_branch": "main"}},
    ]
    calls = []
    def fetch(path):
        calls.append(path)
        if path.startswith("actions/artifacts?"):
            return {"artifacts": artifacts}
        return {
            "path": ".github/workflows/ci.yml" if path.endswith("/3") else guard.WORKFLOW_PATH,
            "event": "workflow_dispatch",
        }
    assert guard.delivered(name, "main", fetch)
    assert "actions/runs/1" not in calls
    assert "actions/runs/2" not in calls


def test_receipt_lookup_paginates():
    name = "weekly-digest-delivered-2026-10-20"
    def fetch(path):
        if "&page=1" in path:
            return {"artifacts": [{"name": name, "expired": True}] * 100}
        if "&page=2" in path:
            return {"artifacts": [
                {"name": name, "expired": False, "workflow_run": {"id": 2, "head_branch": "main"}}
            ]}
        return {"path": guard.WORKFLOW_PATH, "event": "schedule"}
    assert guard.delivered(name, "main", fetch)


def test_receipt_api_failure_is_not_silently_treated_as_a_skip():
    def fail(path):
        raise OSError("GitHub unavailable")
    with pytest.raises(OSError):
        guard.delivered("receipt", "main", fail)


@pytest.mark.parametrize("force", ["false", "true"])
def test_resume_date_suppresses_old_digest_without_faking_a_receipt(monkeypatch, tmp_path, force):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("DIGEST_START_DATE", "2026-10-27")
    monkeypatch.setenv("FORCE_SEND", force)
    monkeypatch.setattr(guard, "digest_window", lambda **kw: ("2026-10-20", "2026-10-20T06:17:00+08:00"))
    def unexpected(*args):
        raise AssertionError("Suppressed weeks must not query delivery or send")
    monkeypatch.setattr(guard, "delivered", unexpected)
    guard.main()
    assert "should_send=false" in output.read_text()


def test_resume_date_allows_the_next_tuesday(monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("DIGEST_START_DATE", "2026-10-27")
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    monkeypatch.setenv("FORCE_SEND", "false")
    monkeypatch.setattr(guard, "digest_window", lambda **kw: ("2026-10-27", "2026-10-27T06:17:00+08:00"))
    monkeypatch.setattr(guard, "delivered", lambda *args: False)
    guard.main()
    assert "should_send=true" in output.read_text()
