"""Print one synthetic analytics event locally; performs no network or DB work."""
from __future__ import annotations

import argparse
from uuid import UUID

from app.core.analytics import emit
from app.core.logging_config import configure_app_logging


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-id", required=True, type=UUID)
    args = parser.parse_args()
    configure_app_logging()
    emit("pipeline_probe", probe_id=args.probe_id)


if __name__ == "__main__":
    main()
