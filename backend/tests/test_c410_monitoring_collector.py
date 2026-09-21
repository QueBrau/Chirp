"""Include c410's network-free policy contracts in the standard backend CI."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts/tests/test_c410_monitoring.py"
_spec = importlib.util.spec_from_file_location("c410_monitoring_contracts", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestC410Monitoring = _module.C410MonitoringTests
