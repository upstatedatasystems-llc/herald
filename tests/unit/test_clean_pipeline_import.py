import subprocess
import sys
from collections.abc import Callable
from typing import get_type_hints

import herald.core.pipeline


def test_pipeline_clean_subprocess_import():
    """
    Verify that herald.core.pipeline can be imported in a fresh Python process
    without NameError (e.g. missing Callable import), and that function type
    annotations on execute_script_generation resolve cleanly.
    """
    code = (
        "import herald.core.pipeline\n"
        "from typing import get_type_hints\n"
        "hints = get_type_hints(herald.core.pipeline.execute_script_generation)\n"
        "assert 'status_notifier' in hints\n"
        "print('IMPORT_AND_HINTS_OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "IMPORT_AND_HINTS_OK" in result.stdout


def test_execute_script_generation_type_hints():
    """
    Verify get_type_hints evaluates properly and status_notifier has Callable type.
    """
    hints = get_type_hints(herald.core.pipeline.execute_script_generation)
    assert "status_notifier" in hints
    notifier_type = hints["status_notifier"]
    assert notifier_type == Callable[[str], None] | None
