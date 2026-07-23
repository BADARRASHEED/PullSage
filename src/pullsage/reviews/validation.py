"""Domain validation and conservative filtering for AI review output."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from pullsage.exceptions import ReviewValidationError
from pullsage.github.models import ChangedFile
from pullsage.reviews.models import (
    MAX_REVIEW_LIST_ITEMS,
    FindingCategory,
    FindingSeverity,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
    RiskLevel,
)

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_SEVERITY_RANK = {
    FindingSeverity.INFO: 0,
    FindingSeverity.LOW: 1,
    FindingSeverity.MEDIUM: 2,
    FindingSeverity.HIGH: 3,
    FindingSeverity.CRITICAL: 4,
}
_RISK_RANK = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}
_BLOCKING_CATEGORIES = {
    FindingCategory.CORRECTNESS,
    FindingCategory.SECURITY,
    FindingCategory.RELIABILITY,
}
NO_FINDINGS_SUMMARY = (
    "No high-confidence defects were identified in the supplied pull request changes."
)
UNMAPPED_LOCATION_LIMITATION = (
    "One or more findings could not be mapped safely to an added diff line; "
    "they are included only in the general review summary."
)


def coerce_review_result(
    review: ReviewResult | Mapping[str, Any],
) -> ReviewResult:
    """Validate an arbitrary mapping as the strict structured review schema."""

    if isinstance(review, ReviewResult):
        return review
    try:
        return ReviewResult.model_validate(review)
    except ValidationError as exc:
        raise ReviewValidationError(
            "Structured review failed schema validation.",
            validation_errors=[
                dict(error)
                for error in exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            ],
        ) from exc


def extract_changed_lines(patch: str | None) -> frozenset[int]:
    """Extract added RIGHT-side line numbers from one unified patch."""

    if not patch:
        return frozenset()
    changed_lines: set[int] = set()
    new_line: int | None = None
    for raw_line in patch.splitlines():
        match = _HUNK_HEADER.match(raw_line)
        if match:
            new_line = int(match.group("start"))
            continue
        if new_line is None:
            continue
        if raw_line.startswith("\\"):
            continue
        # File headers occur before the first hunk. Once inside a hunk, even
        # source text beginning with "+++" or "---" is an ordinary changed line.
        if raw_line.startswith("+"):
            changed_lines.add(new_line)
            new_line += 1
            continue
        if raw_line.startswith("-"):
            continue
        new_line += 1
    return frozenset(changed_lines)


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _duplicate(
    first: ReviewFinding,
    second: ReviewFinding,
) -> bool:
    if _normalize_text(first.id) == _normalize_text(second.id):
        return True
    return (
        first.file_path == second.file_path
        and first.line == second.line
        and _normalize_text(first.title) == _normalize_text(second.title)
    )


def _finding_rank(finding: ReviewFinding) -> tuple[float, int]:
    return finding.confidence, _SEVERITY_RANK[finding.severity]


def deduplicate_findings(
    findings: Sequence[ReviewFinding],
) -> list[ReviewFinding]:
    """Remove semantic duplicates, retaining the strongest occurrence."""

    unique: list[ReviewFinding] = []
    for finding in findings:
        duplicate_index = next(
            (index for index, existing in enumerate(unique) if _duplicate(existing, finding)),
            None,
        )
        if duplicate_index is None:
            unique.append(finding)
        elif _finding_rank(finding) > _finding_rank(unique[duplicate_index]):
            unique[duplicate_index] = finding
    return unique


def _deduplicate_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = _normalize_text(value)
        if normalized and normalized not in seen:
            result.append(value)
            seen.add(normalized)
    return result


def _changed_file_map(
    changed_files: Sequence[ChangedFile],
) -> dict[str, ChangedFile]:
    return {changed_file.filename: changed_file for changed_file in changed_files}


def _safe_location(
    finding: ReviewFinding,
    changed_file: ChangedFile,
) -> tuple[int | None, int | None]:
    if finding.line is None:
        return None, None
    changed_lines = extract_changed_lines(changed_file.patch)
    if finding.line not in changed_lines:
        return None, None
    if finding.start_line is not None and finding.start_line not in changed_lines:
        return finding.line, None
    return finding.line, finding.start_line


def _is_meaningful_blocker(
    finding: ReviewFinding,
    minimum_confidence: float,
) -> bool:
    if finding.severity is FindingSeverity.CRITICAL:
        return True
    return (
        finding.severity is FindingSeverity.HIGH
        and finding.category in _BLOCKING_CATEGORIES
        and finding.confidence >= max(0.9, minimum_confidence)
    )


def _consistent_verdict(
    verdict: ReviewVerdict,
    findings: Sequence[ReviewFinding],
    minimum_confidence: float,
) -> ReviewVerdict:
    high_impact = any(
        finding.severity in {FindingSeverity.HIGH, FindingSeverity.CRITICAL} for finding in findings
    )
    blockers = any(_is_meaningful_blocker(finding, minimum_confidence) for finding in findings)
    if verdict is ReviewVerdict.APPROVE and high_impact:
        return ReviewVerdict.REQUEST_CHANGES if blockers else ReviewVerdict.COMMENT
    if verdict is ReviewVerdict.REQUEST_CHANGES and not blockers:
        return ReviewVerdict.COMMENT
    return verdict


def _consistent_risk(
    risk_level: RiskLevel,
    findings: Sequence[ReviewFinding],
) -> RiskLevel:
    if not findings:
        return risk_level
    highest_severity = max(
        findings,
        key=lambda finding: _SEVERITY_RANK[finding.severity],
    ).severity
    minimum_risk = {
        FindingSeverity.INFO: RiskLevel.LOW,
        FindingSeverity.LOW: RiskLevel.LOW,
        FindingSeverity.MEDIUM: RiskLevel.MEDIUM,
        FindingSeverity.HIGH: RiskLevel.HIGH,
        FindingSeverity.CRITICAL: RiskLevel.CRITICAL,
    }[highest_severity]
    return minimum_risk if _RISK_RANK[minimum_risk] > _RISK_RANK[risk_level] else risk_level


def validate_and_filter_review(
    review: ReviewResult | Mapping[str, Any],
    changed_files: Sequence[ChangedFile],
    *,
    min_confidence: float = 0.8,
) -> ReviewResult:
    """Return a posting-safe review after deterministic domain validation.

    Findings are discarded when they are low-confidence or refer to a file
    outside the supplied change set. Findings with a valid changed file but an
    unsafe line mapping remain useful in the general body, while their inline
    location is cleared.
    """

    if (
        isinstance(min_confidence, bool)
        or not isinstance(min_confidence, float | int)
        or not 0.0 <= float(min_confidence) <= 1.0
    ):
        raise ValueError("min_confidence must be between 0 and 1")
    threshold = float(min_confidence)
    result = coerce_review_result(review)
    changed_by_path = _changed_file_map(changed_files)

    eligible = [
        finding
        for finding in result.findings
        if finding.confidence >= threshold and finding.file_path in changed_by_path
    ]
    eligible = deduplicate_findings(eligible)

    mapped_findings: list[ReviewFinding] = []
    had_unmapped_location = False
    for finding in eligible:
        line, start_line = _safe_location(
            finding,
            changed_by_path[finding.file_path],
        )
        if finding.line is not None and line is None:
            had_unmapped_location = True
        elif finding.start_line is not None and start_line is None:
            had_unmapped_location = True
        if line != finding.line or start_line != finding.start_line:
            finding = finding.model_copy(update={"line": line, "start_line": start_line})
        mapped_findings.append(finding)

    limitations = _deduplicate_strings(result.limitations)
    if had_unmapped_location:
        normalized_limitation = _normalize_text(UNMAPPED_LOCATION_LIMITATION)
        if all(_normalize_text(item) != normalized_limitation for item in limitations):
            limitations = [
                *limitations[: MAX_REVIEW_LIST_ITEMS - 1],
                UNMAPPED_LOCATION_LIMITATION,
            ]
    testing_recommendations = _deduplicate_strings(result.testing_recommendations)
    verdict = _consistent_verdict(
        result.verdict,
        mapped_findings,
        threshold,
    )
    risk_level = _consistent_risk(result.risk_level, mapped_findings)
    summary = result.summary
    if not mapped_findings:
        risk_level = RiskLevel.LOW
        if verdict is ReviewVerdict.REQUEST_CHANGES:
            verdict = ReviewVerdict.COMMENT
        if result.findings:
            summary = NO_FINDINGS_SUMMARY

    return ReviewResult(
        summary=summary,
        verdict=verdict,
        confidence=result.confidence,
        risk_level=risk_level,
        findings=mapped_findings,
        testing_recommendations=testing_recommendations,
        limitations=limitations,
    )


# Natural short name for callers that already have a structured result.
validate_review = validate_and_filter_review
