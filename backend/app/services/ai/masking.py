"""Redaction of direct identifiers before text leaves for a third-party model.

Every prompt and every embedding request in this application is an HTTP call to
Google. The resume behind a personalised interview is the densest personal data
we hold, and the transcript is the candidate's own words -- neither needs to
carry contact details for the model to do its job.

What is redacted, and what deliberately is not
----------------------------------------------
Redacted: the *direct* identifiers. Email addresses, phone numbers, URLs (a
LinkedIn or portfolio link identifies a person as precisely as a name does),
government identity numbers, and -- when the caller supplies them -- the
account holder's own name as a literal string.

Not redacted: employers, job titles, schools, technologies, dates. Those are
the interview. Stripping them would leave the model nothing to ask about, and
none of them is a direct identifier. This is pseudonymisation, not anonymity:
a determined reader of a redacted resume could still often work out who it
belongs to. The goal is that the provider does not receive a name attached to
a phone number attached to an email address.

Redaction is one-way
--------------------
There is no placeholder-to-value map and nothing is restored on the way back,
so no later bug can re-attach a redacted value to model output. The cost is
that a model which echoes a placeholder shows the user "[REDACTED_EMAIL]".
That is self-explanatory, and rare -- placeholders sit in resume context, not
in the instructions the model is answering.

Bypass is not possible by construction
--------------------------------------
The two egress points (`ModelClient.generate_json` and
`EmbeddingService.embed_text`) redact their own input and default to
`default_redactor()` when a caller passes nothing. A new call site cannot
forget to redact; the most it can do is fail to supply the account holder's
name, which downgrades to pattern-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Sequence

# Applied in this order. URLs go first: a profile URL can contain an "@", and
# letting the email rule reach it first would eat the host and leave the handle
# behind -- a half-redacted identifier is worse than either outcome, because it
# looks redacted.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("URL", re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)),
    # Bare profile links, which resumes write without a scheme far more often
    # than they write them with one.
    (
        "URL",
        re.compile(
            r"\b(?:linkedin|github|gitlab|medium|behance|dribbble|stackoverflow)"
            r"\.com/\S+",
            re.IGNORECASE,
        ),
    ),
    ("EMAIL", re.compile(r"\b[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")),
    # Identity numbers before phone numbers: a US SSN is 3-2-4 and a phone is
    # 3-3-4, so they do not actually collide, but the ordering makes that a
    # property of the code rather than of the digit counts.
    ("ID", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),  # US SSN
    ("ID", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),  # India PAN
    ("ID", re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")),  # India Aadhaar
    # Phone rules all demand separators or an explicit country code. A rule
    # loose enough to catch every format also eats "reduced p99 from 1200ms to
    # 300ms" and "2019-2023", and corrupting the resume is a worse failure than
    # missing an unusually formatted number.
    ("PHONE", re.compile(r"\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,5}){1,4}")),
    ("PHONE", re.compile(r"\b\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")),
    ("PHONE", re.compile(r"\b\d{10}\b")),
)

# Below this length a name part collides with too much ordinary text to be
# worth matching: at two characters "Li" and "Wu" are indistinguishable from
# "Go", "AI", "ML", "QA" and "UI", all of which appear constantly in resumes.
# Three is the lowest length where case-sensitive matching carries its weight,
# and it has to be this low -- plenty of real first names are three letters,
# and a resume body that says "Ada led the team" leaks the name outright if
# only the full "Ada Lovelace" is matched.
#
# The residual cost is a capitalised collision: a candidate surnamed "Sun"
# loses "Sun Microsystems" to a placeholder. One redacted word is the cheaper
# error, and the case-sensitivity below keeps it to capitalised occurrences.
_MIN_NAME_PART = 3


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """Redacted text plus a per-category tally, for logging and for tests."""

    text: str
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        """Human-readable tally, e.g. "2 EMAIL, 1 PHONE". Never includes values."""
        return ", ".join(f"{n} {name}" for name, n in sorted(self.counts.items()))


class Redactor:
    """Replaces direct identifiers with category placeholders.

    Stateless and reusable. `literals` are names the caller knows belong to the
    account holder -- the one identifier no pattern can recognise on its own.
    They are matched as NAME, so passing anything a pattern already covers (an
    email address, say) still redacts it but files it under the wrong category
    and inflates the NAME tally; pass names only.
    """

    def __init__(self, literals: Sequence[str] = ()) -> None:
        self._literal_rules = tuple(_compile_literals(literals))

    def apply(self, text: str) -> RedactionResult:
        counts: dict[str, int] = {}

        # Literals first. The account holder's name is the one identifier we
        # know rather than infer, so it should not be left to compete with a
        # pattern that might have consumed the surrounding text.
        for category, pattern in self._literal_rules + _PATTERNS:
            replacement = f"[REDACTED_{category}]"
            text, hits = pattern.subn(replacement, text)
            if hits:
                counts[category] = counts.get(category, 0) + hits

        return RedactionResult(text=text, counts=counts)

    def redact(self, text: str) -> str:
        return self.apply(text).text


def _compile_literals(literals: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Build word-bounded rules for known-identifying strings.

    A full name is matched as a unit and case-insensitively, tolerating any
    run of whitespace or commas between its parts so that "Ada Lovelace",
    "Ada  Lovelace" and "Lovelace, Ada" all match. Individual parts are matched
    case-sensitively, because "Will" appearing capitalised mid-resume is
    usually the person and "will" is usually the verb.

    Longest first: otherwise redacting "Ada" would leave "[REDACTED_NAME]
    Lovelace", which discloses the surname and looks like a bug.
    """
    seen: set[str] = set()
    rules: list[tuple[str, re.Pattern[str]]] = []

    def add(value: str, *, ignore_case: bool) -> None:
        key = value.casefold()
        if key in seen:
            return
        seen.add(key)
        parts = [re.escape(p) for p in value.split()]
        body = r"[\s,]+".join(parts)
        flags = re.IGNORECASE if ignore_case else 0
        rules.append(("NAME", re.compile(rf"\b{body}\b", flags)))

    candidates: list[tuple[str, bool]] = []
    for literal in literals:
        value = (literal or "").strip()
        if len(value) < _MIN_NAME_PART:
            continue
        candidates.append((value, True))
        parts = value.split()
        if len(parts) > 1:
            candidates.extend(
                (part, False) for part in parts if len(part) >= _MIN_NAME_PART
            )

    for value, ignore_case in sorted(candidates, key=lambda c: -len(c[0])):
        add(value, ignore_case=ignore_case)
    return rules


@lru_cache(maxsize=1)
def default_redactor() -> Redactor:
    """Pattern-only redactor, used when a caller supplies no identity.

    This is the floor, not the intended configuration: it is what every egress
    point falls back to so that forgetting to plumb an identity degrades the
    redaction rather than disabling it.
    """
    return Redactor()


def redactor_for(*names: str | None) -> Redactor:
    """Redactor for a known account holder. Blank values are ignored."""
    values = [name for name in names if name and name.strip()]
    if not values:
        return default_redactor()
    return Redactor(values)
