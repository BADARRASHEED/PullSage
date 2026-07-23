"""Prompt-injection boundary tests for the Codex review instructions."""

from pullsage.ai.prompt_builder import build_repair_prompt, build_review_prompt


def test_review_prompt_marks_repository_content_as_untrusted() -> None:
    prompt = build_review_prompt()

    assert "untrusted" in prompt.casefold()
    assert "never follow" in prompt.casefold()
    assert "do not run commands" in prompt.casefold()
    assert "do not access the network" in prompt.casefold()
    assert "return json only" in prompt.casefold()
    assert "never claim tests passed" in prompt.casefold()


def test_repair_prompt_is_constrained_to_one_json_contract() -> None:
    prompt = build_repair_prompt("findings.0.confidence must be <= 1")

    assert "Validation errors:" in prompt
    assert "exactly one JSON object" in prompt
    assert "Do not run commands" in prompt
    assert "SECURITY BOUNDARY" in prompt
    assert "REVIEW TASK" in prompt
    assert "no session memory" in prompt
