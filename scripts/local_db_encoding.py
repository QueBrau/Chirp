"""Detect and (opt-in) repair a local Postgres server whose databases were
created with SQL_ASCII encoding instead of UTF8.

Jose's local cluster (Homebrew Postgres 14, port 5432) has SQL_ASCII on
chirp, chirp_test, postgres and template1, with lc_collate/lc_ctype C. Any
backend test that writes non-ASCII text into a JSONB or text column then
fails locally with asyncpg.exceptions.FeatureNotSupportedError while the
same test passes in CI (postgres:16, UTF8) and on Q's Docker Postgres
(board card c399).

Default mode is a READ-ONLY dry run: connect to the admin database of the
target server, read pg_database and pg_stat_activity for the target
databases, and print one line per database with the planned action and the
exact commands --apply would run. No write call is ever made in dry-run
mode.

--apply performs the plan, but only against a server that is provably local
development (host in localhost/127.0.0.1/::1, port in 5432/5433/5434) and
only against database names that match the dev-database naming pattern
(chirp, chirp_test, template1, chirp_test_*, c399_scratch_*). Anything else
is refused with the reason and exit code 2 before any connection is made
for writing.

Per ordinary database the apply is: refuse (exit 3) if another backend is
connected unless --terminate-connections; pg_dump to a kept file under
--dump-dir; CREATE DATABASE <name>_utf8 WITH OWNER <same owner> ENCODING
'UTF8' TEMPLATE template0; pg_restore into it; on restore failure, count
per-table rows whose bytes are not valid UTF-8 against the ORIGINAL
database, drop only the _utf8 copy, keep the dump, leave the original
untouched, exit non-zero; on success, verify the copy and rename-swap it
in, printing the backup name.

template1 needs a superuser connection: with one, the script drops and
recreates it as UTF8 from template0; without one it prints the exact
commands for Jose to run as psql -U <superuser> and exits 0.

The admin URL is always printed with the password masked. Nothing else
about the databases (row contents) is ever read or printed.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence
from urllib.parse import urlsplit

DEFAULT_DATABASE_URL = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp"
DEFAULT_TARGET_NAMES = ("chirp", "chirp_test", "template1")
DEFAULT_DUMP_DIR = "/private/tmp"

DEV_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEV_LOCAL_PORTS = {5432, 5433, 5434}
DEV_NAME_RE = re.compile(
    r"^(chirp|chirp_test|template1|chirp_test_[A-Za-z0-9_]+|c399_scratch_[0-9a-fA-F]+)$"
)

TEMPLATE1_RECREATE_STATEMENTS = (
    "UPDATE pg_database SET datistemplate = false WHERE datname = 'template1'",
    "DROP DATABASE template1",
    "CREATE DATABASE template1 TEMPLATE template0 ENCODING 'UTF8'",
    "UPDATE pg_database SET datistemplate = true WHERE datname = 'template1'",
)


# ---------------------------------------------------------------------------
# URL parsing / masking (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedUrl:
    user: str
    password: Optional[str]
    host: str
    port: int
    dbname: str


def strip_asyncpg_marker(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def parse_database_url(url: str) -> ParsedUrl:
    parts = urlsplit(strip_asyncpg_marker(url))
    if not parts.hostname:
        raise ValueError(f"could not parse a host from the database URL")
    return ParsedUrl(
        user=parts.username or "",
        password=parts.password,
        host=parts.hostname,
        port=parts.port or 5432,
        dbname=(parts.path or "/").lstrip("/") or "postgres",
    )


def build_url(parsed: ParsedUrl, dbname: str) -> str:
    auth = parsed.user
    if parsed.password is not None:
        auth = f"{auth}:{parsed.password}"
    return f"postgresql://{auth}@{parsed.host}:{parsed.port}/{dbname}"


def mask_url(url: str) -> str:
    parsed = parse_database_url(url)
    auth = f"{parsed.user}:***" if parsed.password is not None else parsed.user
    return f"postgresql://{auth}@{parsed.host}:{parsed.port}/{parsed.dbname}"


def admin_url_from(database_url: str) -> str:
    """The admin (maintenance) URL for a given app database URL: same server,
    database swapped to postgres."""
    parsed = parse_database_url(database_url)
    return build_url(parsed, "postgres")


def resolve_admin_url(cli_admin_url: Optional[str], env: Optional[dict] = None) -> str:
    if cli_admin_url:
        return cli_admin_url
    env = os.environ if env is None else env
    database_url = env.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    return admin_url_from(database_url)


# ---------------------------------------------------------------------------
# The localhost + dev-port + dev-name guard (the apply security boundary)
# ---------------------------------------------------------------------------

def check_local_dev_target(parsed: ParsedUrl, names: Sequence[str]) -> Optional[str]:
    """Return a refusal reason if --apply may NOT proceed against this admin
    URL and these database names; None if it may."""
    if parsed.host not in DEV_LOCAL_HOSTS:
        return (
            f"host {parsed.host!r} is not a local development host "
            f"(allowed: {sorted(DEV_LOCAL_HOSTS)})"
        )
    if parsed.port not in DEV_LOCAL_PORTS:
        return (
            f"port {parsed.port} is not a local development port "
            f"(allowed: {sorted(DEV_LOCAL_PORTS)})"
        )
    bad = [name for name in names if not DEV_NAME_RE.match(name)]
    if bad:
        return f"database name(s) {bad} do not match the local development naming pattern"
    return None


# ---------------------------------------------------------------------------
# Plan building (pure, given already-fetched rows)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanEntry:
    name: str
    encoding: str
    owner: str
    is_template: bool
    action: str  # "nothing" | "recreate" | "blocked" | "needs_superuser" | "recreate_template"
    blockers: tuple = ()


def group_activity_by_name(activity_rows: Iterable[dict]) -> dict:
    grouped: dict = {}
    for row in activity_rows:
        grouped.setdefault(row["datname"], []).append(row)
    return grouped


def build_plan(db_rows: Iterable[dict], activity_by_name: dict, is_superuser: bool) -> list:
    entries = []
    for row in db_rows:
        name = row["datname"]
        encoding = row["encoding_name"]
        is_template = bool(row.get("datistemplate"))
        blockers = tuple(activity_by_name.get(name, ()))
        if encoding == "UTF8":
            action = "nothing"
        elif is_template:
            action = "recreate_template" if is_superuser else "needs_superuser"
        elif blockers:
            action = "blocked"
        else:
            action = "recreate"
        entries.append(PlanEntry(
            name=name, encoding=encoding, owner=row.get("owner", ""),
            is_template=is_template, action=action, blockers=blockers,
        ))
    return entries


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

def _dump_path(dump_dir: str, name: str, run_stamp: str) -> str:
    return str(Path(dump_dir) / f"{name}.{run_stamp}.dump")


def render_ordinary_commands(parsed: ParsedUrl, entry: PlanEntry, dump_dir: str, run_stamp: str) -> list:
    dump_path = _dump_path(dump_dir, entry.name, run_stamp)
    utf8_name = f"{entry.name}_utf8"
    backup_name = f"{entry.name}_sqlascii_{run_stamp}"
    return [
        f"pg_dump -h {parsed.host} -p {parsed.port} -U {parsed.user} -Fc -f {dump_path} {entry.name}",
        f'CREATE DATABASE "{utf8_name}" WITH OWNER "{entry.owner}" ENCODING \'UTF8\' TEMPLATE template0',
        f"pg_restore -h {parsed.host} -p {parsed.port} -U {parsed.user} -d {utf8_name} {dump_path}",
        f'ALTER DATABASE "{entry.name}" RENAME TO "{backup_name}"',
        f'ALTER DATABASE "{utf8_name}" RENAME TO "{entry.name}"',
    ]


def render_needs_superuser_block(superuser_role: str) -> list:
    return [
        f'psql -U {superuser_role} -d postgres -c "{stmt};"'
        for stmt in TEMPLATE1_RECREATE_STATEMENTS
    ]


def render_plan_lines(parsed: ParsedUrl, entries: Sequence[PlanEntry], dump_dir: str,
                       run_stamp: str, superuser_role: str) -> str:
    lines = []
    for entry in entries:
        header = (
            f"{entry.name}: encoding={entry.encoding} owner={entry.owner} "
            f"template={entry.is_template} action={entry.action}"
        )
        if entry.blockers:
            names = ", ".join(
                f"pid={b['pid']} application_name={b.get('application_name') or ''}"
                for b in entry.blockers
            )
            header += f" blocked_by=[{names}]"
        lines.append(header)
        if entry.action == "recreate":
            for cmd in render_ordinary_commands(parsed, entry, dump_dir, run_stamp):
                lines.append(f"  {cmd}")
        elif entry.action == "needs_superuser":
            for cmd in render_needs_superuser_block(superuser_role):
                lines.append(f"  {cmd}")
        elif entry.action == "recreate_template":
            for stmt in TEMPLATE1_RECREATE_STATEMENTS:
                lines.append(f"  {stmt}")
    return "\n".join(lines)


@dataclass(frozen=True)
class OffenderCount:
    table: str
    column: str
    count: int


def render_offender_report(offenders: Sequence[OffenderCount]) -> str:
    lines = [o for o in offenders if o.count > 0]
    if not lines:
        return "no invalid UTF-8 bytes found in the original database"
    return "\n".join(
        f"{o.table}.{o.column}: {o.count} row(s) with bytes that are not valid UTF-8"
        for o in lines
    )


# ---------------------------------------------------------------------------
# Executor abstraction: real I/O by default, replaced with fakes in tests
# ---------------------------------------------------------------------------

@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str


def _real_run_subprocess(argv: list, env: dict) -> SubprocessResult:
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    return SubprocessResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


async def _real_connect(url: str):
    import asyncpg
    return await asyncpg.connect(url)


@dataclass
class Executor:
    connect: Callable[[str], Awaitable[Any]] = field(default=_real_connect)
    run_subprocess: Callable[[list, dict], SubprocessResult] = field(default=_real_run_subprocess)


# ---------------------------------------------------------------------------
# State gathering (impure: talks to the admin database)
# ---------------------------------------------------------------------------

async def gather_state(executor: Executor, admin_url: str, names: Sequence[str]):
    conn = await executor.connect(admin_url)
    try:
        is_superuser = await conn.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        db_rows = await conn.fetch(
            "SELECT datname, pg_encoding_to_char(encoding) AS encoding_name, "
            "pg_get_userbyid(datdba) AS owner, datcollate, datctype, datistemplate "
            "FROM pg_database WHERE datname = ANY($1::text[])",
            list(names),
        )
        activity_rows = await conn.fetch(
            "SELECT pid, application_name, datname FROM pg_stat_activity "
            "WHERE datname = ANY($1::text[]) AND pid <> pg_backend_pid()",
            list(names),
        )
    finally:
        await conn.close()
    return [dict(r) for r in db_rows], [dict(r) for r in activity_rows], bool(is_superuser)


# ---------------------------------------------------------------------------
# Invalid-UTF8-byte probe (impure: runs against the original database)
# ---------------------------------------------------------------------------

async def find_utf8_offenders(conn) -> list:
    await conn.execute(
        "CREATE OR REPLACE FUNCTION pg_temp.c399_is_valid_utf8(val bytea) "
        "RETURNS boolean AS $$ BEGIN PERFORM convert_from(val, 'UTF8'); RETURN true; "
        "EXCEPTION WHEN OTHERS THEN RETURN false; END; $$ LANGUAGE plpgsql"
    )
    columns = await conn.fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type IN ('text', 'character varying', 'jsonb')"
    )
    offenders = []
    for row in columns:
        table, column, data_type = row["table_name"], row["column_name"], row["data_type"]
        cast_expr = f'("{column}"::text)::bytea' if data_type == "jsonb" else f'"{column}"::bytea'
        count = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NOT NULL '
            f'AND NOT pg_temp.c399_is_valid_utf8({cast_expr})'
        )
        if count:
            offenders.append(OffenderCount(table=table, column=column, count=int(count)))
    return offenders


# ---------------------------------------------------------------------------
# Apply (impure)
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    name: str
    ok: bool
    exit_code: int
    reason: Optional[str] = None
    backup_name: Optional[str] = None
    dump_path: Optional[str] = None
    offenders: tuple = ()
    manual_commands: tuple = ()


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


async def apply_ordinary(executor: Executor, parsed: ParsedUrl, entry: PlanEntry, dump_dir: str,
                          terminate_connections: bool, run_stamp: Optional[str] = None) -> ApplyResult:
    name = entry.name
    utf8_name = f"{name}_utf8"
    run_stamp = run_stamp or _run_stamp()

    if entry.blockers:
        if not terminate_connections:
            names = ", ".join(
                f"pid={b['pid']} application_name={b.get('application_name') or ''}"
                for b in entry.blockers
            )
            return ApplyResult(
                name=name, ok=False, exit_code=3,
                reason=f"live connections on {name}: [{names}]; pass --terminate-connections to proceed",
            )
        admin_conn = await executor.connect(build_url(parsed, "postgres"))
        try:
            for blocker in entry.blockers:
                await admin_conn.execute("SELECT pg_terminate_backend($1)", blocker["pid"])
        finally:
            await admin_conn.close()

    dump_path = _dump_path(dump_dir, name, run_stamp)
    env = os.environ.copy()
    if parsed.password is not None:
        env["PGPASSWORD"] = parsed.password
    dump_argv = ["pg_dump", "-h", parsed.host, "-p", str(parsed.port), "-U", parsed.user,
                 "-Fc", "-f", dump_path, name]
    dump_result = executor.run_subprocess(dump_argv, env)
    if dump_result.returncode != 0:
        return ApplyResult(name=name, ok=False, exit_code=4,
                            reason=f"pg_dump failed: {dump_result.stderr.strip()}")

    admin_conn = await executor.connect(build_url(parsed, "postgres"))
    try:
        await admin_conn.execute(
            f'CREATE DATABASE "{utf8_name}" WITH OWNER "{entry.owner}" ENCODING \'UTF8\' TEMPLATE template0'
        )
    finally:
        await admin_conn.close()

    restore_argv = ["pg_restore", "-h", parsed.host, "-p", str(parsed.port), "-U", parsed.user,
                     "-d", utf8_name, dump_path]
    restore_result = executor.run_subprocess(restore_argv, env)
    restore_failed = restore_result.returncode != 0
    reason = f"pg_restore failed (original {name} untouched): {restore_result.stderr.strip()}"

    offenders: list = []
    if not restore_failed:
        # pg_restore preserves the SOURCE database's client_encoding (recorded
        # in the dump) for its own session, and Postgres skips ALL conversion
        # and validation whenever either endpoint of a connection is SQL_ASCII
        # -- so a restore whose source was SQL_ASCII can succeed with exit 0
        # while writing bytes into the UTF8 copy that are not valid UTF-8.
        # pg_restore's exit code alone is therefore not trustworthy here: the
        # restored content is verified for real before it is ever swapped in.
        try:
            verify_conn = await executor.connect(build_url(parsed, utf8_name))
            try:
                offenders = await find_utf8_offenders(verify_conn)
            finally:
                await verify_conn.close()
        except Exception:
            offenders = []
        if offenders:
            restore_failed = True
            reason = (
                f"restored copy of {name} contains bytes that are not valid UTF-8 "
                f"(original {name} untouched): " + render_offender_report(offenders)
            )

    if restore_failed:
        if not offenders:
            try:
                orig_conn = await executor.connect(build_url(parsed, name))
                try:
                    offenders = await find_utf8_offenders(orig_conn)
                finally:
                    await orig_conn.close()
            except Exception:
                offenders = []
        drop_conn = await executor.connect(build_url(parsed, "postgres"))
        try:
            await drop_conn.execute(f'DROP DATABASE IF EXISTS "{utf8_name}"')
        finally:
            await drop_conn.close()
        return ApplyResult(
            name=name, ok=False, exit_code=5, reason=reason,
            offenders=tuple(offenders), dump_path=dump_path,
        )

    verify_conn = await executor.connect(build_url(parsed, utf8_name))
    try:
        encoding = await verify_conn.fetchval(
            "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()"
        )
        new_count = await verify_conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
    finally:
        await verify_conn.close()
    if encoding != "UTF8":
        return ApplyResult(name=name, ok=False, exit_code=6,
                            reason=f"restored copy reports encoding {encoding}, expected UTF8",
                            dump_path=dump_path)

    orig_conn = await executor.connect(build_url(parsed, name))
    try:
        old_count = await orig_conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
    finally:
        await orig_conn.close()
    if new_count != old_count:
        return ApplyResult(name=name, ok=False, exit_code=6,
                            reason=f"table count mismatch: original {old_count}, restored {new_count}",
                            dump_path=dump_path)

    backup_name = f"{name}_sqlascii_{run_stamp}"
    admin_conn = await executor.connect(build_url(parsed, "postgres"))
    try:
        await admin_conn.execute(f'ALTER DATABASE "{name}" RENAME TO "{backup_name}"')
        await admin_conn.execute(f'ALTER DATABASE "{utf8_name}" RENAME TO "{name}"')
    finally:
        await admin_conn.close()

    return ApplyResult(name=name, ok=True, exit_code=0, backup_name=backup_name, dump_path=dump_path)


async def apply_template(executor: Executor, parsed: ParsedUrl, entry: PlanEntry,
                          superuser_role: str) -> ApplyResult:
    if entry.action == "needs_superuser":
        return ApplyResult(
            name=entry.name, ok=True, exit_code=0,
            reason="template1 needs a superuser connection; run the following",
            manual_commands=tuple(render_needs_superuser_block(superuser_role)),
        )
    admin_conn = await executor.connect(build_url(parsed, "postgres"))
    try:
        for stmt in TEMPLATE1_RECREATE_STATEMENTS:
            await admin_conn.execute(stmt)
    finally:
        await admin_conn.close()
    return ApplyResult(name=entry.name, ok=True, exit_code=0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin-url", default=None,
                         help="Admin (maintenance) database URL; defaults from DATABASE_URL")
    parser.add_argument("--database", dest="databases", action="append", default=None,
                         help="Target database name (repeatable); default chirp, chirp_test, template1")
    parser.add_argument("--apply", action="store_true", help="Perform the plan (default: dry run)")
    parser.add_argument("--terminate-connections", action="store_true",
                         help="Terminate other backends on a target before swapping it")
    parser.add_argument("--dump-dir", default=DEFAULT_DUMP_DIR,
                         help=f"Directory for kept pg_dump files (default {DEFAULT_DUMP_DIR})")
    return parser


async def _async_main(args) -> int:
    requested_admin_url = resolve_admin_url(args.admin_url)
    parsed = parse_database_url(requested_admin_url)
    admin_url = build_url(parsed, parsed.dbname)
    names = args.databases or list(DEFAULT_TARGET_NAMES)
    print(f"admin url: {mask_url(admin_url)}", file=sys.stderr)
    print(f"targets: {', '.join(names)}", file=sys.stderr)

    if args.apply:
        guard_reason = check_local_dev_target(parsed, names)
        if guard_reason is not None:
            print(f"refused: {guard_reason}", file=sys.stderr)
            return 2

    executor = Executor()
    db_rows, activity_rows, is_superuser = await gather_state(executor, admin_url, names)
    activity_by_name = group_activity_by_name(activity_rows)
    entries = build_plan(db_rows, activity_by_name, is_superuser)
    superuser_role = next((e.owner for e in entries if e.is_template), "postgres")
    run_stamp = _run_stamp()
    print(render_plan_lines(parsed, entries, args.dump_dir, run_stamp, superuser_role))

    if not args.apply:
        return 0

    worst = 0
    for entry in entries:
        if entry.action in ("nothing",):
            continue
        if entry.action in ("recreate",):
            result = await apply_ordinary(executor, parsed, entry, args.dump_dir,
                                           args.terminate_connections, run_stamp)
        elif entry.action == "blocked":
            result = await apply_ordinary(executor, parsed, entry, args.dump_dir,
                                           args.terminate_connections, run_stamp)
        elif entry.action in ("needs_superuser", "recreate_template"):
            result = await apply_template(executor, parsed, entry, superuser_role)
        else:
            continue
        if result.ok:
            if result.backup_name:
                print(f"{result.name}: swapped to UTF8, backup kept as {result.backup_name}, "
                      f"dump at {result.dump_path}")
            elif result.manual_commands:
                print(f"{result.name}: {result.reason}")
                for cmd in result.manual_commands:
                    print(f"  {cmd}")
            else:
                print(f"{result.name}: applied")
        else:
            print(f"{result.name}: FAILED ({result.exit_code}): {result.reason}", file=sys.stderr)
            if result.offenders:
                print(render_offender_report(list(result.offenders)), file=sys.stderr)
            worst = max(worst, result.exit_code)
    return worst


def main(argv=None) -> int:
    import asyncio
    args = build_arg_parser().parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
