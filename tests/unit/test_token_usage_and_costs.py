"""
Unit tests for AI token usage and exact-match cost accounting.
Tests truthful calculation, presentation across cards, and backward compatibility.
"""

from datetime import UTC, datetime
from herald.db.models import AIInteraction, PodcastJob, PodcastTTSChunk, RequestMode
from herald.services.token_cost import (
    PRICING_TABLE,
    aggregate_job_tokens_and_cost,
    calculate_interaction_cost,
)
from herald.telegram.formatters import (
    format_completion,
    format_diagnostics_card,
    format_first_chunk_progress,
    format_queued,
)


def test_standard_priced_model_calculation():
    """Verify exact cost calculation for standard priced models (e.g. Gemini 3.5 Flash)."""
    # gemini-3.5-flash: prompt $0.075 / 1M, completion $0.30 / 1M
    inter1 = AIInteraction(
        id="call-1",
        job_id="job-1",
        provider="gemini",
        model="gemini-3.5-flash",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
    )
    cost, known = calculate_interaction_cost(inter1)
    assert known is True
    assert round(cost, 4) == round(0.075 + 0.30, 4)

    summary = aggregate_job_tokens_and_cost([inter1])
    assert summary.call_count == 1
    assert summary.total_tokens == 2_000_000
    assert summary.is_cost_complete is True
    assert summary.is_cost_available is True
    assert summary.cost_display == "$0.38"


def test_unpriced_model_never_shows_zero():
    """Verify that unpriced models return 'unavailable' and NEVER '$0.00'."""
    inter = AIInteraction(
        id="call-unpriced",
        job_id="job-unpriced",
        provider="some_new_vendor",
        model="future-ai-v99",
        prompt_tokens=5000,
        completion_tokens=1500,
        total_tokens=6500,
    )
    cost, known = calculate_interaction_cost(inter)
    assert known is False
    assert cost is None

    summary = aggregate_job_tokens_and_cost([inter])
    assert summary.total_tokens == 6500
    assert summary.is_cost_complete is False
    assert summary.is_cost_available is False
    assert summary.cost_display == "unavailable"
    assert "$0.00" not in summary.cost_display


def test_partially_priced_job_never_shows_zero():
    """Verify that jobs with mixed priced and unpriced models show 'partial' cost and NEVER '$0.00'."""
    inter_known = AIInteraction(
        id="call-known",
        job_id="job-mix",
        provider="gemini",
        model="gemini-3.5-flash",
        prompt_tokens=10_000,
        completion_tokens=2_000,
        total_tokens=12_000,
    )
    inter_unknown = AIInteraction(
        id="call-unknown",
        job_id="job-mix",
        provider="custom_llm",
        model="custom-v1",
        prompt_tokens=20_000,
        completion_tokens=5_000,
        total_tokens=25_000,
    )

    summary = aggregate_job_tokens_and_cost([inter_known, inter_unknown])
    assert summary.total_tokens == 37_000
    assert summary.is_cost_complete is False
    assert summary.is_cost_available is True
    assert "(partial)" in summary.cost_display
    assert summary.cost_display != "$0.00"


def test_local_model_and_zero_calls_show_zero():
    """Verify that local Ollama models and empty interaction lists truthfully show $0.00."""
    inter_ollama = AIInteraction(
        id="call-ollama",
        job_id="job-local",
        provider="ollama",
        model="llama3.2",
        prompt_tokens=10_000,
        completion_tokens=2_000,
        total_tokens=12_000,
    )
    summary_ollama = aggregate_job_tokens_and_cost([inter_ollama])
    assert summary_ollama.cost_display == "$0.00"

    summary_empty = aggregate_job_tokens_and_cost([])
    assert summary_empty.cost_display == "$0.00"
    assert summary_empty.tokens_display == "0 tokens"


def test_formatters_truthful_presentation():
    """Verify format_queued, format_first_chunk_progress, and format_completion with AI costs."""
    job = PodcastJob(
        id="cost-job-12345678",
        request_mode="research",
        research_depth="deep",
        research_model="gemini-3.5-flash",
        custom_voice="af_heart",
        custom_speed=1.0,
        source_text="Sample text for costing test.",
        script_json={
            "episode_title": "Cost Accounting Episode",
            "episode_description": "Testing exact-match cost display.",
            "segments": [{"narration": "Narration text"}],
        },
        audio_duration_seconds=120,
    )

    call1 = AIInteraction(
        id="call-1",
        job_id=job.id,
        provider="gemini",
        model="gemini-3.5-flash",
        prompt_tokens=100_000,
        completion_tokens=20_000,
        total_tokens=120_000,
    )
    job.ai_interactions = [call1]

    # 1. Queued
    q_msg = format_queued(job, job.script_json)
    assert "AI Model:" in q_msg
    assert "AI Usage:" in q_msg
    assert "120,000 tokens" in q_msg

    # 2. First Chunk Progress
    p_msg = format_first_chunk_progress(job, total_chunks=4, eta_range="2-3 minutes")
    assert "AI Usage:" in p_msg
    assert "120,000 tokens" in p_msg

    # 3. Completion
    c_msg = format_completion(job, actual_chunks_count=4, file_size_bytes=2_000_000)
    assert "AI Cost:" in c_msg
    assert "120,000 tokens" in c_msg
    assert len(c_msg) <= 1024


def test_literal_mode_truthful_presentation():
    """Literal mode must report 0 AI calls and $0.00 cost without claiming AI model."""
    job = PodcastJob(
        id="literal-job-1234",
        request_mode="literal",
        custom_voice="af_bella",
        custom_speed=1.1,
        source_text="Literal text content here.",
        script_json={
            "episode_title": "Literal Test",
            "episode_description": "No AI was harmed.",
            "segments": [{"narration": "Literal text content here."}],
        },
        audio_duration_seconds=60,
    )

    q_msg = format_queued(job, job.script_json)
    assert "AI Model:" not in q_msg

    p_msg = format_first_chunk_progress(job, total_chunks=2, eta_range="1-2 minutes")
    assert "Literal reader (zero AI calls)" in p_msg
    assert "AI Usage:" not in p_msg

    c_msg = format_completion(job, actual_chunks_count=2, file_size_bytes=1_000_000)
    assert "AI Model:" not in c_msg
    assert "AI Cost:" not in c_msg


def test_missing_breakdown_symmetric_vs_asymmetric():
    """Verify that calculate_interaction_cost falls back only for symmetric rates and refuses to guess for asymmetric."""
    # Groq compound-mini: prompt $0.20 / 1M, completion $0.20 / 1M (symmetric)
    inter_sym = AIInteraction(
        id="call-sym",
        job_id="job-1",
        provider="groq",
        model="groq/compound-mini",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=1_000_000,
    )
    cost, known = calculate_interaction_cost(inter_sym)
    assert known is True
    assert cost == 0.20

    # Gemini 3.5 Flash: prompt $0.075 / 1M, completion $0.30 / 1M (asymmetric)
    inter_asym = AIInteraction(
        id="call-asym",
        job_id="job-1",
        provider="gemini",
        model="gemini-3.5-flash",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=1_000_000,
    )
    cost, known = calculate_interaction_cost(inter_asym)
    assert known is False
    assert cost is None


def test_token_bearing_completeness():
    """Verify that zero-token interactions do not break is_cost_complete."""
    inter_zero = AIInteraction(
        id="call-zero",
        job_id="job-1",
        provider="unregistered_provider",
        model="unregistered_model",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
    )
    inter_paid = AIInteraction(
        id="call-paid",
        job_id="job-1",
        provider="gemini",
        model="gemini-3.5-flash",
        prompt_tokens=1000,
        completion_tokens=1000,
        total_tokens=2000,
    )

    summary = aggregate_job_tokens_and_cost([inter_zero, inter_paid])
    assert summary.is_cost_complete is True
    assert summary.is_cost_available is True
