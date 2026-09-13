"""Register c408's network-free monitoring convergence regressions in backend CI."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts/tests/test_monitoring_convergence.py"
_spec = importlib.util.spec_from_file_location("c408_monitoring_convergence", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestMonitoringConvergence = _module.MonitoringConvergenceTests
