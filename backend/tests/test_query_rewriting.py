"""Rewriting the retrieval query.

Retrieval used to be issued the nearest phrase to hand: "skills and experience
relevant to {role}" for a new interview, and the entire question plus the
entire answer for a follow-up. Both spend most of their terms on words that
appear everywhere in every resume, which hurts each retriever differently --
the keyword half ORs its terms so common words drag irrelevant chunks up, and
the dense half averages the query into one vector that filler pulls toward the
centre.

Deterministic on purpose. The obvious implementation asks the model to rewrite
the query, at one provider request per retrieval against a ceiling of twenty
per day.
"""

from app.services.ai.query import rewrite, rewrite_for_follow_up, rewrite_for_role


def test_filler_is_dropped():
    assert rewrite("skills and experience relevant to Kafka") == "Kafka"


def test_distinctive_terms_survive_in_order():
    rewritten = rewrite("I built an event pipeline on Kafka and Terraform")

    assert rewritten == "built event pipeline Kafka Terraform"


def test_repeats_are_collapsed():
    """A rambling answer says "cache" six times; the sixth adds nothing to a
    bag-of-words vector but crowds the term cap."""
    assert rewrite("cache cache caching cache") == "cache caching"


def test_technical_tokens_are_preserved():
    """C++, .NET and version numbers are exactly what the keyword half is for,
    and exactly what a naive word split destroys."""
    rewritten = rewrite("Worked with C++ and .NET 8.0 on gRPC services")

    assert "C++" in rewritten
    assert ".NET" in rewritten
    assert "8.0" in rewritten
    assert "gRPC" in rewritten


def test_the_query_is_capped():
    rewritten = rewrite(" ".join(f"term{index}" for index in range(200)), max_terms=5)

    assert len(rewritten.split()) == 5


def test_a_query_of_pure_filler_falls_back_to_the_original():
    """A bad query is bad. An *empty* one retrieves nothing at all, and the
    caller cannot tell those two apart from the result."""
    assert rewrite("tell me about your experience") == "tell me about your experience"


def test_case_is_preserved_for_the_keyword_half():
    """Postgres lowercases in `to_tsvector`, but the dense half sees the string
    as written, and "Kafka" is not "kafka" to every embedding model."""
    assert "Kafka" in rewrite("worked on Kafka")


# -- The two call sites --------------------------------------------------------


def test_a_role_query_is_just_the_role():
    """Four filler terms against one real one, and each of the four matches
    most of any resume."""
    assert rewrite_for_role("Senior Backend Engineer") == "Senior Backend Engineer"


def test_a_follow_up_leads_with_the_answer():
    """Retrieval here looks for what the resume says about the claim the
    candidate just made, so when the cap bites it should bite the interviewer's
    phrasing rather than the candidate's specifics."""
    rewritten = rewrite_for_follow_up(
        question="Tell me about your work on performance.",
        answer="I rewrote the settlement ledger on PostgreSQL.",
    )

    assert rewritten.split()[0] == "rewrote"
    assert "settlement" in rewritten


def test_a_rambling_answer_keeps_its_specifics():
    padding = "and so we were just doing more of this and that with them " * 12
    rewritten = rewrite_for_follow_up(
        question="What did you do?", answer=f"{padding} we adopted Terraform for it"
    )

    assert "Terraform" in rewritten
