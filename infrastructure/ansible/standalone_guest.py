"""Fixed root-owned Ubuntu 24.04 PostgreSQL 16 operations. Never emit secrets."""
import grp
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import time

MOUNT = Path('/var/lib/postgresql')
DATA = MOUNT / '16/main'
MARKER = MOUNT / '.railshot-standalone.json'
TLS = Path('/etc/railshot-postgres/tls')
CONFIG = Path('/etc/postgresql/16/main')


def command(argv, *, input=None, timeout=1200):
    result = subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            check=True, timeout=timeout, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'DEBIAN_FRONTEND': 'noninteractive'})
    return result.stdout.strip()


def sql(statement):
    return command(['runuser', '-u', 'postgres', '--', 'psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-d', 'postgres'], input=statement)


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def write(path, content, *, postgres=False, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('symlink refused')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content); stream.flush(); os.fsync(stream.fileno())
    path.chmod(mode)
    if postgres: os.chown(path, pwd.getpwnam('postgres').pw_uid, grp.getgrnam('postgres').gr_gid)


def mount_identity(config):
    mount = json.loads(command(['findmnt', '--json', '--mountpoint', str(MOUNT), '--output', 'TARGET,SOURCE,FSTYPE,UUID']))['filesystems']
    if len(mount) != 1 or mount[0]['target'] != str(MOUNT) or mount[0]['fstype'] not in ('xfs', 'ext4') or not mount[0].get('uuid'): raise ValueError('dedicated mounted filesystem required')
    source = Path(mount[0]['source']).resolve()
    root = command(['findmnt', '--noheadings', '--output', 'SOURCE', '--target', '/'])
    if source == Path(root).resolve(): raise ValueError('root disk refused')
    device = config.get('data_device') or config.get('data_disk', {}).get('device')
    if config['provider'] == 'aws':
        volume = config.get('data_disk', {}).get('resource_id', '')
        if not re.fullmatch(r'vol-[0-9a-f]{8,17}', volume): raise ValueError('owned EBS identity required')
        device = '/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_' + volume.replace('-', '')
    if not device or Path(device).resolve() != source: raise ValueError('data device differs from provider inventory')
    return mount[0]['uuid']


def owned(config, *, installing=False):
    uuid = mount_identity(config)
    identity = {'version': 1, 'environment_id': config['environment_id'], 'resource_id': config['resource_id'], 'mount_uuid': uuid}
    if MARKER.exists():
        if MARKER.is_symlink() or json.loads(MARKER.read_text()) != identity: raise ValueError('foreign database refused')
    elif not installing or (DATA / 'PG_VERSION').exists(): raise ValueError('database adoption refused')
    return identity


def backup_config(config):
    repo = config['backup']
    lines = ['[global]', 'repo1-type=' + repo['type'], 'repo1-path=' + repo['prefix'].rstrip('/') + '/' + config['environment_id'], 'repo1-retention-full=' + str(repo['retention_full']), 'repo1-storage-verify-tls=y', 'start-fast=y', 'process-max=2', 'log-level-console=error', 'log-level-file=off']
    if repo['type'] == 's3':
        lines += ['repo1-s3-bucket=' + repo['bucket'], 'repo1-s3-endpoint=' + repo['endpoint'], 'repo1-s3-region=' + repo['region'], 'repo1-s3-uri-style=path', 'repo1-s3-key-type=' + ('auto' if repo['identity'] == 'instance' else 'shared')]
        if repo['identity'] == 'secret': lines += ['repo1-s3-key=' + repo['credentials']['access_key'], 'repo1-s3-key-secret=' + repo['credentials']['secret_key']]
    else:
        lines += ['repo1-gcs-bucket=' + repo['bucket'], 'repo1-gcs-key-type=' + ('auto' if repo['identity'] == 'instance' else 'service')]
        if repo['identity'] == 'service':
            write(Path('/etc/pgbackrest/gcs-key.json'), json.dumps(repo['credentials']), postgres=True)
            lines += ['repo1-gcs-key=/etc/pgbackrest/gcs-key.json']
    lines += ['', '[railshot]', 'pg1-path=' + str(DATA), 'pg1-port=5432', 'pg1-socket-path=/var/run/postgresql']
    write(Path('/etc/pgbackrest/pgbackrest.conf'), '\n'.join(lines) + '\n', postgres=True)


def backrest(*args):
    return command(['runuser', '-u', 'postgres', '--', 'pgbackrest', '--stanza=railshot', *args])


def hba_config(config):
    rows = ['local all postgres peer', 'local all all reject', 'hostssl all all ' + config['private_ipv4'] + '/32 scram-sha-256']
    rows += ['hostssl all all ' + cidr + ' scram-sha-256' for cidr in config['client_cidrs']]
    rows += ['host all all 0.0.0.0/0 reject', 'host all all ::/0 reject']
    return '\n'.join(rows) + '\n'


def install(config):
    identity = owned(config, installing=True)
    # Refuse installation into an unrelated existing PostgreSQL cluster before package scripts run.
    os_release = Path('/etc/os-release').read_text()
    if 'ID=ubuntu' not in os_release or 'VERSION_ID="24.04"' not in os_release: raise ValueError('Ubuntu 24.04 required')
    write(MARKER, json.dumps(identity))
    command(['apt-get', 'update'])
    command(['apt-get', 'install', '-y', '--no-install-recommends', 'postgresql-16', 'postgresql-client-16', 'pgbackrest', 'python3-psycopg2'])
    command(['systemctl', 'stop', 'postgresql@16-main'])
    for key, filename in (('ca_file', 'ca.crt'), ('certificate_file', 'server.crt'), ('key_file', 'server.key')):
        write(TLS / filename, config['tls'][key], postgres=True, mode=0o600 if key == 'key_file' else 0o644)
    write(CONFIG / 'conf.d/railshot.conf', "listen_addresses = '" + config['private_ipv4'] + "'\nport = 5432\nssl = on\nssl_min_protocol_version = 'TLSv1.2'\nssl_cert_file = '/etc/railshot-postgres/tls/server.crt'\nssl_key_file = '/etc/railshot-postgres/tls/server.key'\nssl_ca_file = '/etc/railshot-postgres/tls/ca.crt'\npassword_encryption = 'scram-sha-256'\nwal_level = replica\narchive_mode = on\narchive_command = 'pgbackrest --stanza=railshot archive-push %p'\n", postgres=True, mode=0o640)
    write(CONFIG / 'pg_hba.conf', hba_config(config), postgres=True, mode=0o640)
    backup_config(config)
    command(['systemctl', 'enable', 'postgresql@16-main'])
    command(['systemctl', 'start', 'postgresql@16-main'])
    sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='railshot_probe') THEN CREATE ROLE railshot_probe LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;\nALTER ROLE railshot_probe PASSWORD " + literal(config['probe_password']) + ";\nCREATE TABLE IF NOT EXISTS public.railshot_backup_proof(environment_id text PRIMARY KEY, token text NOT NULL);\nREVOKE ALL ON public.railshot_backup_proof FROM PUBLIC;\nGRANT SELECT ON public.railshot_backup_proof TO railshot_probe;\n")
    backrest('stanza-create')
    backrest('check')


def verify(config):
    owned(config)
    if (CONFIG / 'pg_hba.conf').read_text() != hba_config(config): raise ValueError('private TLS authentication policy changed')
    active = sql("SELECT current_setting('listen_addresses'), current_setting('ssl_cert_file'), current_setting('ssl_key_file');")
    if active != config['private_ipv4'] + '|/etc/railshot-postgres/tls/server.crt|/etc/railshot-postgres/tls/server.key': raise ValueError('active TLS/private listener differs')
    identity = sql('SELECT system_identifier FROM pg_control_system();')
    version, recovery, ssl, encryption = sql("SELECT current_setting('server_version_num'), pg_is_in_recovery(), current_setting('ssl'), current_setting('password_encryption');").split('|')
    if not re.fullmatch(r'[0-9]{10,30}', identity) or int(version) // 10000 != 16 or (recovery, ssl, encryption) != ('f', 'on', 'scram-sha-256'): raise ValueError('database SQL readiness not proven')
    import psycopg2
    with psycopg2.connect(dbname='postgres', user='railshot_probe', password=config['probe_password'], host=config['tls']['server_name'], hostaddr=config['private_ipv4'], port=5432, sslmode='verify-full', sslrootcert=str(TLS / 'ca.crt'), connect_timeout=10, options='-c statement_timeout=10000') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')
            if cur.fetchone() != (True,): raise ValueError('TLS session unproven')
    backrest('check')
    return identity


def backup(config):
    system_id = verify(config)
    # Every API backup carries a SQL proof to test a later restore, not just an exit code.
    proof = config['nonce']
    sql('INSERT INTO public.railshot_backup_proof VALUES (' + literal(config['environment_id']) + ',' + literal(proof) + ') ON CONFLICT (environment_id) DO UPDATE SET token=EXCLUDED.token;')
    previous = {b['label'] for b in json.loads(backrest('info', '--output=json'))[0].get('backup', [])}
    backrest('backup', '--type=full')
    info = json.loads(backrest('info', '--output=json'))
    if len(info) != 1 or info[0]['status']['code'] != 0: raise ValueError('backup repository unproven')
    created = [b for b in info[0].get('backup', []) if b['label'] not in previous and re.fullmatch(r'[0-9]{8}-[0-9]{6}F', b['label']) and b.get('error', False) is False]
    if len(created) != 1: raise ValueError('one newly completed full backup required')
    return {'backup_id': created[0]['label'], 'system_identifier': system_id, 'proof': proof}


def restore(config):
    owned(config)
    expected = config['restore']; backup_id = config['operation']['backup_id']
    if expected['backup_id'] != backup_id: raise ValueError('backup proof identity mismatch')
    current_id = verify(config)
    if current_id != expected['system_identifier']: raise ValueError('different database system refused')
    info = json.loads(backrest('info', '--output=json'))
    if len(info) != 1 or info[0]['status']['code'] != 0 or not any(b['label'] == backup_id and b.get('error', False) is False for b in info[0]['backup']): raise ValueError('backup no longer available')
    selected = next(b for b in info[0]['backup'] if b['label'] == backup_id)
    logical_size = selected.get('info', {}).get('size')
    if type(logical_size) is not int or logical_size <= 0: raise ValueError('backup logical size unavailable')
    wal_bytes = int(sql("SELECT pg_size_bytes(current_setting('max_wal_size'));"))
    free = os.statvfs(MOUNT)
    required = logical_size + max(logical_size // 4, 1024 ** 3) + wal_bytes * 2
    if free.f_bavail * free.f_frsize < required: raise ValueError('insufficient restore space; old data retained')
    if sql("SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid();") != '0':
        raise ValueError('unexpected active database clients')
    retained = MOUNT / 'retained' / config['operation_id']
    if retained.exists(): raise ValueError('restore was already dispatched; reconcile')
    # Preserve the complete old cluster, never pgBackRest --delta over live data.
    command(['systemctl', 'stop', 'postgresql@16-main'])
    if command(['systemctl', 'show', '--property=ActiveState', '--value', 'postgresql@16-main']) != 'inactive': raise ValueError('database stop unproven')
    retained.parent.mkdir(mode=0o700, exist_ok=True)
    DATA.rename(retained)
    DATA.mkdir(mode=0o700); os.chown(DATA, pwd.getpwnam('postgres').pw_uid, grp.getgrnam('postgres').gr_gid)
    backrest('restore', '--set=' + backup_id, '--type=immediate', '--target-action=promote')
    command(['systemctl', 'start', 'postgresql@16-main'])
    deadline = time.monotonic() + 120
    while True:
        try:
            recovered_id = verify(config)
            break
        except Exception:
            if time.monotonic() >= deadline: raise
            time.sleep(2)
    token = sql('SELECT token FROM public.railshot_backup_proof WHERE environment_id=' + literal(config['environment_id']) + ';')
    if recovered_id != expected['system_identifier'] or token != expected['proof']: raise ValueError('restored SQL proof mismatch')
    return str(retained)


def bind(config):
    import application_database
    import psycopg2
    from psycopg2 import sql as sql_module
    # Peer auth requires the postgres identity; root supplied a private file, not a command.
    postgres = pwd.getpwnam('postgres')
    os.setgroups([postgres.pw_gid]); os.setgid(postgres.pw_gid); os.setuid(postgres.pw_uid)
    application_database.configure(config['application_binding'], psycopg2.connect, sql_module, ca_file=str(TLS / 'ca.crt'))


def _execute(config):
    action = config['operation']['action']
    output = {'nonce': config['nonce'], 'environment_id': config['environment_id'], 'resource_id': config['resource_id'], 'action': action, 'ready': False}
    if action == 'install':
        install(config); output['backup'] = backup(config)
    elif action == 'backup': output['backup'] = backup(config)
    elif action == 'restore': output['retained_data_path'] = restore(config)
    elif action not in ('status', 'verify', 'bind'): raise ValueError('action refused')
    output['system_identifier'] = verify(config)
    stored = json.loads(backrest('info', '--output=json'))
    if len(stored) != 1 or stored[0]['status']['code'] != 0 or not any(re.fullmatch(r'[0-9]{8}-[0-9]{6}F', row['label']) and row.get('error', False) is False for row in stored[0].get('backup', [])):
        raise ValueError('durable full backup unproven')
    if action == 'bind':
        # Child drops privilege so the root parent can write its receipt afterwards.
        pid = os.fork()
        if pid == 0:
            try: bind(config)
            except Exception: os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        if status != 0: raise ValueError('application role TLS verification failed')
    output['ready'] = True
    return output


def execute(config):
    home = Path('/var/lib/railshot/standalone-postgres')
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    mutation = config['operation']['action'] not in ('status', 'verify')
    journal = home / ('operation-' + config['operation_id'] + '.json')
    if mutation:
        if journal.exists(): raise ValueError('prior dispatch requires reconciliation')
        write(journal, json.dumps({'status': 'unknown', 'fingerprint': config['fingerprint']}))
    output = _execute(config)
    if mutation:
        write(journal, json.dumps({'status': 'succeeded', 'fingerprint': config['fingerprint'], 'result': output}))
    elif config.get('pending_operation_id'):
        prior = home / ('operation-' + config['pending_operation_id'] + '.json')
        if prior.exists():
            record = json.loads(prior.read_text())
            if record['status'] == 'succeeded': output['reconciled'] = record
    return output


def main():
    try:
        if os.geteuid() != 0: return 1
        source = Path(sys.argv[1])
        if source.is_symlink() or source.stat().st_uid != 0 or source.stat().st_mode & 0o077: return 1
        config = json.loads(source.read_text())
        lockpath = Path('/run/railshot-standalone-postgres.lock')
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write(Path(sys.argv[2]), json.dumps(execute(config)))
        finally: os.close(fd)
        return 0
    except Exception: return 1


if __name__ == '__main__': raise SystemExit(main())
