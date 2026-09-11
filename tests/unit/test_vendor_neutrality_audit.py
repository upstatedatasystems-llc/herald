"""
Architectural Audit Test: Vendor Neutrality Invariant.
Verifies that core business orchestration (herald/core/pipeline.py, apps/api/main.py)
has ZERO direct imports of vendor-specific AI providers or vendor SDKs,
and instead interfaces strictly through vendor-neutral AI abstractions.
"""

import ast
from pathlib import Path

FORBIDDEN_VENDOR_MODULES = {
    "google.genai",
    "google.generativeai",
    "openai",
    "groq",
    "anthropic",
    "mistralai",
    "herald.ai.gemini_provider",
    "herald.ai.groq_provider",
    "herald.ai.cloudflare_provider",
    "herald.ai.openai_provider",
    "herald.ai.openrouter_provider",
    "herald.ai.mistral_provider",
    "herald.ai.anthropic_provider",
    "herald.ai.ollama_provider",
}


def get_imports_from_file(file_path: Path) -> set[str]:
    """Parse a python file into AST and return all imported module paths."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    imports = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
                for alias in node.names:
                    imports.add(f"{node.module}.{alias.name}")

    return imports


def test_core_pipeline_vendor_neutrality():
    """Verify herald/core/pipeline.py contains zero direct vendor provider imports."""
    pipeline_path = Path("herald/core/pipeline.py")
    assert pipeline_path.exists(), "herald/core/pipeline.py must exist"

    imported_modules = get_imports_from_file(pipeline_path)

    for forbidden in FORBIDDEN_VENDOR_MODULES:
        violating = [m for m in imported_modules if m == forbidden or m.startswith(f"{forbidden}.")]
        assert not violating, f"Core pipeline imports forbidden vendor module: {violating}"


def test_telegram_bot_main_vendor_neutrality():
    """Verify apps/telegram_bot/main.py contains zero direct vendor provider imports."""
    bot_path = Path("apps/telegram_bot/main.py")
    assert bot_path.exists(), "apps/telegram_bot/main.py must exist"

    imported_modules = get_imports_from_file(bot_path)

    for forbidden in FORBIDDEN_VENDOR_MODULES:
        violating = [m for m in imported_modules if m == forbidden or m.startswith(f"{forbidden}.")]
        assert not violating, f"Telegram Bot main imports forbidden vendor module: {violating}"


def test_worker_main_vendor_neutrality():
    """Verify apps/worker/main.py contains zero direct vendor provider imports."""
    worker_path = Path("apps/worker/main.py")
    assert worker_path.exists(), "apps/worker/main.py must exist"

    imported_modules = get_imports_from_file(worker_path)

    for forbidden in FORBIDDEN_VENDOR_MODULES:
        violating = [m for m in imported_modules if m == forbidden or m.startswith(f"{forbidden}.")]
        assert not violating, f"Worker main imports forbidden vendor module: {violating}"


def test_failover_vendor_neutrality():
    """Verify herald/ai/failover.py contains zero direct vendor provider imports."""
    fo_path = Path("herald/ai/failover.py")
    assert fo_path.exists(), "herald/ai/failover.py must exist"

    imported_modules = get_imports_from_file(fo_path)

    for forbidden in FORBIDDEN_VENDOR_MODULES:
        violating = [m for m in imported_modules if m == forbidden or m.startswith(f"{forbidden}.")]
        assert not violating, f"Failover module imports forbidden vendor module: {violating}"


def test_resolution_vendor_neutrality():
    """Verify herald/ai/resolution.py contains zero direct vendor provider imports."""
    res_path = Path("herald/ai/resolution.py")
    assert res_path.exists(), "herald/ai/resolution.py must exist"

    imported_modules = get_imports_from_file(res_path)

    for forbidden in FORBIDDEN_VENDOR_MODULES:
        violating = [m for m in imported_modules if m == forbidden or m.startswith(f"{forbidden}.")]
        assert not violating, f"Resolution module imports forbidden vendor module: {violating}"


def test_non_gemini_providers_clean_of_gemini_settings():
    """Verify OpenAI, Groq, Cloudflare, Anthropic, Ollama do not read generic GEMINI_* settings."""
    import inspect

    from herald.ai import (
        anthropic_provider,
        cloudflare_provider,
        groq_provider,
        ollama_provider,
        openai_provider,
    )

    modules = [
        anthropic_provider,
        cloudflare_provider,
        groq_provider,
        ollama_provider,
        openai_provider,
    ]
    for mod in modules:
        src = inspect.getsource(mod)
        assert "GEMINI_RETRY_COUNT" not in src, f"{mod.__name__} reads GEMINI_RETRY_COUNT"
        assert "GEMINI_TEMPERATURE" not in src, f"{mod.__name__} reads GEMINI_TEMPERATURE"
        assert "GEMINI_MAX_OUTPUT_TOKENS" not in src, f"{mod.__name__} reads GEMINI_MAX_OUTPUT_TOKENS"


def test_startup_validation_uses_ai_provider():
    """Verify startup validation in worker and telegram_bot uses settings.AI_PROVIDER."""
    import inspect

    from apps.telegram_bot import main as bot_main
    from apps.worker import main as worker_main

    worker_src = inspect.getsource(worker_main.run_worker_loop)
    assert "settings.AI_PROVIDER" in worker_src
    assert "settings.AI_PRIMARY_PROVIDER" not in worker_src

    bot_src = inspect.getsource(bot_main.main)
    assert "validate_server_default_chain" in bot_src
    assert "settings.AI_PROVIDER" in bot_src


def test_research_through_research_grounding():
    """Verify pipeline.py requires capability 'research_grounding', not 'google_search_grounding'."""
    import inspect

    from herald.core import pipeline

    pipeline_src = inspect.getsource(pipeline)
    assert 'required_capability="research_grounding"' in pipeline_src
    assert "google_search_grounding" not in pipeline_src


def test_no_direct_gemini_timeout_uses():
    """Verify gemini/client.py uses effective_ai_timeout_seconds and has no direct GEMINI_TIMEOUT_SECONDS in httpx clients."""
    with open("herald/gemini/client.py", "r", encoding="utf-8") as f:
        content = f.read()

    assert "httpx.Client(timeout=settings.GEMINI_TIMEOUT_SECONDS)" not in content
    assert "timeout=settings.effective_ai_timeout_seconds" in content


def test_no_runtime_gemini_model_writes_in_worker():
    """Verify worker/main.py does not write job.gemini_model for completed jobs."""
    with open("apps/worker/main.py", "r", encoding="utf-8") as f:
        content = f.read()

    assert "job.gemini_model = settings.GEMINI_MODEL" not in content


def test_research_snapshot_survives_env_changes():
    """Verify get_job_ai_identity uses snapshotted research model without rereading settings."""
    from unittest.mock import patch

    from herald.db.models import PodcastJob
    from herald.telegram.formatters import get_job_ai_identity

    job = PodcastJob(
        id="job-res-1",
        request_mode="research",
        research_model="gemini-custom-research-v1",
        generation_settings_json={"research_provider": "gemini"},
    )

    with patch("herald.config.settings.GEMINI_RESEARCH_MODEL", "gemini-other-env"):
        prov_name, model_name = get_job_ai_identity(job)

    assert prov_name == "Gemini"
    assert model_name == "gemini-custom-research-v1"


def test_ai_check_stays_inside_user_chain():
    """Verify perform_ai_check inspects capabilities for candidates and has no separate RESEARCH_PROVIDER check."""
    from unittest.mock import MagicMock, patch

    from herald.telegram.bot import perform_ai_check

    mock_client = MagicMock()
    db = MagicMock()

    with patch("herald.telegram.bot.get_effective_user_preferences", return_value={}), \
         patch("herald.telegram.bot.get_ai_provider") as mock_get_p:
        mock_prov = MagicMock()
        mock_prov.check_connection.return_value = {"connected": True}
        mock_get_p.return_value = mock_prov
        perform_ai_check(db, mock_client, chat_id=123, user_id=456)

    assert mock_client.send_message.call_count >= 2
    final_text = mock_client.send_message.call_args_list[-1][1]["text"]
    assert "Your Failover Chain:" in final_text
    assert "Research Grounding" in final_text
    assert "Capabilities:" in final_text


def test_unknown_discovered_models_have_unknown_limits():
    """Verify live discovery sets context_window=None, max_output=None for unknown models."""
    from unittest.mock import MagicMock, patch

    from herald.ai.openai_provider import OpenAIProvider

    prov = OpenAIProvider(api_key="sk-test")
    fake_models_resp = {
        "data": [
            {"id": "unknown-brand-new-model-2026"},
        ]
    }
    with patch("httpx.Client.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_models_resp
        mock_get.return_value = mock_resp

        discovered = prov.discover_models()

    assert len(discovered) == 1
    m = discovered[0]
    assert m.model_id == "unknown-brand-new-model-2026"
    assert m.context_window is None
    assert m.max_output is None


def test_config_no_duplicate_adaptation_block():
    """Verify Settings has only one ADAPTATION_* block containing ADAPTATION_CHUNK_MAX_CHARS."""
    with open("herald/config.py", "r", encoding="utf-8") as f:
        lines = f.readlines()

    adaptation_chunk_lines = [line for line in lines if "ADAPTATION_CHUNK_MAX_CHARS" in line]
    assert len(adaptation_chunk_lines) == 1

    adaptation_max_chunks_lines = [line for line in lines if "ADAPTATION_MAX_CHUNKS" in line]
    assert len(adaptation_max_chunks_lines) == 1

