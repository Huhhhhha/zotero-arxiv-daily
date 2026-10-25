"""Weekly delivery guard; runs with the runner's standard-library Python."""
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BEIJING = timezone(timedelta(hours=8))
SEND_TIME = time(6, 17)
WORKFLOW_PATH = ".github/workflows/main.yml"


def digest_window(now=None, requested_date=""):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BEIJING)
    if requested_date:
        day = date.fromisoformat(requested_date)
        if day.weekday() != 1 or day > local.date():
            raise ValueError("digest_date must be a past or current Tuesday (YYYY-MM-DD).")
    else:
        day = local.date() - timedelta(days=(local.weekday() - 1) % 7)
        if local.date() == day and local.time() < SEND_TIME:
            day -= timedelta(days=7)
    end = datetime.combine(day, SEND_TIME, tzinfo=BEIJING)
    return day.isoformat(), end.isoformat()


def get_json(path):
    token = os.environ["GH_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    request = Request(
        f"https://api.github.com/repos/{repo}/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "zotero-arxiv-daily-delivery-guard",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def delivered(artifact_name, branch, fetch=get_json):
    # A successful *skipped* job is not evidence of an email. Only a receipt
    # uploaded by this workflow after SMTP acceptance counts as delivery.
    page = 1
    while True:
        query = urlencode({"name": artifact_name, "per_page": 100, "page": page})
        artifacts = fetch(f"actions/artifacts?{query}")["artifacts"]
        for artifact in artifacts:
            origin = artifact.get("workflow_run") or {}
            if (
                artifact.get("name") != artifact_name
                or artifact.get("expired", True)
                or origin.get("head_branch") != branch
            ):
                continue
            run = fetch(f"actions/runs/{origin['id']}")
            if run.get("path") == WORKFLOW_PATH and run.get("event") in {
                "schedule", "workflow_dispatch"
            }:
                return True
        if len(artifacts) < 100:
            return False
        page += 1


def main():
    day, end = digest_window(requested_date=os.environ.get("REQUESTED_DIGEST_DATE", ""))
    artifact_name = f"weekly-digest-delivered-{day}"
    force = os.environ.get("FORCE_SEND", "").lower() == "true"
    skip = not force and delivered(artifact_name, os.environ["DEFAULT_BRANCH"])
    outputs = {
        "digest_date": day,
        "as_of": end,
        "receipt_name": artifact_name,
        "should_send": "false" if skip else "true",
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
        for key, value in outputs.items():
            file.write(f"{key}={value}\n")
    print(f"Digest {day}: {'SMTP delivery receipt exists; skipping' if skip else 'no receipt; sending'}")


if __name__ == "__main__":
    main()
