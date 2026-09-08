"""AST-only local dependency query inventory: no backend imports or application I/O."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXTRACTOR = ROOT / "app-mobile/scripts/backend-contracts.py"


def extract(tmp_path, router):
    script = tmp_path / "app-mobile/scripts/backend-contracts.py"
    script.parent.mkdir(parents=True)
    script.write_text(EXTRACTOR.read_text())
    (tmp_path / "backend/app/schemas").mkdir(parents=True)
    routers = tmp_path / "backend/app/routers"
    routers.mkdir()
    (routers / "example.py").write_text(router)
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=5)


def queries(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["routes"][0]["query"]


def test_dependency_query_uses_wire_alias_and_keeps_direct_query(tmp_path):
    result = extract(tmp_path, '''
from unavailable_module import get_current_user, get_session
async def read_ids(request: Request, ids: str | None = Query(None, alias="message_ids")):
    raise RuntimeError("must never execute")
@router.get("/conversations/{conversation_id}/messages/by-id")
async def lookup(conversation_id: str, resolved=Depends(read_ids), limit: int=Query(50),
                 user=Depends(get_current_user), session=Depends(get_session)):
    raise RuntimeError("must never execute")
''')
    assert queries(result) == ["message_ids", "limit"]


def test_current_lookup_inventory_contains_only_ids():
    result = subprocess.run([sys.executable, str(EXTRACTOR)], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    route = next(route for route in json.loads(result.stdout)["routes"]
                 if route["path"] == "/conversations/{conversation_id}/messages/by-id")
    assert route["query"] == ["ids"]
    assert route["returns"] == "list[MessageOut]"
    assert "wrong_ids" not in route["query"]


def test_nested_cycle_and_duplicate_dependency_paths_are_bounded(tmp_path):
    result = extract(tmp_path, '''
def first(q: str=Query("", alias="shared"), other=Depends(second)):
    pass
async def second(q: str=Query("", alias="shared"), size: int=Query(1), other=Depends(first)):
    pass
@router.get("/items")
async def route(a=Depends(first), b=Depends(second), c=Depends(first)):
    pass
''')
    assert queries(result) == ["shared", "size"]


def test_decorator_and_annotated_local_dependencies_are_inventory_only(tmp_path):
    result = extract(tmp_path, '''
def nested(ids: Annotated[str, Query(alias="ids")], authorization=Header()):
    pass
def entry(value: Annotated[str, Depends(nested)]):
    pass
@router.get("/items/{item_id}", dependencies=[Depends(dependency=entry), Depends(factory())])
def route(item_id: str, token: Annotated[str, Header()], direct: Annotated[str, Query(alias="q")]):
    pass
''')
    assert set(queries(result)) == {"q", "ids"}


def test_only_explicit_query_from_reachable_local_dependencies(tmp_path):
    result = extract(tmp_path, '''
def unrelated(secret=Query("")):
    pass
def helper(implicit: str="", request: Request=None, body=Body(), header=Header(), cookie=Cookie()):
    pass
@router.get("/items", dependencies=[Depends(imported), Depends(factory())])
def route(a=Depends(helper), b=Depends(module.helper)):
    pass
''')
    assert queries(result) == []


@pytest.mark.parametrize("alias", ["dynamic_name", "42"])
def test_unresolved_alias_fails_closed(tmp_path, alias):
    result = extract(tmp_path, f'''
def helper(q: str=Query("", alias={alias})):
    pass
@router.get("/items")
def route(a=Depends(helper)):
    pass
''')
    assert result.returncode != 0
    assert "Unresolved query alias" in result.stderr


def test_ambiguous_local_function_name_refused(tmp_path):
    result = extract(tmp_path, '''
def helper(first=Query("")):
    pass
def helper(second=Query("")):
    pass
@router.get("/items")
def route(a=Depends(helper)):
    pass
''')
    assert result.returncode != 0
    assert "Ambiguous dependency function" in result.stderr
