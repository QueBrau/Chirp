"""Run the artifact string-table regressions in the normal backend CI job."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts/tests/test_c418_compiled_endpoints.py"
_spec = importlib.util.spec_from_file_location("c418_artifact_inventory", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestCompiledEndpointInventory = _module.CompiledEndpointTests
