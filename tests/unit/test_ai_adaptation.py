"""
Unit test suite for Checkpoint 8: Large-Source Bounded Adaptation.
Tests:
- Semantic chunking with paragraph/sentence boundaries and budget limits.
- Structured fact distillation and dossier merging.
- Typed AdaptationBudget bounds and AdaptationUsage persistence across failover.
- Literal mode zero-adaptation guarantee.
- Integration with execute_with_failover: same-provider adaptation first.
"""

from unittest.mock import MagicMock, patch
import pytest

from herald.ai.adaptation import (
    adapt_source_text,
    distill_chunk,
    merge_dossier,
    semantic_chunk_text,
)
from herald.ai.errors import (
    AIContextExceededError,
    AIRequestTooLargeError,
)
from herald.ai.failover import execute_with_failover
from herald.ai.literal_provider import LiteralProvider
from herald.ai.policy import AdaptationBudget, AdaptationUsage
from herald.db.models import PodcastJob


def test_semantic_chunking_boundaries_and_limits():
    """Verify semantic chunking splits on paragraphs and enforces chunk limits."""
    # 3 paragraphs within budget
    para1 = "Paragraph 1 with some introductory context and narrative facts."
    para2 = "Paragraph 2 with crucial statistics: 42 percent growth in Q3."
    para3 = "Paragraph 3 with conclusion and final takeaways."
    source = f"{para1}\n\n{para2}\n\n{para3}"

    chunks = semantic_chunk_text(source, max_chunk_chars=100, max_chunks=5)
    assert len(chunks) == 3
    assert "Paragraph 1" in chunks[0]
    assert "Paragraph 2" in chunks[1]
    assert "Paragraph 3" in chunks[2]

    # Giant paragraph splits on sentence boundaries
    giant_para = "Sentence one is clear. Sentence two has 100 items! Sentence three concludes it."
    chunks_sub = semantic_chunk_text(giant_para, max_chunk_chars=40, max_chunks=10)
    assert len(chunks_sub) > 1
    for c in chunks_sub:
        assert len(c) <= 45

    # Exceeding max_chunks raises AIRequestTooLargeError
    overflow_source = "\n\n".join([f"Paragraph {i} has content." for i in range(15)])
    with pytest.raises(AIRequestTooLargeError) as exc_info:
        semantic_chunk_text(overflow_source, max_chunk_chars=30, max_chunks=5)

    assert "exceeds maximum adaptation chunk budget" in str(exc_info.value)


def test_distill_chunk_and_merge_dossier():
    """Verify fact distillation preserves numbers and headers, and dossier compiles properly."""
    chunk = (
        "## Financial Results\n\n"
        "The company reported revenue of $4.2B in 2026. This represents a 25% increase over 2025. "
        "CEO Jane Doe stated that operations were strong across all sectors.\n\n"
        "Further expansion into international markets is scheduled for November 2026."
    )
    distilled = distill_chunk(chunk, chunk_index=0, total_chunks=2)
    assert "### SECTION 1/2" in distilled
    assert "## Financial Results" in distilled
    assert "$4.2B" in distilled
    assert "25%" in distilled

    dossier = merge_dossier([distilled, "### SECTION 2/2\nPart 2"], title="Annual Report")
    assert "# ADAPTED RESEARCH & SOURCE DOSSIER" in dossier
    assert "SOURCE TITLE: Annual Report" in dossier
    assert "COMPILED SECTIONS: 2" in dossier


def test_literal_mode_zero_adaptation_guarantee():
    """Verify Literal mode performs zero adaptation mutation."""
    lit = LiteralProvider()
    huge_source = "\n\n".join([f"Long text section {i}" for i in range(50)])
    result = adapt_source_text(huge_source, provider=lit)
    assert result == huge_source


def test_adaptation_budget_limits():
    """Verify AdaptationUsage enforces limits on chunks, depth, and time."""
    budget = AdaptationBudget(max_chunks=2, max_reduction_depth=1)
    usage = AdaptationUsage(chunks_processed=5)

    with pytest.raises(AIRequestTooLargeError) as exc_info:
        usage.check_budget(budget)
    assert "chunk budget" in str(exc_info.value)

    usage2 = AdaptationUsage(reduction_depth=3)
    with pytest.raises(AIRequestTooLargeError) as exc_info2:
        usage2.check_budget(budget)
    assert "reduction depth" in str(exc_info2.value)


def test_failover_same_provider_adaptation_first():
    """Verify execute_with_failover triggers same-provider adaptation on size error before failing over."""
    job = PodcastJob(
        id="adapt-job-1",
        ai_provider_chain_json=[
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            {"provider": "openai", "model": "gpt-4o"},
        ],
        ai_failover_index=0,
        source_text="Long initial source text needing adaptation",
    )

    calls = []

    def mock_execute(prov, attempt):
        calls.append((prov.provider_name, attempt))
        if attempt == 1:
            raise AIContextExceededError("Context limit exceeded on Groq", provider="groq", http_status=413)
        return "SUCCESS_AFTER_ADAPTATION"

    with patch("herald.ai.failover.is_provider_configured", return_value=True), \
         patch("herald.ai.failover.adapt_source_text", return_value="Condensed text") as mock_adapt:
        result = execute_with_failover(
            job=job,
            operation="script_generation",
            execute_fn=mock_execute,
            source_text=job.source_text,
        )

    assert result == "SUCCESS_AFTER_ADAPTATION"
    assert mock_adapt.called
    # Succeeded on same provider (Groq, attempt 2) without advancing cursor
    assert len(calls) == 2
    assert calls[0] == ("Groq", 1)
    assert calls[1] == ("Groq", 2)
    assert job.ai_failover_index == 0
    assert job.ai_effective_provider == "groq"
