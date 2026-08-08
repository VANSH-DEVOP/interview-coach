"""Grouping free-text feedback into recurring themes.

The whole point is that "add metrics", "quantify your impact" and "no numbers
were given" collapse into one theme. If they don't, the feature degrades into a
list of unique sentences, which is what the raw reports already were.
"""

import pytest

from app.services.skill_themes import OTHER, classify, summarise

# -- classify ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Quantify your achievements with metrics.", "Quantifying impact"),
        ("No numbers were given for the impact.", "Quantifying impact"),
        ("Use the STAR method to structure answers.", "Structure and clarity"),
        ("Answers were rambling and hard to follow.", "Structure and clarity"),
        ("Give a specific example next time.", "Concrete examples"),
        ("Responses were vague.", "Concrete examples"),
        ("Needs more technical depth on internals.", "Technical depth"),
        ("Did not discuss how the system would scale.", "System design"),
        ("No mention of testing or edge cases.", "Testing and quality"),
        ("Strong collaboration with stakeholders.", "Collaboration"),
        ("Showed clear ownership of the outcome.", "Ownership and initiative"),
    ],
)
def test_phrasings_map_to_the_expected_theme(text, expected):
    assert expected in classify(text)


def test_differently_worded_feedback_lands_in_one_theme():
    phrasings = [
        "Quantify your achievements.",
        "Add metrics to show impact.",
        "There were no numbers to measure the result.",
    ]
    themes = [classify(text) for text in phrasings]
    assert all("Quantifying impact" in t for t in themes)


def test_matching_is_case_insensitive():
    assert "Quantifying impact" in classify("QUANTIFY THE IMPACT")


def test_a_line_can_belong_to_several_themes():
    # Genuinely about both, and forcing a single bucket would lose one.
    themes = classify("Structure the answer around a measurable outcome.")
    assert "Structure and clarity" in themes
    assert "Quantifying impact" in themes


def test_unrecognised_feedback_is_surfaced_not_dropped():
    themes = classify("Wear a nicer shirt to the interview.")
    # Visible as Other rather than silently discarded, so the taxonomy's gaps
    # are apparent instead of invisible.
    assert themes == [OTHER]


def test_classify_is_never_empty():
    assert classify("") == [OTHER]


# -- summarise -----------------------------------------------------------------


def test_summarise_counts_and_ranks_themes():
    entries = [
        "Quantify your impact.",
        "Add metrics.",
        "Use numbers.",
        "Give a specific example.",
    ]

    result = summarise(entries)

    assert result[0]["theme"] == "Quantifying impact"
    assert result[0]["count"] == 3
    assert result[1]["theme"] == "Concrete examples"
    assert result[1]["count"] == 1


def test_summarise_returns_the_users_own_wording_as_evidence():
    # A bare label ("Quantifying impact: 3") is not actionable on its own.
    result = summarise(["Quantify your impact.", "Add metrics."])

    examples = result[0]["examples"]
    assert "Quantify your impact." in examples
    assert len(examples) <= 2


def test_summarise_ignores_blank_and_non_string_entries():
    # The column is JSONB and the model has returned dicts before now.
    result = summarise(["Add metrics.", "", "   ", {"point": "ignored"}, None, 42])

    assert len(result) == 1
    assert result[0]["count"] == 1


def test_summarise_of_nothing_is_empty():
    assert summarise([]) == []


def test_summarise_respects_the_limit():
    entries = [
        "Quantify impact.",
        "Use the STAR method.",
        "Give a specific example.",
        "More technical depth.",
        "Discuss how it scales.",
        "No tests mentioned.",
        "Work with stakeholders.",
        "Take ownership.",
    ]

    assert len(summarise(entries, limit=3)) == 3


def test_ties_are_ordered_stably():
    # Equal counts must not reshuffle between requests.
    entries = ["Quantify impact.", "Give a specific example."]

    first = [t["theme"] for t in summarise(entries)]
    second = [t["theme"] for t in summarise(entries)]

    assert first == second == sorted(first)
