import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from database import recommend, scan_workspace
import hashlib
import os


class DatabaseOptionsTest(unittest.TestCase):
    def facts(self, engine="sqlite", **kwargs):
        return {"engine": engine, "evidence": [{"path": "app/config.py", "kind": "connection-config", "engine": engine}], **kwargs}

    def test_stack_without_database_evidence_is_not_a_recommendation(self):
        self.assertEqual("NEEDS_EVIDENCE", recommend({"stack": "Next.js Prisma FastAPI Spring"})["decision"])
        self.assertEqual("NEEDS_EVIDENCE", recommend({"engine": "postgresql"})["decision"])

    def test_sqlite_scale_out_is_blocked_even_if_only_maximum_is_two(self):
        for mode in ("preserve", "local-sqlite"):
            result = recommend(self.facts(max_replicas=2, requested_mode=mode))
            self.assertEqual("BLOCKED", result["decision"])
            self.assertIn("SQLITE_HORIZONTAL_UNSUPPORTED", result["reasons"][0])

    def test_local_sqlite_keeps_engine_and_exposes_missing_persistence(self):
        result = recommend(self.facts(max_replicas=1, access="read-write"))
        self.assertEqual("sqlite", result["selected_engine"])
        self.assertEqual("PERSISTENT_SQLITE_RENDERER_NOT_IMPLEMENTED", result["support"])
        self.assertFalse(result["deployment_approved"])

    def test_existing_mysql_is_not_changed_by_framework_or_default(self):
        result = recommend(self.facts("mysql", stack="FastAPI SQLAlchemy"))
        self.assertEqual("mysql", result["selected_engine"])
        self.assertEqual("preserve-existing-connection", result["selection"])

    def test_requested_engine_conversion_is_a_plan_not_execution(self):
        result = recommend(self.facts(requested_mode="container-postgres", max_replicas=3))
        self.assertEqual("MIGRATION_REQUIRED", result["decision"])
        self.assertEqual(("sqlite", "postgresql"), (result["current_engine"], result["selected_engine"]))
        self.assertEqual("TEAM_DATABASE_RUNTIME_NOT_CONFIGURED", result["support"])
        self.assertFalse(result["automatic_engine_change"])
        self.assertFalse(result["deployment_approved"])

    def test_managed_connection_uses_only_secret_reference(self):
        facts = self.facts("postgresql", requested_mode="managed")
        self.assertEqual("BLOCKED_ENV", recommend(facts)["decision"])
        facts["connection_secret_ref"] = {"name": "app-db", "key": "DATABASE_URL"}
        result = recommend(facts)
        self.assertEqual("PLAN", result["decision"])
        self.assertEqual("MANAGED_CONNECTION_RENDERER_NOT_IMPLEMENTED", result["support"])
        for change in ({"url": "postgres://user:secret@host/db"}, {"connection_secret_ref": {"name": "db", "key": "uri", "password": "secret"}}):
            with self.assertRaises(ValueError):
                recommend({**facts, **change})

    def test_conflicts_and_invented_evidence_do_not_choose_engine(self):
        facts = self.facts("postgresql")
        facts["evidence"].append({"path": "legacy.py", "kind": "sqlite-open", "engine": "sqlite"})
        self.assertEqual("NEEDS_EVIDENCE", recommend(facts)["decision"])
        for path in ("../secret", "/etc/passwd", "postgres://secret@host"):
            bad = self.facts()
            bad["evidence"][0]["path"] = path
            with self.assertRaises(ValueError):
                recommend(bad)

    def test_prisma_commands_follow_evidenced_major_version(self):
        facts = self.facts("postgresql", migration={"tool": "prisma", "version": "7.4.0", "evidence": ["prisma/schema.prisma"]})
        self.assertIn(["prisma", "migrate", "deploy"], recommend(facts)["migration"]["command_examples"])
        facts["migration"]["version"] = "8.0.0"
        self.assertIn(["prisma", "db", "migrate"], recommend(facts)["migration"]["command_examples"])
        facts["migration"].pop("version")
        self.assertEqual("NEEDS_VERSION", recommend(facts)["migration"]["status"])
        self.assertEqual("NOT_CONFIGURED", recommend(self.facts())["migration"]["status"])

    def test_invalid_scalar_types_fail_closed(self):
        for value in (True, 0, -1, "2", 31):
            with self.assertRaises(ValueError):
                recommend(self.facts(max_replicas=value))

    def test_conflicting_target_and_invalid_secret_reference_are_rejected(self):
        with self.assertRaises(ValueError):
            recommend(self.facts(requested_mode="container-postgres", target_engine="mysql"))
        with self.assertRaises(ValueError):
            recommend(self.facts("postgresql", requested_mode="managed", connection_secret_ref={"name": "db..invalid", "key": "uri"}))

    def test_cli_exit_codes_and_secret_redaction(self):
        script = Path(__file__).with_name("database.py")
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            for facts, code in ((self.facts(), 0), (self.facts(max_replicas=2), 2),
                                ({**self.facts(), "password": "do-not-echo-this"}, 2)):
                evidence.write_text(json.dumps(facts))
                result = subprocess.run([sys.executable, str(script), str(evidence)], capture_output=True, text=True)
                self.assertEqual(code, result.returncode)
                self.assertNotIn("do-not-echo-this", result.stdout + result.stderr)
                self.assertIn("decision", json.loads(result.stdout))


class WorkspaceDBScanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, value):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else value.encode())
        return path

    def test_sqlite_source_and_header_are_hash_bound_and_not_opened(self):
        app = self.write('app.py', 'import sqlite3 as db\nconnection = db.connect("data.sqlite")\n')
        self.write('data.sqlite', b'SQLite format 3\x00' + b'not-a-valid-database')
        result = scan_workspace(self.root)
        self.assertEqual(result['facts']['engine'], 'sqlite')
        self.assertEqual(len(result['facts']['evidence']), 2)
        item = next(x for x in result['evidence_files'] if x['path'] == 'app.py')
        self.assertEqual(item['sha256'], hashlib.sha256(app.read_bytes()).hexdigest())
        self.assertEqual(result['review']['support'], 'PERSISTENT_SQLITE_RENDERER_NOT_IMPLEMENTED')
        self.assertFalse(result['deployment_approved'])

    def test_prisma_datasource_is_evidence_but_dependency_alone_is_not(self):
        self.write('package.json', '{"dependencies":{"@prisma/client":"7.4.0"}}')
        first = scan_workspace(self.root)
        self.assertEqual(first['facts']['engine'], 'unknown')
        self.assertEqual(first['orm_configs'][0]['framework'], '@prisma/client')
        self.write('prisma/schema.prisma', 'datasource db {\n provider = "postgresql"\n url = env("DATABASE_URL")\n}')
        self.assertEqual(scan_workspace(self.root)['facts']['engine'], 'postgresql')

    def test_source_connection_values_and_env_credentials_are_never_returned(self):
        self.write('app.py', 'from sqlalchemy import create_engine\nengine = create_engine("mysql+pymysql://u:do-not-emit@localhost/db")\n')
        self.write('.envrc', 'DATABASE_URL=postgres://ignored-secret@host/db')
        self.write('auth.json', '{"token":"ignored-auth"}')
        result = scan_workspace(self.root)
        self.assertEqual(result['facts']['engine'], 'mysql')
        rendered = json.dumps(result)
        for token in ('do-not-emit', 'ignored-secret', 'ignored-auth', 'localhost', '.envrc', 'auth.json'):
            self.assertNotIn(token, rendered)

    def test_conflicting_engines_do_not_choose_a_default(self):
        self.write('app.py', 'import sqlite3\ncon=sqlite3.connect("app.db")')
        self.write('src/main/resources/application.properties', 'spring.datasource.url=jdbc:postgresql://private-server/db')
        result = scan_workspace(self.root)
        self.assertEqual({x['engine'] for x in result['facts']['evidence']}, {'sqlite', 'postgresql'})
        self.assertEqual(result['facts']['engine'], 'unknown')
        self.assertEqual(result['review']['decision'], 'NEEDS_EVIDENCE')
        self.assertNotIn('private-server', json.dumps(result))

    def test_django_config_and_docstring_false_positive(self):
        self.write('settings.py', 'DATABASES={"default":{"ENGINE":"django.db.backends.sqlite3"}}')
        self.write('notes.py', '\"\"\"Example:\nDATABASE_URL = "mysql://fake:secret@server/db"\n\"\"\"')
        result = scan_workspace(self.root)
        self.assertEqual(result['facts']['engine'], 'sqlite')

    def test_symlink_fifo_large_file_and_limit_are_partial_not_silent_success(self):
        outside = self.root.parent / (self.root.name + '-external.py')
        outside.write_text('DATABASE_URL="postgres://do-not-read@host/db"'); self.addCleanup(outside.unlink)
        (self.root / 'linked.py').symlink_to(outside)
        os.mkfifo(self.root / 'pipe.py')
        self.write('large.py', b'x' * 40)
        result = scan_workspace(self.root, max_file_bytes=16)
        self.assertEqual(result['scan_status'], 'PARTIAL')
        self.assertEqual(result['facts']['engine'], 'unknown')
        self.assertEqual(result['evidence_files'], [])
        self.assertNotIn('do-not-read', json.dumps(result))

    def test_no_signal_is_unknown_and_tests_are_not_production_evidence(self):
        self.write('tests/test_db.py', 'import sqlite3\ncon=sqlite3.connect(":memory:")')
        self.write('app.py', 'print("web app")')
        result = scan_workspace(self.root)
        self.assertEqual(result['scan_status'], 'COMPLETE')
        self.assertEqual(result['facts']['engine'], 'unknown')
        self.assertEqual(result['review']['decision'], 'NEEDS_EVIDENCE')


if __name__ == "__main__":
    unittest.main()
