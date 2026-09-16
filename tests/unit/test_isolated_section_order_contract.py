"""
Unit and regression tests for Isolated Section Order Contract.

Tests:
A. Global Section 3 returns one segment with order=3 -> accepted locally, narration preserved, Herald section_index remains 3
B. Global Section 11 returns order=11 -> accepted/rebased locally
C. Isolated response [3,4] -> safely rebased to [1,2]
D. Proper local response [1] -> unchanged
E. Duplicate [3,3] -> rejected
F. Gap [3,5] -> rejected
G. Non-monotonic [4,3] -> rejected
H. Normal full PodcastScriptResponse validation still rejects a response beginning at 3.
I. Retry topology: global-section/local-order mismatch must not trigger another provider call.
J. Resume/diagnostics continue reporting the correct GLOBAL section index.
K. Provider neutrality: Gemini, OpenAI, Anthropic, Cloudflare, Ollama all normalize isolated sections.
"""

import json
from unittest.mock import MagicMock, patch

import pydantic
import pytest

from herald.ai.anthropic_provider import AnthropicProvider
from herald.ai.cloudflare_provider import CloudflareProvider
from herald.ai.gemini_provider import GeminiProvider
from herald.ai.long_form import EvidenceScope, generate_single_section
from herald.ai.ollama_provider import OllamaProvider
from herald.ai.openai_provider import OpenAIProvider
from herald.ai.schema import (
    PodcastScriptResponse,
    PodcastSegment,
    is_isolated_section_instruction,
    parse_isolated_section_response,
    rebase_isolated_section_orders,
)
from herald.db.models import PodcastJob


# ---------------------------------------------------------------------------
# Test C: Isolated response [3, 4] -> safely rebased to [1, 2]
# ---------------------------------------------------------------------------
def test_c_isolated_response_3_4_rebased_to_1_2():
    data = {
        "episode_title": "Pepsi in the USSR",
        "episode_description": "Pepsi trading Russia for warships",
        "segments": [
            {"order": 3, "heading": "Naval Negotiations", "narration": "In May 1989, Pepsi signed a trade agreement."},
            {"order": 4, "heading": "The Flotilla", "narration": "The fleet included seventeen submarines and a cruiser."},
        ],
        "warnings": [],
    }
    rebased = rebase_isolated_section_orders(data)
    assert [s["order"] for s in rebased["segments"]] == [1, 2]
    assert rebased["segments"][0]["heading"] == "Naval Negotiations"
    assert rebased["segments"][1]["narration"] == "The fleet included seventeen submarines and a cruiser."

    # Verify parse_isolated_section_response produces valid PodcastScriptResponse
    resp = parse_isolated_section_response(data)
    assert isinstance(resp, PodcastScriptResponse)
    assert resp.segments[0].order == 1
    assert resp.segments[1].order == 2
    assert resp.segments[0].heading == "Naval Negotiations"


# ---------------------------------------------------------------------------
# Test D: Proper local response [1] -> unchanged
# ---------------------------------------------------------------------------
def test_d_proper_local_response_1_unchanged():
    data = {
        "episode_title": "Pepsi in the USSR",
        "episode_description": "Overview",
        "segments": [
            {"order": 1, "heading": "Introduction", "narration": "Opening hook for the episode."},
        ],
        "warnings": [],
    }
    rebased = rebase_isolated_section_orders(data)
    assert [s["order"] for s in rebased["segments"]] == [1]

    resp = parse_isolated_section_response(data)
    assert resp.segments[0].order == 1
    assert resp.segments[0].narration == "Opening hook for the episode."


# ---------------------------------------------------------------------------
# Test E: Duplicate [3, 3] -> rejected
# ---------------------------------------------------------------------------
def test_e_duplicate_orders_rejected():
    data = {
        "episode_title": "Test Title",
        "episode_description": "Test Desc",
        "segments": [
            {"order": 3, "heading": "Part A", "narration": "Narration text A."},
            {"order": 3, "heading": "Part B", "narration": "Narration text B."},
        ],
        "warnings": [],
    }
    # Rebasing must NOT silently repair duplicate orders
    rebased = rebase_isolated_section_orders(data)
    assert [s["order"] for s in rebased["segments"]] == [3, 3]

    with pytest.raises(pydantic.ValidationError) as exc_info:
        parse_isolated_section_response(data)
    err_str = str(exc_info.value)
    assert "Duplicate segment order found" in err_str or "expected sequential order starting at 1" in err_str


# ---------------------------------------------------------------------------
# Test F: Gap [3, 5] -> rejected
# ---------------------------------------------------------------------------
def test_f_gap_orders_rejected():
    data = {
        "episode_title": "Test Title",
        "episode_description": "Test Desc",
        "segments": [
            {"order": 3, "heading": "Part A", "narration": "Narration text A."},
            {"order": 5, "heading": "Part B", "narration": "Narration text B."},
        ],
        "warnings": [],
    }
    rebased = rebase_isolated_section_orders(data)
    assert [s["order"] for s in rebased["segments"]] == [3, 5]

    with pytest.raises(pydantic.ValidationError) as exc_info:
        parse_isolated_section_response(data)
    assert "expected sequential order starting at 1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test G: Non-monotonic [4, 3] -> rejected
# ---------------------------------------------------------------------------
def test_g_non_monotonic_orders_rejected():
    data = {
        "episode_title": "Test Title",
        "episode_description": "Test Desc",
        "segments": [
            {"order": 4, "heading": "Part A", "narration": "Narration text A."},
            {"order": 3, "heading": "Part B", "narration": "Narration text B."},
        ],
        "warnings": [],
    }
    rebased = rebase_isolated_section_orders(data)
    assert [s["order"] for s in rebased["segments"]] == [4, 3]

    with pytest.raises(pydantic.ValidationError) as exc_info:
        parse_isolated_section_response(data)
    assert "expected sequential order starting at 1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test H: Normal full PodcastScriptResponse validation still rejects order starting at 3
# ---------------------------------------------------------------------------
def test_h_canonical_validation_still_rejects_order_starting_at_3():
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PodcastScriptResponse(
            episode_title="Full Episode",
            episode_description="Full episode description",
            segments=[
                PodcastSegment(order=3, heading="Third Section", narration="Full script narration starting at 3.")
            ],
            warnings=[],
        )
    assert "expected sequential order starting at 1, but got 3 at position 1" in str(exc_info.value)

    # Also via dict unpacking
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PodcastScriptResponse(
            **{
                "episode_title": "Full Episode",
                "episode_description": "Full episode description",
                "segments": [
                    {"order": 3, "heading": "Third Section", "narration": "Full script narration starting at 3."}
                ],
                "warnings": [],
            }
        )
    assert "expected sequential order starting at 1, but got 3 at position 1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Additional Safety Checks: zero / negative / bool values
# ---------------------------------------------------------------------------
def test_safety_zero_negative_bool_values_not_rebased():
    for invalid_order in [0, -1, True]:
        data = {
            "episode_title": "Test",
            "episode_description": "Desc",
            "segments": [{"order": invalid_order, "heading": "H", "narration": "N"}],
            "warnings": [],
        }
        rebased = rebase_isolated_section_orders(data)
        assert rebased["segments"][0]["order"] == invalid_order


# ---------------------------------------------------------------------------
# Test A: Global Section 3 returns one segment with order=3
# ---------------------------------------------------------------------------
def test_a_global_section_3_returns_order_3_accepted_and_preserves_section_index():
    """Verify Section 3 with order=3 is accepted locally, narration preserved, Herald section_index remains 3."""
    job = PodcastJob(id="job-sec-3", source_hash="hash-3", source_text="Pepsi trading warships with USSR")
    sec_info = {
        "section_index": 3,
        "heading": "The 1989 Submarine Fleet Accord",
        "purpose": "Explain the barter deal where Pepsi acquired 17 submarines.",
        "word_budget": 500,
        "word_budget_min": 425,
        "word_budget_max": 575,
        "key_points": ["17 submarines", "cruiser, frigate, destroyer"],
        "relevant_evidence_ids": ["ev_fleet"],
    }
    packet = {
        "topic": "Pepsi Warships",
        "items": [{"evidence_id": "ev_fleet", "title": "Submarines", "snippet": "17 submarines transferred."}],
    }

    raw_provider_dict = {
        "episode_title": "Pepsi Warships",
        "episode_description": "Section on naval deal",
        "segments": [
            {
                "order": 3,
                "heading": "The 1989 Submarine Fleet Accord",
                "narration": "In 1989, Pepsi briefly commanded the sixth largest navy in the world after accepting seventeen diesel-electric submarines.",
            }
        ],
        "warnings": [],
    }

    mock_provider = MagicMock()
    mock_provider.generate_script.return_value = raw_provider_dict

    def mock_failover(job, operation, execute_fn, **kwargs):
        return execute_fn(mock_provider, 1, kwargs.get("source_text", ""))

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        result = generate_single_section(
            job=job,
            section_info=sec_info,
            topic="Pepsi Warships",
            evidence_packet=packet,
            previous_summary=None,
            scope=EvidenceScope.RESEARCH,
        )

    assert result["completed"] is True
    # Herald global section index must remain 3
    assert result["section_index"] == 3
    assert result["heading"] == "The 1989 Submarine Fleet Accord"
    # Narration must be preserved intact
    assert "seventeen diesel-electric submarines" in result["narration"]
    assert result["word_count"] > 10


# ---------------------------------------------------------------------------
# Test B: Global Section 11 returns order=11
# ---------------------------------------------------------------------------
def test_b_global_section_11_returns_order_11_accepted_locally():
    """Verify Section 11 with order=11 is accepted locally and Herald section_index remains 11."""
    job = PodcastJob(id="job-sec-11", source_hash="hash-11", source_text="Long episode topic")
    sec_info = {
        "section_index": 11,
        "heading": "Legacy and Dissolution",
        "purpose": "Examine how the ships were scrapped in Sweden.",
        "word_budget": 400,
        "relevant_evidence_ids": [],
    }
    packet = {"topic": "Deep Dive", "items": []}

    raw_provider_dict = {
        "episode_title": "Deep Dive",
        "episode_description": "Section 11 overview",
        "segments": [
            {
                "order": 11,
                "heading": "Legacy and Dissolution",
                "narration": "Ultimately, the decommissioned vessels were sold to a Swedish scrap yard for recycling.",
            }
        ],
        "warnings": [],
    }

    mock_provider = MagicMock()
    mock_provider.generate_script.return_value = raw_provider_dict

    def mock_failover(job, operation, execute_fn, **kwargs):
        return execute_fn(mock_provider, 1, kwargs.get("source_text", ""))

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        result = generate_single_section(
            job=job,
            section_info=sec_info,
            topic="Deep Dive",
            evidence_packet=packet,
            previous_summary=None,
            scope=EvidenceScope.RESEARCH,
        )

    assert result["completed"] is True
    assert result["section_index"] == 11
    assert "sold to a Swedish scrap yard" in result["narration"]


# ---------------------------------------------------------------------------
# Test I: Retry topology: global-section/local-order mismatch must not trigger another provider call
# ---------------------------------------------------------------------------
def test_i_retry_topology_no_additional_provider_calls():
    """
    Verify that an isolated section returning order=3 for Section 3 succeeds on attempt 1
    without triggering retry, failover, or extra provider calls.
    """
    call_count = 0

    from herald.gemini.client import generate_podcast_script

    raw_gemini_json = json.dumps({
        "episode_title": "Pepsi Warships",
        "episode_description": "Section 3",
        "segments": [
            {
                "order": 3,
                "heading": "Naval Exchange",
                "narration": "The barter deal was struck with the Soviet government.",
            }
        ],
        "warnings": [],
    })

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {"parts": [{"text": raw_gemini_json}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50, "totalTokenCount": 150},
    }

    def counting_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch("httpx.Client.post", side_effect=counting_post), \
         patch("herald.config.settings.GEMINI_API_KEY", "test-api-key"), \
         patch("herald.gemini.client._record_gemini_interaction"):
        resp = generate_podcast_script(
            source_text="Test source",
            is_isolated_section=True,
            job_id="test-job-retry-topo",
        )

    # Must succeed on attempt 1 with exactly ONE provider call
    assert call_count == 1
    assert isinstance(resp, PodcastScriptResponse)
    assert resp.segments[0].order == 1
    assert resp.segments[0].narration == "The barter deal was struck with the Soviet government."


# ---------------------------------------------------------------------------
# Test J: Resume/diagnostics continue reporting the correct GLOBAL section index
# ---------------------------------------------------------------------------
def test_j_resume_and_diagnostics_report_correct_global_section_index():
    """Verify that completed sections persist the true global section_index (1, 2, 3...)."""
    job = PodcastJob(
        id="job-resume-topo",
        source_hash="hash-j",
        source_text="Pepsi warships topic",
        outline_json={
            "sections": [
                {"section_index": 1, "heading": "S1", "purpose": "P1", "word_budget": 300},
                {"section_index": 2, "heading": "S2", "purpose": "P2", "word_budget": 300},
                {"section_index": 3, "heading": "S3", "purpose": "P3", "word_budget": 300},
            ]
        },
        section_progress_json=[
            {"section_index": 1, "heading": "S1", "narration": "Section 1 narration.", "word_count": 50, "completed": True},
            {"section_index": 2, "heading": "S2", "narration": "Section 2 narration.", "word_count": 50, "completed": True},
        ],
    )

    # When resuming, section 1 and 2 are skipped, section 3 is generated
    sec_info = job.outline_json["sections"][2]
    assert sec_info["section_index"] == 3

    mock_provider = MagicMock()
    mock_provider.generate_script.return_value = {
        "episode_title": "Pepsi",
        "episode_description": "Section 3",
        "segments": [{"order": 3, "heading": "S3", "narration": "Section 3 narration."}],
        "warnings": [],
    }

    def mock_failover(job, operation, execute_fn, **kwargs):
        return execute_fn(mock_provider, 1, kwargs.get("source_text", ""))

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        result = generate_single_section(
            job=job,
            section_info=sec_info,
            topic="Pepsi",
            evidence_packet={"topic": "Pepsi", "items": []},
            previous_summary="Section 2 covered previous facts",
            scope=EvidenceScope.RESEARCH,
        )

    # Append to completed sections as pipeline does
    completed = list(job.section_progress_json)
    completed.append(result)

    # All section indices must be sequentially 1, 2, 3
    indices = [s["section_index"] for s in completed]
    assert indices == [1, 2, 3]


# ---------------------------------------------------------------------------
# Test: Trusted generation instructions include explicit isolated section contract
# ---------------------------------------------------------------------------
def test_trusted_generation_instructions_include_isolated_contract():
    job = PodcastJob(id="job-inst", source_hash="hash-inst", source_text="Source text")
    sec_info = {
        "section_index": 3,
        "heading": "Section Three",
        "purpose": "Explain third part",
        "word_budget": 400,
        "relevant_evidence_ids": [],
    }
    captured_instructions = []

    def mock_failover(job, operation, execute_fn, **kwargs):
        prov = MagicMock()
        def mock_gen(*args, **kw):
            captured_instructions.append(kw.get("generation_instructions", ""))
            return PodcastScriptResponse(
                episode_title="T",
                episode_description="D",
                segments=[PodcastSegment(order=1, heading="H", narration="N")],
                warnings=[],
            )
        prov.generate_script = mock_gen
        return execute_fn(prov, 1, kwargs.get("source_text", ""))

    with patch("herald.ai.long_form.execute_with_failover", side_effect=mock_failover):
        generate_single_section(
            job=job,
            section_info=sec_info,
            topic="Test Topic",
            evidence_packet={"topic": "Test Topic", "items": []},
            previous_summary=None,
        )

    assert len(captured_instructions) == 1
    inst = captured_instructions[0]
    # Verify trusted generation instructions explicitly inform the model
    assert "standalone response" in inst.lower()
    assert "response-local" in inst.lower()
    assert "order=1" in inst or "order 1" in inst
    assert is_isolated_section_instruction(inst) is True


# ---------------------------------------------------------------------------
# Test K: Multi-provider neutrality
# ---------------------------------------------------------------------------
def test_provider_neutrality_openai_isolated_rebase():
    prov = OpenAIProvider(api_key="sk-test", model="gpt-4o")
    raw_content = json.dumps({
        "episode_title": "OpenAI Section",
        "episode_description": "Desc",
        "segments": [{"order": 7, "heading": "Sec 7", "narration": "OpenAI generated section 7."}],
        "warnings": [],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": raw_content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.ai.openai_provider.record_ai_interaction"):
        resp = prov.generate_script(
            source_text="Test source",
            is_isolated_section=True,
            job_id="test-job-openai",
        )
    assert resp.segments[0].order == 1
    assert resp.segments[0].narration == "OpenAI generated section 7."


def test_provider_neutrality_anthropic_isolated_rebase():
    prov = AnthropicProvider(api_key="sk-ant-test", model="claude-3-5-sonnet-20241022")
    raw_content = json.dumps({
        "episode_title": "Anthropic Section",
        "episode_description": "Desc",
        "segments": [{"order": 5, "heading": "Sec 5", "narration": "Claude generated section 5."}],
        "warnings": [],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": raw_content}],
        "usage": {"input_tokens": 50, "output_tokens": 30},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.ai.anthropic_provider.record_ai_interaction"):
        resp = prov.generate_script(
            source_text="Test source",
            is_isolated_section=True,
            job_id="test-job-anthropic",
        )
    assert resp.segments[0].order == 1
    assert resp.segments[0].narration == "Claude generated section 5."


def test_provider_neutrality_cloudflare_isolated_rebase():
    prov = CloudflareProvider(api_token="cf-test-tok", account_id="cf-acc-id")
    raw_content = json.dumps({
        "episode_title": "Cloudflare Section",
        "episode_description": "Desc",
        "segments": [{"order": 4, "heading": "Sec 4", "narration": "Workers AI generated section 4."}],
        "warnings": [],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "success": True,
        "result": {"response": raw_content},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.ai.cloudflare_provider.record_ai_interaction"):
        resp = prov.generate_script(
            source_text="Test source",
            is_isolated_section=True,
            job_id="test-job-cf",
        )
    assert resp.segments[0].order == 1
    assert resp.segments[0].narration == "Workers AI generated section 4."


def test_provider_neutrality_ollama_isolated_rebase():
    prov = OllamaProvider(base_url="http://localhost:11434", model="llama3")
    raw_content = json.dumps({
        "episode_title": "Ollama Section",
        "episode_description": "Desc",
        "segments": [{"order": 9, "heading": "Sec 9", "narration": "Ollama generated section 9."}],
        "warnings": [],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"content": raw_content},
        "prompt_eval_count": 20,
        "eval_count": 30,
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.ai.ollama_provider.record_ai_interaction"):
        resp = prov.generate_script(
            source_text="Test source",
            is_isolated_section=True,
            job_id="test-job-ollama",
        )
    assert resp.segments[0].order == 1
    assert resp.segments[0].narration == "Ollama generated section 9."


def test_provider_neutrality_gemini_isolated_rebase():
    prov = GeminiProvider(model="gemini-2.5-flash")
    raw_content = json.dumps({
        "episode_title": "Gemini Section",
        "episode_description": "Desc",
        "segments": [{"order": 6, "heading": "Sec 6", "narration": "Gemini generated section 6."}],
        "warnings": [],
    })
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {"parts": [{"text": raw_content}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 30, "totalTokenCount": 50},
    }

    with patch("httpx.Client.post", return_value=mock_resp), \
         patch("herald.config.settings.GEMINI_API_KEY", "test-api-key"), \
         patch("herald.gemini.client._record_gemini_interaction"):
        resp = prov.generate_script(
            source_text="Test source",
            is_isolated_section=True,
            job_id="test-job-gemini",
        )
    assert resp.segments[0].order == 1
    assert resp.segments[0].narration == "Gemini generated section 6."

