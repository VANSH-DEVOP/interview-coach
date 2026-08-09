"""Redaction rules.

Two failure modes matter here and they pull in opposite directions: leaking an
identifier to the provider, and corrupting the resume so the interview degrades.
Both get tests -- the "left alone" cases are as load-bearing as the redactions.
"""

import pytest

from app.services.ai.masking import Redactor, default_redactor, redactor_for

# -- Identifiers that must not reach the provider ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Reach me at ada@example.com",
        "ada.lovelace+jobs@sub.example.co.uk is my address",
        "EMAIL: A.L@EXAMPLE.COM",
    ],
)
def test_emails_are_redacted(text: str) -> None:
    result = default_redactor().apply(text)
    assert "@" not in result.text
    assert "[REDACTED_EMAIL]" in result.text
    assert result.counts == {"EMAIL": 1}


@pytest.mark.parametrize(
    "number",
    [
        "+1 (555) 123-4567",
        "+91 98765 43210",
        "+44 20 7946 0958",
        "(555) 123-4567",
        "555-123-4567",
        "555.123.4567",
        "5551234567",
    ],
)
def test_phone_numbers_are_redacted(number: str) -> None:
    result = default_redactor().apply(f"Phone: {number}. Available weekdays.")
    assert "[REDACTED_PHONE]" in result.text
    # Any surviving run of digits would mean the rule matched only part of the
    # number, which discloses most of it while looking redacted.
    assert not any(char.isdigit() for char in result.text)


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/in/adalovelace",
        "http://ada.dev/portfolio",
        "www.github.com/ada",
        "github.com/ada-lovelace",
        "linkedin.com/in/ada",
    ],
)
def test_urls_are_redacted(url: str) -> None:
    result = default_redactor().apply(f"Portfolio: {url}")
    assert "[REDACTED_URL]" in result.text
    assert "ada" not in result.text.lower().replace("[redacted_url]", "")


@pytest.mark.parametrize(
    ("identifier", "label"),
    [
        ("123-45-6789", "US SSN"),
        ("ABCDE1234F", "India PAN"),
        ("1234 5678 9012", "India Aadhaar"),
    ],
)
def test_identity_numbers_are_redacted(identifier: str, label: str) -> None:
    result = default_redactor().apply(f"{label}: {identifier}")
    assert "[REDACTED_ID]" in result.text, f"{label} survived"
    assert identifier not in result.text


# -- Content that must survive, or the interview degrades ----------------------


@pytest.mark.parametrize(
    "text",
    [
        "Senior Engineer at Acme Corp, 2019-2023",
        "Reduced p99 latency from 1200ms to 300ms",
        "Grew revenue by $1,500,000 in FY2021",
        "Python 3.12, PostgreSQL 16, Kubernetes 1.28",
        "Improved test coverage from 40% to 92%",
        "Led a team of 12 across 3 time zones",
        "B.S. Computer Science, MIT, 2015",
    ],
)
def test_ordinary_resume_content_is_untouched(text: str) -> None:
    assert default_redactor().redact(text) == text


def test_employers_and_technologies_are_not_identifiers() -> None:
    """The interview is built out of these. Redacting them empties the product."""
    text = "Worked at Stripe on Ruby services, then Datadog on Go."
    assert default_redactor().redact(text) == text


# -- Known account holder ------------------------------------------------------


def test_full_name_is_redacted_regardless_of_case_or_spacing() -> None:
    redactor = redactor_for("Ada Lovelace")
    for written in ["Ada Lovelace", "ada lovelace", "Ada  Lovelace", "Lovelace, Ada"]:
        assert "[REDACTED_NAME]" in redactor.redact(written), written


def test_name_parts_are_redacted_on_their_own() -> None:
    result = redactor_for("Ada Lovelace").apply("Lovelace led the analytical engine work.")
    assert result.text.startswith("[REDACTED_NAME] led")


def test_lowercase_words_matching_a_name_part_survive() -> None:
    """Parts match case-sensitively so a surname cannot gut ordinary prose."""
    redactor = redactor_for("Grace Long")
    assert "long-term ownership" in redactor.redact("Drove long-term ownership")


def test_three_letter_first_name_is_redacted_in_body_text() -> None:
    """Resume prose refers to the candidate by first name; that is a leak.

    Regression: matching only the full "Ada Lovelace" left every later "Ada"
    in the clear, which is the whole name disclosed one sentence further down.
    """
    result = redactor_for("Ada Lovelace").apply("Ada led the analytical engine team.")
    assert result.text == "[REDACTED_NAME] led the analytical engine team."


def test_two_letter_name_parts_are_left_alone() -> None:
    """At two characters a name is indistinguishable from "Go", "AI" or "QA"."""
    redactor = redactor_for("Li Chen")
    assert redactor.redact("Li") == "Li"
    assert "[REDACTED_NAME]" in redactor.redact("Li Chen")


def test_longest_literal_wins() -> None:
    """Redacting the first name first would leave the surname in the clear."""
    result = redactor_for("Ada Lovelace").apply("Ada Lovelace, engineer")
    assert result.text == "[REDACTED_NAME], engineer"
    assert result.counts == {"NAME": 1}


def test_blank_identity_falls_back_to_patterns_only() -> None:
    assert redactor_for(None, "  ") is default_redactor()


def test_identity_redactor_still_applies_every_pattern() -> None:
    result = redactor_for("Ada Lovelace").apply(
        "Ada Lovelace | ada@example.com | +1 555-123-4567 | github.com/ada"
    )
    assert set(result.counts) == {"NAME", "EMAIL", "PHONE", "URL"}
    assert "ada" not in result.text.lower().replace("[redacted_name]", "")


# -- Properties ----------------------------------------------------------------


def test_redaction_is_idempotent() -> None:
    """Text can pass a boundary twice; placeholders must not be re-eaten."""
    redactor = redactor_for("Ada Lovelace")
    once = redactor.redact("Ada Lovelace, ada@example.com, +1 555-123-4567")
    assert redactor.redact(once) == once


def test_counts_summarise_without_disclosing_values() -> None:
    result = default_redactor().apply("a@b.com and c@d.com and www.e.com")
    assert result.counts == {"EMAIL": 2, "URL": 1}
    assert result.total == 3
    assert result.summary() == "2 EMAIL, 1 URL"


def test_empty_and_clean_text_report_nothing() -> None:
    assert default_redactor().apply("").counts == {}
    assert default_redactor().apply("Tell me about a hard bug.").counts == {}


def test_default_redactor_is_shared() -> None:
    assert default_redactor() is default_redactor()


def test_redactor_with_no_literals_matches_the_default_behaviour() -> None:
    assert Redactor().redact("a@b.com") == default_redactor().redact("a@b.com")
