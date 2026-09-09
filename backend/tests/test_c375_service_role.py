"""Board c375: explicit API/WS deployment boundary via Settings.service_role.

Covers: (a) an out-of-enum service_role fails Settings() construction; (b) a
validly-constructed Settings whose service_role is mutated post-construction to
an unhandled value makes create_app() itself refuse to serve, rather than
falling through to some default mounting; (c) the positive/negative route
matrix across role in ("all", "api", "ws") for /_health, an auth-gated HTTP
route, and the /ws websocket; (d) role="all"'s route set equals the union of
role="api" and role="ws", with the overlap being exactly {/_health, /_deployment}
and nothing else; (e) a router-file guard: every module under app.routers that
exposes a `router` attribute must appear in main.ALL_HTTP_ROUTERS, so a router
file added later cannot be silently dropped from the api role.

Route introspection uses app.openapi()["paths"], NOT app.routes: the installed
FastAPI/Starlette pair here (0.141.1 / 1.6.0) mounts app.include_router() via a
lazy `_IncludedRouter` wrapper whose entries in app.routes carry no usable
`.path`/`.methods` -- confirmed empirically while writing this file, matching
the risk the planner flagged in advance. app.openapi() is public API, walks the
same effective route table FastAPI itself dispatches against, and, as a
bonus, already excludes the /docs, /redoc, /openapi.json meta-routes (they are
plain Starlette routes with no OpenAPI operation), so the overlap assertion
below needs no separate filtering for them. The /ws websocket has no OpenAPI
representation, so its presence is read from actual ASGI behavior instead: a
mounted /ws closes with code 4401 (gateway.router's own "no credentials"
close), while an absent /ws never reaches application code at all and closes
with Starlette's generic no-route code 1000 -- confirmed empirically per role
below.
"""
import importlib
import os
import pkgutil
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.routers as routers_pkg
from app.config import Settings, get_settings

ROLES = ("all", "api", "ws")
_WS_ABSENT_ROUTE_CODE = 1000
_WS_NO_CREDENTIALS_CODE = 4401


def _clear_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def role_app(request: Any, migrated_db: str) -> Any:
    """Build create_app() under SERVICE_ROLE=<role>, restoring env/cache after.

    Yields (role, app): tests use `role` (the label this fixture itself set)
    to know what to expect, and then check ACTUAL ASGI behavior against it --
    the role is not re-derived from the app, since request.param already IS
    the ground truth about what was configured.

    Restoration order matters: env is put back to its prior state FIRST, then
    the settings cache is cleared, so a later test that never touches
    SERVICE_ROLE at all still gets a Settings() built from the real ambient
    env, not a stale cached object left over from this test's role.
    """
    role = request.param
    prior = os.environ.get("SERVICE_ROLE")
    os.environ["SERVICE_ROLE"] = role
    _clear_settings_cache()
    import app.db as app_db
    from app import models  # noqa: F401  # populate Base.metadata
    from app.main import create_app

    app_db._engine = None
    app_db._session_factory = None
    app = create_app()
    try:
        yield role, app
    finally:
        if prior is None:
            os.environ.pop("SERVICE_ROLE", None)
        else:
            os.environ["SERVICE_ROLE"] = prior
        _clear_settings_cache()


def _http_route_shapes(app: Any) -> set[tuple[tuple[str, ...], str]]:
    """(sorted method tuple, path) for every OpenAPI-documented HTTP route."""
    paths: dict[str, dict[str, Any]] = app.openapi().get("paths", {})
    return {
        (tuple(sorted(method.upper() for method in methods)), path)
        for path, methods in paths.items()
    }


def _ws_close_code(app: Any) -> int:
    """Attempt the /ws handshake with no credentials; return the close code.

    4401 means gateway.router is mounted and ran its own auth logic; 1000 means
    Starlette never matched a websocket route at all.
    """
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws"):
                pass
        return exc_info.value.code


# ---------------------------------------------------------------------------
# (a) Settings() itself rejects an out-of-enum role
# ---------------------------------------------------------------------------


def test_invalid_service_role_rejected_at_settings() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError) as exc_info:
        Settings(_env_file=None, service_role="worker")
    assert "service_role" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (b) create_app() itself refuses an unhandled role reaching it post-construction
# ---------------------------------------------------------------------------


def test_create_app_refuses_to_serve_under_unhandled_role(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    settings.service_role = "worker"  # bypasses the Literal check: no validate_assignment
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    from app.main import create_app

    with pytest.raises(RuntimeError) as exc_info:
        create_app()
    assert "worker" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (c) positive/negative route matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_app", ROLES, indirect=True)
async def test_health_answers_under_every_role(role_app: tuple[str, Any]) -> None:
    _role, app = role_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/_health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("role_app", ROLES, indirect=True)
async def test_auth_gated_route_401_under_all_and_api_404_under_ws(role_app: tuple[str, Any]) -> None:
    role, app = role_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/conversations")
    if role in ("all", "api"):
        assert resp.status_code == 401
    else:
        assert resp.status_code == 404


@pytest.mark.parametrize("role_app", ROLES, indirect=True)
def test_ws_handshake_reaches_auth_close_under_all_and_ws_route_absent_under_api(
    role_app: tuple[str, Any],
) -> None:
    role, app = role_app
    code = _ws_close_code(app)
    if role in ("all", "ws"):
        assert code == _WS_NO_CREDENTIALS_CODE
    else:
        assert code == _WS_ABSENT_ROUTE_CODE


# ---------------------------------------------------------------------------
# (d) role="all" route set equals the union of "api" and "ws", overlap exactly
#     {/_health, /_deployment}
# ---------------------------------------------------------------------------


def test_all_role_route_set_equals_api_union_ws_with_shared_overlap_only(
    migrated_db: str,
) -> None:
    import app.db as app_db
    from app import models  # noqa: F401
    from app.main import create_app

    prior = os.environ.get("SERVICE_ROLE")
    shapes: dict[str, set[tuple[tuple[str, ...], str]]] = {}
    ws_codes: dict[str, int] = {}
    try:
        for role in ROLES:
            os.environ["SERVICE_ROLE"] = role
            _clear_settings_cache()
            app_db._engine = None
            app_db._session_factory = None
            built = create_app()
            shapes[role] = _http_route_shapes(built)
            ws_codes[role] = _ws_close_code(built)
    finally:
        if prior is None:
            os.environ.pop("SERVICE_ROLE", None)
        else:
            os.environ["SERVICE_ROLE"] = prior
        _clear_settings_cache()

    # Construct the ws (method-tuple, path) entries only for roles whose actual
    # handshake behavior proves the route is mounted (code 4401), not from a
    # role-name assumption.
    ws_shape = (("WEBSOCKET",), "/ws")
    all_combined = shapes["all"] | ({ws_shape} if ws_codes["all"] == _WS_NO_CREDENTIALS_CODE else set())
    api_combined = shapes["api"] | ({ws_shape} if ws_codes["api"] == _WS_NO_CREDENTIALS_CODE else set())
    ws_combined = shapes["ws"] | ({ws_shape} if ws_codes["ws"] == _WS_NO_CREDENTIALS_CODE else set())

    # Discriminating conditions this test constructs and must see hold before
    # trusting the equality/overlap assertions below: "all" mounts /ws, "api"
    # does not, "ws" does.
    assert ws_codes["all"] == _WS_NO_CREDENTIALS_CODE
    assert ws_codes["api"] == _WS_ABSENT_ROUTE_CODE
    assert ws_codes["ws"] == _WS_NO_CREDENTIALS_CODE

    assert all_combined == api_combined | ws_combined
    health_shape = (("GET",), "/_health")
    deployment_shape = (("GET",), "/_deployment")
    assert (api_combined & ws_combined) == {health_shape, deployment_shape}


# ---------------------------------------------------------------------------
# (e) manager-added guard: every app.routers module exposing `router` is
#     enumerated in ALL_HTTP_ROUTERS
# ---------------------------------------------------------------------------


def test_all_router_modules_are_enumerated_in_all_http_routers() -> None:
    from app.main import ALL_HTTP_ROUTERS

    discovered = set()
    for _finder, name, _is_pkg in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"app.routers.{name}")
        if hasattr(module, "router"):
            discovered.add(name)

    enumerated = {module.__name__.rsplit(".", 1)[-1] for module in ALL_HTTP_ROUTERS}
    assert discovered == enumerated
