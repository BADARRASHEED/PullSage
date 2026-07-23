"""Unit tests for strict structured review filtering and line mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pullsage.github.models import ChangedFile, ChangedFileStatus
from pullsage.reviews.formatter import format_review_markdown
from pullsage.reviews.models import ReviewResult, ReviewVerdict, RiskLevel
from pullsage.reviews.validation import (
    extract_changed_lines,
    validate_and_filter_review,
)


def _changed_file() -> ChangedFile:
    return ChangedFile(
        filename="src/example.py",
        status=ChangedFileStatus.MODIFIED,
        additions=3,
        deletions=1,
        changes=4,
        patch=(
            "@@ -8,3 +8,5 @@\n"
            " existing = True\n"
            "-old_value = 1\n"
            "+new_value = 1\n"
            "+unsafe = data[0]\n"
            " return new_value\n"
            "+log(new_value)\n"
        ),
    )


def _review(findings: list[dict[str, object]]) -> ReviewResult:
    return ReviewResult.model_validate(
        {
            "summary": "The change contains findings that require review.",
            "verdict": "approve",
            "confidence": 0.95,
            "risk_level": "low",
            "findings": findings,
            "testing_recommendations": ["Exercise the new branch."],
            "limitations": ["Tests were not executed."],
        }
    )


def _finding(
    finding_id: str,
    *,
    confidence: float,
    path: str = "src/example.py",
    line: int | None = 10,
    severity: str = "medium",
    category: str = "correctness",
) -> dict[str, object]:
    return {
        "id": finding_id,
        "title": "Unchecked empty input",
        "body": "Indexing this value fails when the input is empty.",
        "severity": severity,
        "category": category,
        "confidence": confidence,
        "file_path": path,
        "line": line,
        "start_line": None,
        "side": "RIGHT",
        "suggested_fix": "Guard the empty input before indexing.",
        "evidence": "The new expression reads index zero without a length check.",
    }


def test_extracts_only_added_right_side_lines() -> None:
    assert extract_changed_lines(_changed_file().patch) == frozenset({9, 10, 12})


def test_filters_confidence_invalid_paths_and_duplicates() -> None:
    review = _review(
        [
            _finding("strong", confidence=0.96),
            _finding("strong", confidence=0.90),
            _finding("low-confidence", confidence=0.79, line=11),
            _finding(
                "wrong-path",
                confidence=0.99,
                path="src/not_changed.py",
            ),
        ]
    )

    validated = validate_and_filter_review(
        review,
        [_changed_file()],
        min_confidence=0.8,
    )

    assert [finding.id for finding in validated.findings] == ["strong"]
    assert validated.findings[0].confidence == 0.96


def test_invalid_inline_line_becomes_general_finding() -> None:
    review = _review([_finding("bad-line", confidence=0.95, line=999)])

    validated = validate_and_filter_review(review, [_changed_file()])

    assert len(validated.findings) == 1
    assert validated.findings[0].line is None
    assert any("mapped safely" in item for item in validated.limitations)


def test_system_location_limitation_respects_twenty_item_schema_bound() -> None:
    review_payload = _review(
        [_finding("bad-line", confidence=0.95, line=999)]
    ).model_dump(mode="python")
    review_payload["limitations"] = [f"Caller limitation {index}." for index in range(20)]
    review = ReviewResult.model_validate(review_payload)

    validated = validate_and_filter_review(review, [_changed_file()])

    assert len(validated.limitations) == 20
    assert validated.limitations[-1].startswith("One or more findings")
    ReviewResult.model_validate(validated.model_dump(mode="python"))


def test_approve_is_not_allowed_with_blocking_finding() -> None:
    review = _review(
        [
            _finding(
                "blocking-security",
                confidence=0.97,
                severity="high",
                category="security",
            )
        ]
    )

    validated = validate_and_filter_review(review, [_changed_file()])

    assert validated.verdict is ReviewVerdict.REQUEST_CHANGES
    assert validated.risk_level is RiskLevel.HIGH


def test_all_removed_findings_produce_honest_empty_result() -> None:
    review = _review([_finding("weak", confidence=0.25)])

    validated = validate_and_filter_review(review, [_changed_file()])

    assert validated.findings == []
    assert "No high-confidence defects" in validated.summary
    assert validated.risk_level is RiskLevel.LOW


def test_structured_output_and_github_body_are_size_bounded() -> None:
    findings: list[dict[str, object]] = []
    for index in range(50):
        finding = _finding(f"finding-{index}", confidence=0.99)
        finding.update(
            {
                "title": f"Finding {index}",
                "body": "b" * 3_000,
                "evidence": "e" * 1_500,
                "suggested_fix": "f" * 3_000,
            }
        )
        findings.append(finding)
    review = _review(findings)

    body = format_review_markdown(review)

    assert len(body) <= 60_000
    assert "Additional finding detail was truncated" in body
    assert "### Limitations" in body
    assert "AI-assisted review by PullSage" in body

    invalid = review.model_dump(mode="python")
    invalid["summary"] = "s" * 4_001
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(invalid)


def test_diff_parser_keeps_added_source_lines_starting_with_pluses() -> None:
    patch = "@@ -1 +1,2 @@\n-old\n++++actual_source_text\n+normal\n"

    assert extract_changed_lines(patch) == frozenset({1, 2})
