#!/usr/bin/env python3
"""Print the schema-only dependency subset from the canonical backend hash lock.

No dependency versions are independently resolved or copied into this spike.
Pip's --require-hashes also fails if a future Pydantic adds an unlisted dependency.
"""

from pathlib import Path
import re

REQUIRED = {"annotated-types", "pydantic", "pydantic-core", "typing-extensions", "typing-inspection"}
source = (Path(__file__).resolve().parents[2] / "backend" / "requirements.lock").read_text()
entries = re.findall(r"(?ms)^([A-Za-z0-9_-]+)==(.*?)(?=^[A-Za-z0-9_-]+==|\Z)", source)
selected = {}
for name, body in entries:
    if name in REQUIRED:
        if name in selected or "--hash=sha256:" not in body:
            raise SystemExit("schema_lock_entry_invalid")
        selected[name] = name + "==" + body
if selected.keys() != REQUIRED:
    raise SystemExit("schema_lock_dependency_missing")
print("\n".join(selected[name].rstrip() for name in sorted(selected)))
