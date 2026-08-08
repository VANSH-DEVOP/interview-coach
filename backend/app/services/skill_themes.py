"""Group free-text feedback into recurring themes.

Reports come back with strengths and weaknesses phrased differently every time
-- "add metrics", "quantify your impact", "no numbers given" are one problem
wearing three hats. Counting the raw strings would produce a list of unique
sentences and tell the user nothing.

So the strings are matched against a fixed taxonomy by keyword. This is
deliberately dumb and deliberately deterministic: asking the model to
categorise its own output would cost another call per report, vary between
runs, and be untestable. The cost is that phrasing outside the keyword sets
lands in "Other", which is honest -- an uncategorised item is visible rather
than silently dropped.

Keywords are matched as substrings against the lower-cased text, so stems like
"quantif" cover quantify / quantified / quantifying.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

OTHER = "Other"

# Ordered only for readability; a single item can match several themes, because
# "structure your answer around a measurable outcome" genuinely is both.
THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Quantifying impact": (
        "quantif", "metric", "measur", "number", "percentage", "roi", "data-driven",
    ),
    "Structure and clarity": (
        "star method", "star ", "structur", "rambl", "concise", "clarity", "clear",
        "organis", "organiz", "verbose",
    ),
    "Concrete examples": (
        "example", "specific", "vague", "concrete", "generic", "anecdote", "detail",
    ),
    "Technical depth": (
        "technical depth", "deeper", "shallow", "fundamental", "internals",
        "trade-off", "tradeoff", "implementation",
    ),
    "System design": (
        "system design", "scal", "architect", "distributed", "throughput", "latency",
        "bottleneck",
    ),
    "Testing and quality": (
        "test", "coverage", "edge case", "regression", "reliability", "quality",
    ),
    "Collaboration": (
        "team", "collaborat", "stakeholder", "communicat", "mentor", "conflict",
        "cross-functional",
    ),
    "Ownership and initiative": (
        "ownership", "initiative", "leadership", "led ", "drove", "responsib",
        "proactive",
    ),
}


def classify(text: str) -> list[str]:
    """Themes a single feedback line belongs to. Never empty."""
    lowered = text.lower()
    matched = [
        theme
        for theme, keywords in THEME_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    ]
    return matched or [OTHER]


def summarise(entries: list[Any], *, limit: int = 6) -> list[dict[str, Any]]:
    """Count themes across feedback lines, most frequent first.

    Args:
        entries: Raw strengths or weaknesses from reports. Non-string and blank
            items are ignored -- the column is JSONB and the model has been
            known to return dicts.
        limit: How many themes to return.

    Returns:
        [{"theme": str, "count": int, "examples": [str, ...]}], where examples
        are the user's actual feedback wording, so the label is never the only
        thing on screen.
    """
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)

    for entry in entries:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if not text:
            continue
        for theme in classify(text):
            counts[theme] += 1
            if len(examples[theme]) < 2 and text not in examples[theme]:
                examples[theme].append(text)

    # Sort by count, then alphabetically, so equal counts are stable rather
    # than shuffling between requests.
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        {"theme": theme, "count": count, "examples": examples[theme]}
        for theme, count in ordered[:limit]
    ]
