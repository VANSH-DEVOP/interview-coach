"""Structure-aware resume chunking.

A resume declares its own structure in headings, and those headings are the
strongest retrieval signal in the document: "what did they study" wants the
block under EDUCATION, and no amount of character counting finds it. The
chunker this replaces packed paragraphs to a character budget and faked overlap
by duplicating the previous chunk's tail.
"""

from app.services.ai.rag import ResumeChunker, _is_heading

RESUME = """\
Dana Okonkwo
Platform Engineer

SUMMARY
Ten years on infrastructure teams.

EXPERIENCE
Principal Engineer, Halcyon (2020-2026)
Ran the migration to Kubernetes across forty services.

Engineer, Tidewater (2016-2020)
Wrote the deployment tooling still in use.

EDUCATION
MSc Distributed Systems, Imperial College London

SKILLS
Go, Rust, Kubernetes, Terraform
"""


def _chunker(**kwargs) -> ResumeChunker:
    return ResumeChunker(**kwargs)


# -- Heading detection ---------------------------------------------------------


def test_known_headings_are_recognised_in_any_case():
    assert _is_heading("EXPERIENCE")
    assert _is_heading("Experience")
    assert _is_heading("Technical Skills")
    assert _is_heading("EDUCATION:")


def test_short_shouty_lines_are_headings_even_when_unknown():
    """Resumes invent their own section names, and an all-caps short line is
    the convention they use for them."""
    assert _is_heading("OPEN SOURCE")


def test_ordinary_text_is_not_a_heading():
    assert not _is_heading("Ran the migration to Kubernetes across forty services.")
    assert not _is_heading("")
    assert not _is_heading("2020-2026")  # no letters
    # Long, even in caps: a sentence someone shouted is not a section.
    assert not _is_heading("I RAN THE ENTIRE MIGRATION TO KUBERNETES ACROSS FORTY SERVICES")


# -- Chunking ------------------------------------------------------------------


def test_each_chunk_carries_the_section_it_came_from():
    chunks = _chunker().chunk(RESUME)

    sections = [chunk.section for chunk in chunks]
    assert "EXPERIENCE" in sections
    assert "EDUCATION" in sections
    assert "SKILLS" in sections


def test_text_before_the_first_heading_is_kept_with_no_section():
    """The name and contact block. Dropping it, or attaching it to whichever
    section happens to come first, both lose or mislabel it."""
    chunks = _chunker().chunk(RESUME)

    assert chunks[0].section is None
    assert "Dana Okonkwo" in chunks[0].content


def test_the_heading_travels_with_the_text_that_gets_embedded():
    """Every chunk has to say which part of the resume it is, including the
    third chunk of a long EXPERIENCE section."""
    chunks = _chunker().chunk(RESUME)
    education = next(chunk for chunk in chunks if chunk.section == "EDUCATION")

    assert education.retrieval_text.startswith("EDUCATION\n")
    # Stored separately from the text so it can be filtered and read in the
    # database without the label being embedded into the content column.
    assert "EDUCATION" not in education.content


def test_ordinals_are_dense_and_in_document_order():
    chunks = _chunker().chunk(RESUME)

    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_no_chunk_repeats_another_chunks_text():
    """The old chunker prepended the previous chunk's last 100 characters
    behind a "\\n...\\n" marker, so the same sentences were embedded twice, could
    be retrieved twice, and paid for prompt budget as a copy."""
    chunks = _chunker().chunk(RESUME)

    assert not any("\n...\n" in chunk.content for chunk in chunks)
    bodies = [chunk.content for chunk in chunks]
    assert len(bodies) == len(set(bodies))


def test_a_long_section_splits_at_paragraph_boundaries_not_mid_sentence():
    """Resume text arrives from a PDF hard-wrapped, so a physical line break is
    a typographic accident. Splitting on one cut a sentence about gRPC latency
    in half, and a query about latency then matched neither piece well."""
    wrapped = (
        "EXPERIENCE\n"
        "Senior Engineer, Northwind (2018-2021)\n"
        "Introduced gRPC between the routing and dispatch services, cutting\n"
        "p99 latency from 340ms to 45ms.\n"
        "\n"
        "Engineer, Calico (2016-2018)\n"
        "Maintained an internal billing service in Django.\n"
    )

    chunks = _chunker(max_chunk_chars=90).chunk(wrapped)

    # Whatever the budget forces, the two halves of that sentence stay together.
    holder = next(chunk for chunk in chunks if "Introduced gRPC" in chunk.content)
    assert "p99 latency from 340ms to 45ms." in holder.content


def test_a_short_resume_stays_in_one_chunk_per_section():
    chunks = _chunker().chunk(RESUME)

    assert [chunk.section for chunk in chunks].count("EXPERIENCE") == 1


def test_empty_input_produces_nothing():
    assert _chunker().chunk("") == []
    assert _chunker().chunk("   \n\n  ") == []


def test_a_resume_with_no_headings_is_still_chunked():
    """Plenty of resumes are one unformatted block. It must not vanish."""
    chunks = _chunker().chunk("Just one paragraph about my career.\n")

    assert len(chunks) == 1
    assert chunks[0].section is None
    assert chunks[0].retrieval_text == "Just one paragraph about my career."


def test_windows_line_endings_do_not_break_heading_detection():
    chunks = _chunker().chunk("SKILLS\r\nGo, Rust\r\n")

    assert chunks[0].section == "SKILLS"
