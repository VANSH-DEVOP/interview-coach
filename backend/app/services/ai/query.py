"""Turning what the generator knows into a query worth retrieving on.

Retrieval was issued the nearest phrase to hand. For initial questions that was
`"skills and experience relevant to {role}"`, five words of scaffolding around
one useful one. For follow-ups it was the entire question plus the entire
answer -- a candidate's rambling paragraph embedded whole, where the one
concrete claim worth probing is averaged in with every filler word around it.

Rewriting is **deterministic and local**, not a model call. The obvious
implementation asks the provider to rewrite the query, which costs one request
per retrieval against a ceiling of twenty per day -- undoing part 4 to improve
part 5. Stopword removal and length capping get most of the benefit for none of
the quota.

What it does, and why each piece:

- **Drops filler.** Both retrievers are hurt by it, differently: the keyword
  half ORs its terms, so "experience" and "work" match nearly every chunk and
  drag irrelevant ones up the ranking, while the dense half averages the whole
  query into one vector and every meaningless word pulls it toward the centre.
  Postgres removes its own stopwords; these are the *interview*-specific ones
  it has never heard of.
- **Keeps the rare words.** A resume's decisive tokens are the unusual ones --
  Kafka, Terraform, a company name -- and they are exactly what a long answer
  buries.
- **Caps length.** A 400-word answer produces a query vector that is mostly
  noise; the first distinctive terms carry the signal.
"""

from __future__ import annotations

import re

# Words that say "this is an interview" rather than "this is about X". Removing
# them is safe in a way that removing general English is not: they appear in
# essentially every query the generator builds, so they cannot discriminate
# between chunks, and they appear all over a resume, so they match everything.
_FILLER = frozenset(
    {
        "a", "about", "an", "and", "any", "are", "as", "at", "background",
        "be", "been", "but", "by", "can", "candidate", "could", "describe",
        "did", "do", "does", "experience", "explain", "for", "from", "had",
        "has", "have", "how", "i", "in", "into", "is", "it", "its", "just",
        "like", "may", "me", "more", "most", "my", "of", "on", "one", "or",
        "our", "relevant", "role", "skills", "so", "some", "such", "tell",
        "that", "the", "their", "them", "then", "there", "these", "they",
        "this", "to", "up", "us", "use", "used", "using", "very", "was",
        "we", "were", "what", "when", "which", "while", "who", "why", "will",
        "with", "would", "you", "your",
    }
)

# Query terms beyond this add noise faster than signal. Generous enough that a
# focused answer survives whole.
_MAX_TERMS = 40

_WORD = re.compile(r"[A-Za-z0-9+#.]+")


def _terms(text: str) -> list[str]:
    """Distinctive words, in order, without repeats."""
    seen: set[str] = set()
    kept: list[str] = []
    for word in _WORD.findall(text):
        lowered = word.lower().strip(".")
        if not lowered or lowered in _FILLER or len(lowered) < 2:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        kept.append(word)
    return kept


def rewrite(text: str, *, max_terms: int = _MAX_TERMS) -> str:
    """Reduce free text to the terms worth retrieving on.

    Falls back to the original when nothing survives. A query of pure filler is
    a bad query, but an *empty* one retrieves nothing at all, and the caller has
    no way to tell those apart from the result.
    """
    terms = _terms(text)[:max_terms]
    return " ".join(terms) if terms else text.strip()


def rewrite_for_role(role: str) -> str:
    """The retrieval query for a new interview.

    Just the role's own words. The scaffolding this replaces -- "skills and
    experience relevant to" -- was four filler terms against one real one, and
    every one of them matches most of a resume.
    """
    return rewrite(role)


def rewrite_for_follow_up(question: str, answer: str) -> str:
    """The retrieval query for a follow-up.

    Keyed on the answer first, then the question. Retrieval here is looking for
    what the resume says about the claim the candidate just made, so when the
    cap bites it should bite on the interviewer's phrasing rather than on the
    candidate's specifics.
    """
    return rewrite(f"{answer}\n{question}")
