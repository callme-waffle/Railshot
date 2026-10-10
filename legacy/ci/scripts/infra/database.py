#!/usr/bin/env python3
"""Offline DB option review from explicit evidence. Never connects, installs, or migrates.

Usage: python3 platform/infra/database.py evidence.json
Exit 0: reviewable plan (not deployment approval); 2: blocked/missing/invalid evidence.
"""
import argparse
import ast
import hashlib
import os
import stat
import json
from pathlib import Path, PurePosixPath
import re
import sys

ENGINES = {"unknown", "none", "sqlite", "postgresql", "mysql", "mariadb", "sqlserver", "oracle"}
MODES = {"preserve", "local-sqlite", "container-postgres", "managed"}
EVIDENCE = {"datasource", "connection-config", "sqlite-open", "sqlite-file", "runtime-observation", "user-declaration"}


def relative_path(value):
    if not isinstance(value, str) or not value or ":" in value or "\\" in value:
        raise ValueError("evidence paths must be relative paths or non-secret observation identifiers")
    if PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
        raise ValueError("evidence paths must remain within the project")
    return value


def migration_tool(facts):
    if facts is None or facts == {}:
        return {"status": "NOT_CONFIGURED", "reason": "No migration tool evidence; do not invent a runner or mark migration passed."}
    if not isinstance(facts, dict) or not isinstance(facts.get("evidence", []), list):
        raise ValueError("migration must be an object with an evidence list")
    if set(facts) - {"tool", "version", "evidence"}:
        raise ValueError("migration accepts tool, version and evidence only; never connection values")
    tool, version = facts.get("tool"), facts.get("version")
    paths = [relative_path(p) for p in facts.get("evidence", [])]
    if tool not in {"prisma", "alembic", "flyway", "liquibase", "sql"} or not paths:
        return {"status": "NEEDS_REVIEW", "reason": "Confirm the existing migration configuration and history."}
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?", version):
        return {"status": "NEEDS_VERSION", "tool": tool, "evidence": paths}
    major = int(version.split(".")[0])
    if tool == "prisma" and major not in range(2, 9):
        return {"status": "NEEDS_REVIEW", "tool": tool, "version": version, "evidence": paths}
    commands = {
        "alembic": [["alembic", "heads"], ["alembic", "upgrade", "head"]],
        "flyway": [["flyway", "validate"], ["flyway", "migrate"]],
        "liquibase": [["liquibase", "validate"], ["liquibase", "update-sql"], ["liquibase", "update"]],
    }
    if tool == "prisma":
        commands[tool] = ([["prisma", "migrate", "status"], ["prisma", "migrate", "deploy"]] if major <= 7 else
                          [["prisma", "migration", "check"], ["prisma", "db", "migrate", "--show"], ["prisma", "db", "migrate"]])
    return {"status": "REVIEW_EXISTING_COMMAND" if tool in commands else "CUSTOM_RUNNER_REVIEW_REQUIRED",
            "tool": tool, "version": version, "evidence": paths, "command_examples": commands.get(tool, []),
            "note": "Use the repository-pinned binary and existing scripts; examples are not executed or approved."}


def recommend(facts):
    allowed = {"engine", "evidence", "stack", "access", "max_replicas", "requested_mode", "target_engine",
               "connection_secret_ref", "migration"}
    if not isinstance(facts, dict) or set(facts) - allowed:
        raise ValueError("unknown input fields; pass connection_secret_ref, never a URL, password or token")
    engine = facts.get("engine", "unknown")
    mode = facts.get("requested_mode", "preserve")
    target = facts.get("target_engine", engine)
    access = facts.get("access", "unknown")
    maximum = facts.get("max_replicas", 1)
    if (not all(isinstance(value, str) for value in (engine, target, mode)) or
            engine not in ENGINES or target not in ENGINES or mode not in MODES):
        raise ValueError("unsupported engine or requested_mode")
    if (not isinstance(access, str) or access not in {"read-only", "read-write", "unknown"} or
            type(maximum) is not int or not 1 <= maximum <= 30):
        raise ValueError("access or max_replicas is invalid")
    if mode == "container-postgres" and "target_engine" in facts and target != "postgresql":
        raise ValueError("container-postgres conflicts with the explicit target_engine")
    if mode == "local-sqlite" and "target_engine" in facts and target != "sqlite":
        raise ValueError("local-sqlite conflicts with the explicit target_engine")
    evidence = facts.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 30:
        raise ValueError("evidence must be a bounded list")
    normalized = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"path", "kind", "engine"}:
            raise ValueError("each evidence item requires path, kind and engine")
        if (not isinstance(item["kind"], str) or not isinstance(item["engine"], str) or
                item["kind"] not in EVIDENCE or item["engine"] not in ENGINES - {"unknown"}):
            raise ValueError("unsupported DB evidence kind or engine")
        normalized.append({**item, "path": relative_path(item["path"])})
    ref = facts.get("connection_secret_ref")
    if ref is not None:
        if not isinstance(ref, dict) or set(ref) != {"name", "key"}:
            raise ValueError("connection_secret_ref requires name and key only")
        if (not isinstance(ref["name"], str) or len(ref["name"]) > 253 or
                not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in ref["name"].split("."))):
            raise ValueError("invalid secret reference name")
        if not isinstance(ref["key"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", ref["key"]):
            raise ValueError("invalid secret reference key")
    result = {"decision": "PLAN", "deployment_approved": False, "current_engine": engine,
              "selected_engine": engine, "requested_mode": mode, "evidence": normalized,
              "migration": migration_tool(facts.get("migration")), "reasons": [], "options": []}
    def block(code, reason):
        result.update(decision=code, reasons=[reason])
        return result
    if engine == "unknown" or not normalized:
        return block("NEEDS_EVIDENCE", "Framework names do not identify a database. Provide observed datasource/connection evidence.")
    if {e["engine"] for e in normalized} != {engine}:
        return block("NEEDS_EVIDENCE", "Conflicting engines: review each database separately; do not silently consolidate them.")
    if mode == "preserve" and target != engine:
        return block("BLOCKED", "Preserve mode cannot change the engine; request an explicit migration plan.")
    if engine == "none":
        if mode != "preserve":
            return block("NEEDS_EVIDENCE", "A new database needs an explicit app connection/schema contract before provisioning.")
        result.update(selection="no-database", support="NO_DB_RESOURCE_REQUIRED")
        return result
    if engine == "sqlite":
        result["options"] = ["local-sqlite-single-replica", "container-postgres-with-reviewed-migration", "managed-with-reviewed-migration"]
        if maximum >= 2 and mode in {"preserve", "local-sqlite"}:
            return block("BLOCKED", "SQLITE_HORIZONTAL_UNSUPPORTED: the runtime SQLite profile is limited to one replica, including autoscaling maximum.")
        if mode in {"preserve", "local-sqlite"}:
            result.update(selection="local-sqlite", support="PERSISTENT_SQLITE_RENDERER_NOT_IMPLEMENTED")
            result["reasons"] = ["Preserve SQLite; require durable volume, backup and single-writer restart strategy before runtime writes."]
            if access == "read-only":
                result["reasons"] = ["An immutable image dataset may stay SQLite; verify read-only behavior. No shared mutable file or scale-out exception is assumed."]
            return result
    elif mode == "local-sqlite":
        return block("BLOCKED", "An existing server database is preserved; an engine conversion to local SQLite is not supported.")
    if mode == "container-postgres":
        target = "postgresql"
        result.update(selection="container-postgres", selected_engine=target, support="TEAM_DATABASE_RUNTIME_NOT_CONFIGURED")
    elif mode == "managed":
        if target in {"sqlite", "none", "unknown"}:
            return block("NEEDS_EVIDENCE", "Select an explicit relational target engine for managed connectivity.")
        if ref is None:
            return block("BLOCKED_ENV", "Managed/external connectivity accepts only an existing approved connection_secret_ref.")
        result.update(selection="managed-connection", selected_engine=target, connection_secret_ref=ref,
                      support="MANAGED_CONNECTION_RENDERER_NOT_IMPLEMENTED")
    else:
        result.update(selection="preserve-existing-connection", support="VERIFY_EXISTING_CONNECTION_BINDING")
        result["options"] = (["preserve-existing-connection", "container-postgres", "managed-postgresql"] if engine == "postgresql"
                             else ["preserve-existing-connection", f"managed-{engine}"])
    if result["selected_engine"] != engine:
        result.update(decision="MIGRATION_REQUIRED", automatic_engine_change=False)
        result["reasons"] = ["Explicit target proposal only: review schema/data conversion, backup, write freeze, validation and cutover before changing the engine."]
    else:
        result["reasons"] = ["Preserve the current engine and repository migration tool. Moving hosts still requires a data transfer/cutover plan."]
    return result



def scan_workspace(root, *, max_files=5000, max_file_bytes=2_000_000):
    """Bounded source inspection only. Return paths/hashes/signal kinds, never connection values.

    Absence of a static signal is UNKNOWN, not no-database. No source import, database
    open/query, environment loading, dependency installation, or migration is performed.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or type(max_files) is not int or not 1 <= max_files <= 5000:
        raise ValueError('bounded real workspace directory required')
    if type(max_file_bytes) is not int or not 16 <= max_file_bytes <= 2_000_000:
        raise ValueError('invalid source scan byte limit')
    root = root.resolve()
    evidence, files, orm, issues = [], {}, [], []
    excluded = {'node_modules', 'vendor', 'venv', '__pycache__', 'build', 'dist', 'target', 'tests', 'test', 'docs', 'examples'}
    suffixes = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.prisma', '.properties', '.yml', '.yaml', '.json', '.toml', '.ini', '.db', '.sqlite', '.sqlite3'}
    engines = {'postgres': 'postgresql', 'postgresql': 'postgresql', 'mysql': 'mysql', 'mariadb': 'mariadb',
               'sqlite': 'sqlite', 'mssql': 'sqlserver', 'sqlserver': 'sqlserver', 'oracle': 'oracle'}
    def engine_url(value):
        if not isinstance(value, str):
            return None
        found = re.match(r'^(?:jdbc:)?([a-z0-9]+)(?:\+[a-z0-9_]+)?:', value, re.I)
        return engines.get(found[1].lower()) if found else None
    def add(path, kind, engine, data):
        item = {'path': path, 'kind': kind, 'engine': engine}
        if item not in evidence:
            evidence.append(item)
        files[path] = {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    count = 0
    for directory, dirs, names, dirfd in os.fwalk(root, follow_symlinks=False):
        dirs.sort(); names.sort()
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                issues.append({'code': 'SYMLINK_REJECTED', 'path': path.relative_to(root).as_posix()})
                dirs.remove(name)
            elif name.startswith('.') or name in excluded:
                dirs.remove(name)
        for name in names:
            path = (Path(directory) / name).relative_to(root).as_posix()
            try:
                relative_path(path)
            except ValueError:
                issues.append({'code': 'UNSAFE_PATH', 'path': path}); continue
            if name.startswith('.env') or name.startswith('id_') or name in {'auth.json', 'credentials', '.netrc'} or Path(name).suffix in {'.pem', '.key', '.p12', '.pfx'}:
                continue # Never read or hash credential files.
            if Path(name).suffix.lower() not in suffixes:
                continue
            count += 1
            if count > max_files:
                issues.append({'code': 'FILE_LIMIT', 'path': path}); break
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
                with os.fdopen(fd, 'rb') as stream:
                    details = os.fstat(stream.fileno())
                    if not stat.S_ISREG(details.st_mode):
                        issues.append({'code': 'NONREGULAR_REJECTED', 'path': path}); continue
                    if details.st_size > max_file_bytes:
                        issues.append({'code': 'FILE_TOO_LARGE', 'path': path}); continue
                    data = stream.read(max_file_bytes + 1)
                if len(data) > max_file_bytes:
                    issues.append({'code': 'FILE_TOO_LARGE', 'path': path}); continue
            except OSError:
                issues.append({'code': 'UNREADABLE_OR_SYMLINK', 'path': path}); continue
            if data.startswith(b'SQLite format 3\x00'):
                add(path, 'sqlite-file', 'sqlite', data); continue
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                continue
            signals = set()
            if name == 'package.json':
                try:
                    package = json.loads(text)
                    for group in ('dependencies', 'devDependencies'):
                        for dependency in ('prisma', '@prisma/client', 'typeorm', 'sequelize', 'knex'):
                            if isinstance(package.get(group), dict) and dependency in package[group]:
                                signals.add(dependency)
                except (ValueError, AttributeError):
                    issues.append({'code': 'MALFORMED_MANIFEST', 'path': path})
            if name == 'schema.prisma':
                signals.add('prisma')
                active = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
                active = re.sub(r'(?m)^\s*//.*$', '', active)
                for block in re.findall(r'\bdatasource\s+\w+\s*\{([^}]*)\}', active, re.S):
                    provider = re.search(r'\bprovider\s*=\s*[\"\']([a-z]+)[\"\']', block)
                    if provider and provider[1] in engines:
                        add(path, 'datasource', engines[provider[1]], data)
            if name == 'alembic.ini':
                signals.add('alembic')
            if Path(name).suffix == '.py':
                try:
                    tree = ast.parse(text)
                    sqlite_modules, sqlite_calls, sa_modules, sa_calls = set(), set(), set(), set()
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                if alias.name == 'sqlite3': sqlite_modules.add(alias.asname or alias.name)
                                if alias.name == 'sqlalchemy': sa_modules.add(alias.asname or alias.name); signals.add('sqlalchemy')
                        if isinstance(node, ast.ImportFrom):
                            if node.module == 'sqlite3': sqlite_calls.update(x.asname or x.name for x in node.names if x.name == 'connect')
                            if node.module == 'sqlalchemy':
                                sa_calls.update(x.asname or x.name for x in node.names if x.name == 'create_engine'); signals.add('sqlalchemy')
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                            if any(isinstance(t, ast.Name) and t.id in {'DATABASE_URL', 'SQLALCHEMY_DATABASE_URI'} for t in node.targets):
                                engine = engine_url(node.value.value)
                                if engine: add(path, 'connection-config', engine, data)
                        if isinstance(node, ast.Call):
                            fn = node.func
                            if ((isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in sqlite_modules and fn.attr == 'connect')
                                    or (isinstance(fn, ast.Name) and fn.id in sqlite_calls)):
                                add(path, 'sqlite-open', 'sqlite', data)
                            sqlalchemy = ((isinstance(fn, ast.Name) and fn.id in sa_calls)
                                          or (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in sa_modules and fn.attr == 'create_engine'))
                            if sqlalchemy and node.args and isinstance(node.args[0], ast.Constant):
                                engine = engine_url(node.args[0].value)
                                if engine: add(path, 'connection-config', engine, data)
                        if isinstance(node, ast.Dict):
                            for key, value in zip(node.keys, node.values):
                                if isinstance(key, ast.Constant) and key.value == 'ENGINE' and isinstance(value, ast.Constant) and isinstance(value.value, str):
                                    match = re.fullmatch(r'django\.db\.backends\.(sqlite3|postgresql|mysql|oracle)', value.value)
                                    if match:
                                        signals.add('django'); add(path, 'connection-config', 'sqlite' if match[1] == 'sqlite3' else match[1], data)
                except SyntaxError:
                    issues.append({'code': 'SOURCE_PARSE_FAILED', 'path': path})
            # Explicit configuration keys only; libraries/framework names alone never select an engine.
            for line in (text.splitlines() if Path(name).suffix in {'.properties', '.yaml', '.yml', '.ini', '.toml'} else []):
                if line.lstrip().startswith(('#', '//', '*')):
                    continue
                match = re.search(r'(?:DATABASE_URL|SQLALCHEMY_DATABASE_URI|database_url|spring\.datasource\.url|sqlalchemy\.url|\burl)\s*[\"\']?\s*[:=]\s*[\"\']?((?:jdbc:)?[a-z0-9]+(?:\+[a-z0-9_]+)?:)', line, re.I)
                engine = engine_url(match[1]) if match else None
                if engine: add(path, 'connection-config', engine, data)
            for framework in sorted(signals):
                files[path] = {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                orm.append({'path': path, 'framework': framework, 'sha256': files[path]['sha256']})
        if count > max_files:
            break
    if len(evidence) > 30:
        issues.append({'code': 'EVIDENCE_LIMIT', 'path': '.'}); evidence = evidence[:30]
    found = {item['engine'] for item in evidence}
    facts = {'engine': next(iter(found)) if len(found) == 1 and not issues else 'unknown',
             'evidence': evidence, 'access': 'unknown', 'requested_mode': 'preserve'}
    return {'schema_version': 'v1', 'scan_status': 'PARTIAL' if issues else 'COMPLETE',
            'facts': facts, 'evidence_files': sorted(files.values(), key=lambda x: x['path']),
            'orm_configs': orm, 'issues': issues, 'review': recommend(facts),
            'runtime_verified': False, 'deployment_approved': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    try:
        result = recommend(json.loads(args.evidence.read_text()))
    except (ValueError, OSError, TypeError) as exc:
        # Never echo malformed input, connection values, or a file's raw contents.
        print(json.dumps({"decision": "INVALID_INPUT", "reason": str(exc) if type(exc) is ValueError else "Unable to read or validate input"}))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["decision"] in {"PLAN", "MIGRATION_REQUIRED"} else 2


if __name__ == "__main__":
    sys.exit(main())
