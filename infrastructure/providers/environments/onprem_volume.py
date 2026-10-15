"""Initialize only a newly-created, Cinder-attached blank PostgreSQL data disk."""
import json
import re
import shlex
import subprocess
from pathlib import Path
from .onprem import require
from infrastructure.providers.environments.control import digest, save


def prepare(home,value,node):
    disk=node['data_disk'];device=disk['device']
    require(re.fullmatch(r'/dev/(?:xvd|vd|sd)[b-z]',device),'ONPREM_DATABASE_DEVICE_UNPROVEN')
    ssh=node['ssh'];prefix=['ssh','-F','/dev/null','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
        '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+ssh['known_hosts_file'],
        '-o','HostKeyAlias='+node['private_ipv4'],'-o','ConnectTimeout=10','-i',ssh['identity_file'],
        ssh['user']+'@'+ssh['connect_host']]
    def call(script):
        result=subprocess.run([*prefix,'sudo -n /usr/bin/python3 -'],input=script.encode(),capture_output=True,timeout=180)
        require(result.returncode==0,'ONPREM_DATABASE_DISK_GUEST_FAILED')
        try:return json.loads(result.stdout)
        except (ValueError,UnicodeError):require(False,'ONPREM_DATABASE_DISK_GUEST_INVALID')
    # Device is sourced from the authenticated attach response and the guest SSH
    # host key is pinned. No caller text becomes a shell command.
    common="""import json,os,subprocess
from pathlib import Path
def run(*argv):return subprocess.check_output(argv,text=True).strip()
"""+f'device={device!r}\nmount="/var/lib/postgresql"\n'
    probe=common+"""rows=json.loads(run('lsblk','--json','--paths','--output','NAME,TYPE,FSTYPE,MOUNTPOINT,UUID',device))['blockdevices']
assert len(rows)==1 and rows[0]['name']==device and rows[0]['type']=='disk'
found=subprocess.run(['findmnt','--json','--mountpoint',mount],capture_output=True,text=True)
print(json.dumps({'disk':rows[0],'mount':json.loads(found.stdout)['filesystems'] if found.returncode==0 else []}))
"""
    observed=call(probe);receipt=value.get('database_filesystem')
    if receipt:
        require(receipt['resource_id']==disk['resource_id'] and receipt['device']==device and observed['disk']['uuid']==receipt['uuid']
            and len(observed['mount'])==1 and observed['mount'][0]['source']==device,'ONPREM_DATABASE_MOUNT_CHANGED')
        return
    require('database-format' not in value['steps'],'ONPREM_DATABASE_DISK_RECONCILIATION_REQUIRED')
    require(not observed['mount'] and not observed['disk'].get('fstype') and not observed['disk'].get('children')
        and not observed['disk'].get('mountpoint'),'ONPREM_DATABASE_DISK_NOT_BLANK')
    value['steps']['database-format']={'status':'unknown','fingerprint':digest(disk)};save(home/'state.json',value)
    script=common+"""rows=json.loads(run('lsblk','--json','--paths','--output','NAME,TYPE,FSTYPE,MOUNTPOINT,UUID',device))['blockdevices']
assert len(rows)==1 and rows[0]['name']==device and rows[0]['type']=='disk' and not rows[0].get('fstype') and not rows[0].get('children') and not rows[0].get('mountpoint')
# wipefs --no-act inspects signatures without erasing anything.
assert not run('wipefs','--no-act','--noheadings','--output','TYPE',device)
path=Path(mount)
assert not path.exists() or path.is_dir() and not any(path.iterdir())
subprocess.run(['mkfs.ext4','-q',device],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
uuid=run('blkid','-s','UUID','-o','value',device)
assert uuid and all(c in '0123456789abcdef-' for c in uuid)
path.mkdir(mode=0o700,parents=True,exist_ok=True)
fstab=Path('/etc/fstab');old=fstab.read_text()
assert not any(line.strip() and not line.lstrip().startswith('#') and len(line.split())>1 and line.split()[1]==mount for line in old.splitlines())
with fstab.open('a') as stream:stream.write('\\nUUID='+uuid+' '+mount+' ext4 defaults 0 2\\n');stream.flush();os.fsync(stream.fileno())
subprocess.run(['mount',mount],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
assert run('findmnt','--noheadings','--output','SOURCE','--mountpoint',mount)==device
print(json.dumps({'uuid':uuid}))
"""
    result=call(script);require(re.fullmatch(r'[a-f0-9-]{36}',result.get('uuid','')),'ONPREM_DATABASE_FILESYSTEM_INVALID')
    value['database_filesystem']={**disk,'uuid':result['uuid']};value['steps']['database-format']['status']='succeeded';save(home/'state.json',value)
    prepare(home,value,node)
