"""PR review automation — scan, dispatch, and fix."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass
class PullRequest:
    number: int
    title: str
    branch: str
    url: str
    unresolved_comments: list[str]
    ci_failing: bool
    ci_summary: str


def list_my_prs() -> list[PullRequest]:
    """List open PRs authored by the current user with review status."""
    r = subprocess.run(
        ["gh", "pr", "list", "--author", "@me", "--state", "open",
         "--json", "number,title,headRefName,url,reviewDecision"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return []

    prs = []
    for item in json.loads(r.stdout or "[]"):
        pr_num = item["number"]
        comments = get_unresolved_comments(pr_num)
        ci_failing, ci_summary = get_ci_status(pr_num)

        prs.append(PullRequest(
            number=pr_num,
            title=item["title"],
            branch=item["headRefName"],
            url=item["url"],
            unresolved_comments=comments,
            ci_failing=ci_failing,
            ci_summary=ci_summary,
        ))

    return prs


def get_unresolved_comments(pr_number: int) -> list[str]:
    """Get unresolved review comments on a PR."""
    r = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
         "--jq", '.[] | select(.in_reply_to_id == null) | .body'],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        # Try the review threads approach
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number),
             "--json", "reviewThreads",
             "--jq", '.reviewThreads[] | select(.isResolved == false) | .comments[0].body'],
            capture_output=True, text=True, check=False,
        )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    return [c.strip() for c in r.stdout.strip().splitlines() if c.strip()]


def get_ci_status(pr_number: int) -> tuple[bool, str]:
    """Check CI status for a PR. Returns (is_failing, summary)."""
    r = subprocess.run(
        ["gh", "pr", "checks", str(pr_number), "--json", "name,state,conclusion",
         "--jq", '.[] | select(.conclusion == "FAILURE" or .state == "FAILURE") | .name'],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return False, ""

    failed = [line.strip() for line in r.stdout.strip().splitlines() if line.strip()]
    if failed:
        return True, f"{len(failed)} check(s) failing: {', '.join(failed[:3])}"
    return False, "all passing"


def build_pr_prompt(pr: PullRequest) -> str:
    """Build a Claude prompt for addressing a PR's issues."""
    parts = [f"You are on branch `{pr.branch}` for PR #{pr.number}: {pr.title}"]
    parts.append(f"PR URL: {pr.url}")

    if pr.unresolved_comments:
        parts.append(f"\n## Unresolved review comments ({len(pr.unresolved_comments)}):")
        parts.append("Address each of these comments:")
        for i, comment in enumerate(pr.unresolved_comments, 1):
            parts.append(f"\n### Comment {i}:\n{comment}")

    if pr.ci_failing:
        parts.append(f"\n## CI is failing: {pr.ci_summary}")
        parts.append("Please investigate and fix the failing checks.")

    if not pr.unresolved_comments and not pr.ci_failing:
        parts.append("\nNo unresolved comments or failing CI. Review the PR and make any improvements you see.")

    parts.append("\nAfter making changes, commit and push them to the branch.")

    return "\n".join(parts)
