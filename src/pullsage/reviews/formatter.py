"""Formatting helpers for GitHub review summaries and inline comments."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from pullsage.github.models import (
    ChangedFile,
    GitHubReviewComment,
    ReviewCommentSide,
    ReviewEvent,
)
from pullsage.reviews.models import (
    FindingSeverity,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
)
from pullsage.reviews.validation import extract_changed_lines

_SEVERITY_ORDER = (
    FindingSeverity.CRITICAL,
    FindingSeverity.HIGH,
    FindingSeverity.MEDIUM,
    FindingSeverity.LOW,
    FindingSeverity.INFO,
)
_MAX_INLINE_BODY_CHARS = 6_000


def _safe_markdown(value: str) -> str:
    """Remove control characters and neutralize unintended GitHub mentions."""

    cleaned = "".join(
        character
        for character in value.replace("\r\n", "\n").replace("\r", "\n")
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    return (
        cleaned.strip()
        .replace("@", "@\u200b")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _single_line(value: str) -> str:
    return " ".join(_safe_markdown(value).split())


def _location(finding: ReviewFinding) -> str:
    path = f"`{_single_line(finding.file_path)}`"
    if finding.line is None:
        return path
    if finding.start_line is not None and finding.start_line != finding.line:
        return f"{path}, lines {finding.start_line}–{finding.line}"
    return f"{path}, line {finding.line}"


def _finding_markdown(finding: ReviewFinding, index: int) -> list[str]:
    lines = [
        (
            f"{index}. **{_single_line(finding.title)}** "
            f"({_location(finding)}; {finding.category.value}; "
            f"{finding.confidence:.0%} confidence)"
        ),
        "",
        f"   {_safe_markdown(finding.body)}",
        "",
        f"   **Evidence:** {_safe_markdown(finding.evidence)}",
    ]
    if finding.suggested_fix and finding.suggested_fix.strip():
        lines.extend(
            [
                "",
                (
                    "   **Suggested fix:** "
                    f"{_safe_markdown(finding.suggested_fix)}"
                ),
            ]
        )
    return lines


def format_review_markdown(review: ReviewResult) -> str:
    """Create a concise professional GitHub review body."""

    lines = [
        "## PullSage review",
        "",
        f"- **Verdict:** `{review.verdict.value}`",
        f"- **Risk:** `{review.risk_level.value}`",
        f"- **Confidence:** {review.confidence:.0%}",
        "",
        "### Summary",
        "",
        _safe_markdown(review.summary),
        "",
        "### Findings",
        "",
    ]

    if not review.findings:
        lines.extend(
            [
                "No reportable high-confidence findings.",
                "",
            ]
        )
    else:
        grouped: dict[FindingSeverity, list[ReviewFinding]] = defaultdict(list)
        for finding in review.findings:
            grouped[finding.severity].append(finding)
        for severity in _SEVERITY_ORDER:
            findings = grouped.get(severity, [])
            if not findings:
                continue
            lines.extend([f"#### {severity.value.title()}", ""])
            for index, finding in enumerate(findings, start=1):
                lines.extend(_finding_markdown(finding, index))
                lines.append("")

    if review.testing_recommendations:
        lines.extend(["### Testing recommendations", ""])
        lines.extend(
            f"- {_safe_markdown(recommendation)}"
            for recommendation in review.testing_recommendations
        )
        lines.append("")

    if review.limitations:
        lines.extend(["### Limitations", ""])
        lines.extend(
            f"- {_safe_markdown(limitation)}"
            for limitation in review.limitations
        )
        lines.append("")

    lines.extend(
        [
            "---",
            "_AI-assisted review by PullSage; a human reviewer should make the "
            "final merge decision._",
        ]
    )
    return "\n".join(lines).strip()


def _inline_body(finding: ReviewFinding) -> str:
    sections = [
        (
            f"**PullSage · {finding.severity.value.title()} · "
            f"{finding.confidence:.0%} confidence**"
        ),
        "",
        f"**{_single_line(finding.title)}**",
        "",
        _safe_markdown(finding.body),
        "",
        f"**Evidence:** {_safe_markdown(finding.evidence)}",
    ]
    if finding.suggested_fix and finding.suggested_fix.strip():
        sections.extend(
            [
                "",
                (
                    "**Suggested fix:** "
                    f"{_safe_markdown(finding.suggested_fix)}"
                ),
            ]
        )
    body = "\n".join(sections)
    if len(body) > _MAX_INLINE_BODY_CHARS:
        body = f"{body[: _MAX_INLINE_BODY_CHARS - 25].rstrip()}\n\n_[truncated]_"
    return body


def build_inline_comments(
    review: ReviewResult,
    changed_files: Sequence[ChangedFile],
    *,
    max_comments: int = 50,
) -> list[GitHubReviewComment]:
    """Build only comments whose locations still map to an added diff line."""

    if max_comments <= 0:
        return []
    changed_lines_by_path = {
        changed_file.filename: extract_changed_lines(changed_file.patch)
        for changed_file in changed_files
    }
    comments: list[GitHubReviewComment] = []
    seen: set[tuple[str, int, str]] = set()
    for finding in review.findings:
        if finding.line is None:
            continue
        valid_lines = changed_lines_by_path.get(finding.file_path, frozenset())
        if finding.line not in valid_lines:
            continue
        start_line = finding.start_line
        if start_line is not None and start_line not in valid_lines:
            start_line = None
        key = (
            finding.file_path,
            finding.line,
            _single_line(finding.title).casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        comments.append(
            GitHubReviewComment(
                path=finding.file_path,
                body=_inline_body(finding),
                line=finding.line,
                side=ReviewCommentSide.RIGHT,
                start_line=start_line,
                start_side=(
                    ReviewCommentSide.RIGHT if start_line is not None else None
                ),
            )
        )
        if len(comments) >= max_comments:
            break
    return comments


def review_event_for_result(review: ReviewResult) -> ReviewEvent:
    """Map a validated domain verdict to GitHub's review event."""

    return {
        ReviewVerdict.APPROVE: ReviewEvent.APPROVE,
        ReviewVerdict.COMMENT: ReviewEvent.COMMENT,
        ReviewVerdict.REQUEST_CHANGES: ReviewEvent.REQUEST_CHANGES,
    }[review.verdict]


# Public convenience alias.
format_review = format_review_markdown

