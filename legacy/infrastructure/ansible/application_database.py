#!/usr/bin/env python3
"""Fixed guest-side PostgreSQL 16 installer; invoked privately as postgres."""
import json
from contextlib import closing
import os
import re
import stat
import sys


def configure(binding, connect, sql):
    database = binding['database']
    owner, runtime = binding['migration']['username'], binding['runtime']['username']
    if (binding['version'] != 1 or binding['sslmode'] != 'verify-full' or binding['port'] != 5432
            or len({database, owner, runtime}) != 3 or any(
                not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', name) for name in (database, owner, runtime))):
        raise ValueError('invalid application database identity')
    marker = 'railshot:' + database
    def statement(template, *names):
        return sql.SQL(template).format(*(sql.Identifier(name) for name in names))
    with closing(connect(dbname='postgres', user='postgres', host='/var/run/postgresql', connect_timeout=10)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute('SHOW server_version_num')
            if int(cursor.fetchone()[0]) // 10000 != 16:
                raise ValueError('PostgreSQL 16 required')
            cursor.execute('SELECT pg_is_in_recovery()')
            if cursor.fetchone()[0]:
                raise ValueError('primary changed before application setup')
            for role, credential in ((owner, binding['migration']), (runtime, binding['runtime'])):
                cursor.execute("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s", (role,))
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(statement('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %s', role), (credential['password'],))
                    cursor.execute(statement('COMMENT ON ROLE {} IS %s', role), (marker,))
                elif existing[0] != marker:
                    raise ValueError('application role is not owned by this deployment')
                cursor.execute('SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=%s', (role,))
                if cursor.fetchone():
                    raise ValueError('unexpected application role membership')
                cursor.execute(statement('ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %s', role), (credential['password'],))
            cursor.execute("SELECT shobj_description(oid, 'pg_database'), pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (database,))
            existing = cursor.fetchone()
            if existing is None:
                cursor.execute(statement('CREATE DATABASE {} OWNER {}', database, owner))
                cursor.execute(statement('COMMENT ON DATABASE {} IS %s', database), (marker,))
            elif existing != (marker, owner):
                raise ValueError('application database is not owned by this deployment')
            cursor.execute(statement('REVOKE ALL ON DATABASE {} FROM PUBLIC', database))
            cursor.execute(statement('GRANT CONNECT ON DATABASE {} TO {}', database, runtime))
    with closing(connect(dbname=database, user='postgres', host='/var/run/postgresql', connect_timeout=10)) as connection, connection:
        with connection.cursor() as cursor:
            # ponytail: DML grants cover public; add an approved schema policy if migrations create other schemas.
            cursor.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
            cursor.execute(statement('GRANT USAGE, CREATE ON SCHEMA public TO {}', owner))
            cursor.execute(statement('GRANT USAGE ON SCHEMA public TO {}', runtime))
            cursor.execute(statement('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}', runtime))
            cursor.execute(statement('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}', runtime))
            cursor.execute(statement('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}', owner, runtime))
            cursor.execute(statement('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}', owner, runtime))
            cursor.execute(statement('ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC', owner))
    for credential in (binding['migration'], binding['runtime']):
        with closing(connect(dbname=database, user=credential['username'], password=credential['password'],
                host=binding['host'], port=5432, sslmode='verify-full',
                sslrootcert='/etc/patroni/tls/postgres-ca.crt', connect_timeout=10,
                options='-c statement_timeout=10000')) as connection, connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_user, current_database(), ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')
                if cursor.fetchone() != (credential['username'], database, True):
                    raise ValueError('application TLS login was not verified')
                if credential is binding['runtime']:
                    cursor.execute("SELECT has_database_privilege(current_user,current_database(),'CREATE'), has_schema_privilege(current_user,'public','CREATE')")
                    if cursor.fetchone() != (False, False):
                        raise ValueError('runtime role must not have DDL privileges')


def main():
    try:
        import psycopg2
        from psycopg2 import sql
        fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('private binding required')
            binding = json.load(stream)
        configure(binding, psycopg2.connect, sql)
    except Exception:
        # Never expose credentials, SQL, or driver diagnostics to controller stdout.
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
