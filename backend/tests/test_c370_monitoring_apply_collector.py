"""Register the standard-library monitoring_apply fixture suite in backend CI."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts/tests/test_monitoring_apply.py"
_spec = importlib.util.spec_from_file_location("c370_apply_contracts", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestMonitoringApply = _module.MonitoringApplyTests
