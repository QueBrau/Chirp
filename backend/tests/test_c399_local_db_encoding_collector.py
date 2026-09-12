"""Register the standard-library local_db_encoding fixture suite in backend CI.

scripts/tests is not on CI's collection path (the backend job runs pytest with
working-directory: backend), so without this shim the guard tests that keep
--apply away from the Cloud SQL proxy would only ever run by hand. Same eight-line
importlib pattern as test_c370_monitoring_apply_collector.py.
"""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts/tests/test_local_db_encoding.py"
_spec = importlib.util.spec_from_file_location("c399_encoding_contracts", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TestDetection = _module.DetectionTests
TestGuard = _module.GuardTests
TestLiveConnection = _module.LiveConnectionTests
TestDryRun = _module.DryRunTests
TestRenderedPlan = _module.RenderedPlanTests
TestOffenderReport = _module.OffenderReportTests
TestOffenderProbe = _module.OffenderProbeTests
TestWrapper = _module.WrapperTests
