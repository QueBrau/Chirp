"""Register the standard-library collector contract suite in backend CI."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts/tests/test_monitoring_check.py"
_spec = importlib.util.spec_from_file_location("c370_collector_contracts", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestMonitoringCollector = _module.MonitoringCheckTests
