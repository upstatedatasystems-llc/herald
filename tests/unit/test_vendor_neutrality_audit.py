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


def test_api_main_vendor_neutrality():
    """Verify apps/api/main.py contains zero direct vendor provider imports."""
    api_path = Path("apps/api/main.py")
    assert api_path.exists(), "apps/api/main.py must exist"

    imported_modules = get_imports_from_file(api_path)

    for forbidden in FORBIDDEN_VENDOR_MODULES:
        violating = [m for m in imported_modules if m == forbidden or m.startswith(f"{forbidden}.")]
        assert not violating, f"API main imports forbidden vendor module: {violating}"


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
