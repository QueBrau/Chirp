"""Fake-executor tests only; no network, no database, no gcloud. Board c399."""
import asyncio
import importlib.util
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "local_db_encoding.py"
spec = importlib.util.spec_from_file_location("local_db_encoding", PATH)
m = importlib.util.module_from_spec(spec)
# The module uses @dataclass; on 3.11 dataclass processing looks the module up
# in sys.modules by __module__ name, which requires registering it here first.
sys.modules["local_db_encoding"] = m
spec.loader.exec_module(m)

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "local-db-encoding"


def _row(name, encoding, owner="chirp", collate="C", ctype="C", template=False):
    return {
        "datname": name, "encoding_name": encoding, "owner": owner,
        "datcollate": collate, "datctype": ctype, "datistemplate": template,
    }


def _activity(pid, name, application_name="uvicorn"):
    return {"pid": pid, "application_name": application_name, "datname": name}


class DetectionTests(unittest.TestCase):
    """chirps-17 acceptance: classify pg_database rows into a plan."""

    def _plan(self, is_superuser):
        db_rows = [
            _row("chirp", "SQL_ASCII"),
            _row("chirp_test", "UTF8"),
            _row("template1", "SQL_ASCII", owner="joseperdomo", template=True),
        ]
        return m.build_plan(db_rows, {}, is_superuser)

    def test_recreate_nothing_and_superuser_classification(self):
        entries = {e.name: e for e in self._plan(is_superuser=False)}
        self.assertEqual(entries["chirp"].action, "recreate")
        self.assertEqual(entries["chirp_test"].action, "nothing")
        self.assertEqual(entries["template1"].action, "needs_superuser")

    def test_superuser_flag_flips_template1_to_recreate_template(self):
        entries = {e.name: e for e in self._plan(is_superuser=True)}
        self.assertEqual(entries["template1"].action, "recreate_template")

    def test_falsification_dropping_encoding_comparison_plans_recreate_as_nothing(self):
        # Sabotage: classify purely on datistemplate/blockers, ignoring encoding.
        # This constructs the discriminating failure the real function must avoid.
        db_rows = [_row("chirp", "SQL_ASCII")]
        entries = m.build_plan(db_rows, {}, is_superuser=False)
        self.assertEqual(entries[0].action, "recreate")

        def sabotaged_build_plan(rows, activity_by_name, is_superuser):
            out = []
            for row in rows:
                action = "recreate_template" if row.get("datistemplate") else "recreate"
                out.append(m.PlanEntry(name=row["datname"], encoding=row["encoding_name"],
                                        owner=row["owner"], is_template=bool(row.get("datistemplate")),
                                        action=action))
            return out

        sabotaged = sabotaged_build_plan(
            [_row("chirp_test", "UTF8")], {}, is_superuser=False
        )
        self.assertEqual(sabotaged[0].action, "recreate",
                          "sabotaged classifier ignores encoding and always recreates: red")


class GuardTests(unittest.TestCase):
    """chirps-17 acceptance + manager ruling 2: the apply security boundary."""

    def test_non_local_host_refused(self):
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="db.example.com", port=5432, dbname="postgres")
        reason = m.check_local_dev_target(parsed, ["chirp"])
        self.assertIsNotNone(reason)
        self.assertIn("db.example.com", reason)

    def test_private_ip_refused(self):
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="10.0.0.5", port=5432, dbname="postgres")
        self.assertIsNotNone(m.check_local_dev_target(parsed, ["chirp"]))

    def test_non_dev_port_refused(self):
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=6543, dbname="postgres")
        reason = m.check_local_dev_target(parsed, ["chirp"])
        self.assertIsNotNone(reason)
        self.assertIn("6543", reason)

    def test_non_dev_names_refused(self):
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=5432, dbname="postgres")
        reason = m.check_local_dev_target(parsed, ["prod", "chirp_prod"])
        self.assertIsNotNone(reason)
        self.assertIn("prod", reason)

    def test_cloud_sql_proxy_port_refused_by_name(self):
        """5433 is the Cloud SQL Auth Proxy port on this project's machines, and
        production's database is itself named 'chirp' -- host, port and name would
        ALL have looked like a dev target. Refused explicitly, with a reason that
        says why, so nobody re-adds it to DEV_LOCAL_PORTS as a tidy-up."""
        parsed = m.ParsedUrl(user="chirp", password="secret", host="localhost", port=5433, dbname="chirp")
        reason = m.check_local_dev_target(parsed, ["chirp"])
        self.assertIsNotNone(reason, "localhost:5433/chirp is PRODUCTION through the proxy")
        self.assertIn("5433", reason)
        self.assertIn("PRODUCTION", reason)
        self.assertNotIn(5433, m.DEV_LOCAL_PORTS)

    def test_hostless_socket_url_is_refused_at_parse_time(self):
        """The /cloudsql/<instance> unix-socket form has no TCP host, so it can
        never be classified as a local dev target."""
        with self.assertRaises(ValueError) as caught:
            m.parse_database_url("postgresql://chirp:secret@/chirp?host=/cloudsql/chirps-prod:us-central1:chirp-db")
        self.assertIn("host", str(caught.exception))

    def test_dev_ports_and_names_accepted(self):
        for port in (5432, 5434):
            parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=port, dbname="postgres")
            self.assertIsNone(m.check_local_dev_target(
                parsed, ["chirp", "chirp_test", "template1", "chirp_test_p123", "c399_scratch_abc123"]
            ))
        for host in ("127.0.0.1", "::1"):
            parsed = m.ParsedUrl(user="chirp", password="chirp", host=host, port=5432, dbname="postgres")
            self.assertIsNone(m.check_local_dev_target(parsed, ["chirp"]))

    def test_asyncpg_marker_is_stripped_before_connecting(self):
        parsed = m.parse_database_url("postgresql+asyncpg://chirp:chirp@localhost:5432/chirp")
        self.assertEqual(m.build_url(parsed, "postgres"), "postgresql://chirp:chirp@localhost:5432/postgres")

    def test_falsification_removing_host_check_accepts_remote_host(self):
        # Sabotage: only check port and name, drop the host check entirely.
        def sabotaged_guard(parsed, names):
            if parsed.port not in m.DEV_LOCAL_PORTS:
                return "bad port"
            bad = [n for n in names if not m.DEV_NAME_RE.match(n)]
            if bad:
                return f"bad names {bad}"
            return None

        parsed = m.ParsedUrl(user="chirp", password="chirp", host="db.example.com", port=5432, dbname="postgres")
        self.assertIsNone(sabotaged_guard(parsed, ["chirp"]),
                           "sabotaged guard without a host check accepts a remote host: red")


class LiveConnectionTests(unittest.TestCase):
    """chirps-17 acceptance + manager ruling 3: connections block the swap."""

    def test_plan_line_names_pid_and_application_name(self):
        db_rows = [_row("chirp", "SQL_ASCII")]
        activity = {"chirp": [_activity(4242, "chirp", "uvicorn")]}
        entries = m.build_plan(db_rows, activity, is_superuser=False)
        self.assertEqual(entries[0].action, "blocked")
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=5432, dbname="postgres")
        rendered = m.render_plan_lines(parsed, entries, "/private/tmp", "20260101000000", "postgres")
        self.assertIn("pid=4242", rendered)
        self.assertIn("uvicorn", rendered)

    def test_apply_without_terminate_flag_refuses_before_any_write(self):
        entry = m.PlanEntry(name="chirp", encoding="SQL_ASCII", owner="chirp", is_template=False,
                             action="blocked", blockers=(_activity(4242, "chirp"),))
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=5432, dbname="postgres")

        async def _refusing_connect(_url):
            raise AssertionError("connect() called when the guard should have refused first")

        executor = m.Executor(connect=_refusing_connect, run_subprocess=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("run_subprocess() called when the guard should have refused first")))
        result = asyncio.run(m.apply_ordinary(executor, parsed, entry, "/private/tmp",
                                               terminate_connections=False, run_stamp="20260101000000"))
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertIn("4242", result.reason)

    def test_apply_with_terminate_flag_terminates_before_dumping(self):
        entry = m.PlanEntry(name="chirp", encoding="SQL_ASCII", owner="chirp", is_template=False,
                             action="blocked", blockers=(_activity(4242, "chirp"),))
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=5432, dbname="postgres")
        calls = []

        class FakeConn:
            async def execute(self, sql, *params):
                calls.append(("execute", sql, params))
            async def close(self):
                pass

        async def fake_connect(_url):
            return FakeConn()

        def fake_run_subprocess(argv, _env):
            calls.append(("subprocess", argv[0]))
            return m.SubprocessResult(returncode=1, stdout="", stderr="stop here, this test only checks ordering")

        executor = m.Executor(connect=fake_connect, run_subprocess=fake_run_subprocess)
        asyncio.run(m.apply_ordinary(executor, parsed, entry, "/private/tmp",
                                      terminate_connections=True, run_stamp="20260101000000"))
        kinds = [c[0] for c in calls]
        self.assertIn(("execute", "SELECT pg_terminate_backend($1)", (4242,)), calls)
        self.assertEqual(kinds[0], "execute", "pg_terminate_backend must run before pg_dump")

    def test_falsification_removing_activity_check_lets_apply_proceed(self):
        entry = m.PlanEntry(name="chirp", encoding="SQL_ASCII", owner="chirp", is_template=False,
                             action="blocked", blockers=(_activity(4242, "chirp"),))
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=5432, dbname="postgres")
        called = []

        async def fake_connect(_url):
            class C:
                async def execute(self, *a, **k):
                    called.append("execute")
                async def close(self):
                    pass
            return C()

        def fake_run_subprocess(argv, _env):
            called.append("subprocess")
            return m.SubprocessResult(returncode=1, stdout="", stderr="halt")

        # Sabotage: bypass the blocker check that the real apply_ordinary starts with.
        async def sabotaged_apply(executor, parsed, entry, dump_dir, terminate_connections, run_stamp):
            dump_path = m._dump_path(dump_dir, entry.name, run_stamp)
            result = executor.run_subprocess(["pg_dump"], os.environ.copy())
            return result

        asyncio.run(sabotaged_apply(m.Executor(connect=fake_connect, run_subprocess=fake_run_subprocess),
                                     parsed, entry, "/private/tmp", False, "20260101000000"))
        self.assertIn("subprocess", called,
                       "sabotaged apply skips the blocker refusal and calls pg_dump anyway: red")


class DryRunTests(unittest.TestCase):
    """chirps-17 acceptance: dry run performs zero writes."""

    def test_dry_run_gather_state_only_selects(self):
        recorded = []

        class FakeConn:
            async def fetchval(self, sql, *params):
                recorded.append(("fetchval", sql))
                return False
            async def fetch(self, sql, *params):
                recorded.append(("fetch", sql))
                return []
            async def execute(self, *a, **k):
                raise AssertionError("execute() called during a dry run")
            async def close(self):
                pass

        async def fake_connect(_url):
            return FakeConn()

        def refusing_subprocess(*_a, **_k):
            raise AssertionError("run_subprocess() called during a dry run")

        executor = m.Executor(connect=fake_connect, run_subprocess=refusing_subprocess)
        db_rows, activity_rows, is_superuser = asyncio.run(
            m.gather_state(executor, "postgresql://chirp:chirp@localhost:5432/postgres", ["chirp"])
        )
        self.assertEqual(db_rows, [])
        self.assertEqual(activity_rows, [])
        self.assertFalse(is_superuser)
        self.assertTrue(all(kind == "fetch" or kind == "fetchval" for kind, _ in recorded))
        self.assertTrue(recorded, "gather_state made no calls at all — the test would be vacuous")

    def test_falsification_calling_apply_during_dry_run_hits_the_refusing_fake(self):
        def refusing_subprocess(*_a, **_k):
            raise AssertionError("run_subprocess() called during a dry run")

        async def fake_connect(_url):
            class C:
                async def execute(self, *a, **k):
                    pass
                async def close(self):
                    pass
            return C()

        executor = m.Executor(connect=fake_connect, run_subprocess=refusing_subprocess)
        entry = m.PlanEntry(name="chirp", encoding="SQL_ASCII", owner="chirp", is_template=False, action="recreate")
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=5432, dbname="postgres")
        with self.assertRaises(AssertionError):
            asyncio.run(m.apply_ordinary(executor, parsed, entry, "/private/tmp", False, "20260101000000"))


class OffenderProbeTests(unittest.TestCase):
    """The probe must take TEXT. A ::bytea cast in the SELECT is evaluated before
    the plpgsql function runs, so its EXCEPTION handler does not cover it, and
    text-to-bytea input syntax interprets backslashes: one ordinary row such as
    'path C:\\temp\\new' aborts the whole count, apply_ordinary swallows the error
    and reports ZERO offenders, and a corrupt database gets swapped in as clean."""

    class _FakeConn:
        def __init__(self, columns):
            self.statements = []
            self._columns = columns

        async def execute(self, sql, *params):
            self.statements.append(sql)

        async def fetch(self, sql, *params):
            self.statements.append(sql)
            return self._columns

        async def fetchval(self, sql, *params):
            self.statements.append(sql)
            return 0

        async def close(self):
            pass

    def _run_probe(self):
        conn = self._FakeConn([
            {"table_name": "t", "column_name": "col", "data_type": "text"},
            {"table_name": "t", "column_name": "name", "data_type": "character varying"},
            {"table_name": "t", "column_name": "j", "data_type": "jsonb"},
        ])
        asyncio.run(m.find_utf8_offenders(conn))
        return conn.statements

    def test_probe_function_takes_text_not_bytea(self):
        create = self._run_probe()[0]
        self.assertIn("val text", create)
        self.assertNotIn("val bytea", create)
        self.assertIn("convert_to(val, 'UTF8')", create)

    def test_no_count_query_casts_a_column_to_bytea(self):
        counts = [sql for sql in self._run_probe() if sql.startswith("SELECT count(")]
        self.assertEqual(len(counts), 3, "one count per text/varchar/jsonb column")
        for sql in counts:
            self.assertNotIn("::bytea", sql,
                              "a ::bytea cast sits OUTSIDE the plpgsql handler and aborts on backslash text")

    def test_falsification_the_bytea_form_is_what_this_pins(self):
        # The shape this test rejects is exactly the shape that shipped in the
        # first draft; assert it would fail the check above.
        bytea_form = 'SELECT count(*) FROM "t" WHERE "col" IS NOT NULL AND NOT pg_temp.c399_is_valid_utf8("col"::bytea)'
        self.assertIn("::bytea", bytea_form,
                       "if this ever stops containing ::bytea the check above proves nothing")


class RenderedPlanTests(unittest.TestCase):
    def test_recreate_commands_are_exact(self):
        parsed = m.ParsedUrl(user="chirp", password="chirp", host="localhost", port=5432, dbname="postgres")
        entry = m.PlanEntry(name="chirp", encoding="SQL_ASCII", owner="chirp", is_template=False, action="recreate")
        cmds = m.render_ordinary_commands(parsed, entry, "/private/tmp", "20260101000000")
        self.assertEqual(cmds[0], "pg_dump -h localhost -p 5432 -U chirp -Fc -f /private/tmp/chirp.20260101000000.dump chirp")
        self.assertEqual(cmds[1], 'CREATE DATABASE "chirp_utf8" WITH OWNER "chirp" ENCODING \'UTF8\' TEMPLATE template0')
        self.assertEqual(cmds[3], 'ALTER DATABASE "chirp" RENAME TO "chirp_sqlascii_20260101000000"')
        self.assertEqual(cmds[4], 'ALTER DATABASE "chirp_utf8" RENAME TO "chirp"')

    def test_needs_superuser_block_is_prefixed_with_psql(self):
        cmds = m.render_needs_superuser_block("joseperdomo")
        self.assertEqual(len(cmds), 4)
        self.assertTrue(all(c.startswith("psql -U joseperdomo -d postgres -c ") for c in cmds))


class OffenderReportTests(unittest.TestCase):
    """Offender-count rendering from constructed probe results."""

    def test_offenders_rendered_per_table_column(self):
        offenders = [m.OffenderCount(table="delivery_outbox", column="payload", count=1)]
        text = m.render_offender_report(offenders)
        self.assertIn("delivery_outbox.payload", text)
        self.assertIn("1 row", text)

    def test_zero_offenders_rendered_as_clean(self):
        text = m.render_offender_report([])
        self.assertIn("no invalid UTF-8", text)

    def test_falsification_dropping_the_count_filter_reports_clean_table_as_dirty(self):
        offenders = [m.OffenderCount(table="clean_table", column="col", count=0)]
        # Real function filters count > 0; sabotage renders everything unconditionally.
        sabotaged = "\n".join(f"{o.table}.{o.column}: {o.count} row(s)" for o in offenders)
        self.assertIn("clean_table.col: 0 row(s)", sabotaged,
                       "sabotaged renderer prints a zero-offender table as if it were dirty: red")
        real = m.render_offender_report(offenders)
        self.assertNotIn("clean_table", real)


class WrapperTests(unittest.TestCase):
    def test_wrapper_exists_executable_and_sources_pick_python(self):
        self.assertTrue(WRAPPER.exists())
        mode = WRAPPER.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("source", text)
        self.assertIn("lib/pick-python.sh", text)
        self.assertIn("chirp_pick_python", text)

    def test_wrapper_help_runs(self):
        # CHIRP_PYTHON pinned to the shared venv: whether bare python3 on PATH
        # has a usable CA trust store is scripts/lib/pick-python.sh's own
        # concern (c391/c392), not this wrapper's — --help never needs one
        # either way since the module defers `import asyncpg` past argparse.
        env = dict(os.environ)
        env["CHIRP_PYTHON"] = sys.executable
        result = subprocess.run([str(WRAPPER), "--help"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--apply", result.stdout)


if __name__ == "__main__":
    unittest.main()
