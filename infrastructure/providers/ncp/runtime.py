"""NCP native receipts into the existing kubeadm and NFS CSI executors."""
import copy
import ipaddress
from pathlib import Path
import re

from infrastructure.providers.environments.control import read_private
from .api import Client, require


def node(config,member):
    descriptor=read_private(member['descriptor_file'])
    require(descriptor.get('provider')=='ncp' and descriptor.get('member_no')==config['account_id']
        and descriptor.get('environment_id')==config['cluster_id'] and descriptor.get('region')==config['region']=='KR'
        and descriptor.get('availability_zone')==member['availability_zone'] in ('KR-1','KR-2'), 'NCP_NODE_SCOPE_INVALID')
    require(re.fullmatch(r'[0-9]+',descriptor['resource_id']) and descriptor['ssh']==member['ssh'],'NCP_NODE_IDENTITY_CHANGED')
    require(ipaddress.IPv4Address(descriptor['private_ipv4']).is_private and ipaddress.IPv4Address(descriptor['public_ipv4']).is_global
        and member['ssh']['connect_host']==descriptor['public_ipv4'],'NCP_NODE_ADDRESS_INVALID')
    return {'id':member['id'],'resource_id':descriptor['resource_id'],'private_ipv4':descriptor['private_ipv4'],
        'availability_zone':member['availability_zone'],'ssh':copy.deepcopy(member['ssh'])}


def validate_storage(request):
    storage=request['runtime']['storage'];target=request['target'];cfg=storage.get('ncp',{})
    require(set(storage)=={'backend','mode','binding','credentials_file','ncp'} and storage['backend']=='nfs' and storage['mode']=='reuse', 'NCP_STORAGE_CONFIG_INVALID')
    require(storage['binding']=={'cluster_id':target['id'],'provider':'ncp','region':'KR','account_id':target['account_id']},'NCP_STORAGE_BINDING_CHANGED')
    require(set(cfg)=={'nas_volume_instance_no','server','share','zone','worker_instance_nos','owner_marker'},'NCP_NAS_CONFIG_INVALID')
    require(re.fullmatch(r'[0-9]+',cfg['nas_volume_instance_no']) and re.fullmatch(r'/[A-Za-z0-9_-]+',cfg['share']),'NCP_NAS_REFERENCE_INVALID')
    address=ipaddress.IPv4Address(cfg['server']);require(not address.is_global and not address.is_loopback and not address.is_unspecified,'NCP_NAS_ADDRESS_INVALID')
    workers=request['inventory']['workers'];require(len(workers)==2 and sorted(cfg['worker_instance_nos'])==sorted(n['resource_id'] for n in workers)
        and all(n['availability_zone']==cfg['zone'] for n in workers),'NCP_NAS_ZONE_OR_WORKER_CHANGED')
    return request


def prepare_storage(request,**kwargs):
    validate_storage(request);storage=request['runtime']['storage'];cfg=storage['ncp'];client=Client(storage['credentials_file'])
    require(client.credentials['member_no']==request['target']['account_id'],'NCP_NAS_ACCOUNT_CHANGED')
    result=client.call('vnas','getNasVolumeInstanceDetail',{'nasVolumeInstanceNo':cfg['nas_volume_instance_no']})
    rows=result.get('nasVolumeInstanceList',[]);require(len(rows)==1,'NCP_NAS_MISSING')
    row=rows[0]
    require(str(row['nasVolumeInstanceNo'])==cfg['nas_volume_instance_no'] and row.get('zoneCode')==cfg['zone']
        and row.get('nasVolumeDescription')==cfg['owner_marker'] and row.get('mountInformation')==cfg['server']+':'+cfg['share']
        and row.get('volumeAllotmentProtocolType',{}).get('code')=='NFS'
        and row.get('nasVolumeInstanceStatus',{}).get('code')=='CREAT'
        and sorted(row.get('nasVolumeServerInstanceNoList',[]))==sorted(cfg['worker_instance_nos']), 'NCP_NAS_NATIVE_BINDING_CHANGED')
    safe={k:v for k,v in storage.items() if k!='credentials_file'}
    safe['workers']=[n['id'] for n in request['inventory']['workers']]
    safe['backend_descriptor']={**storage['binding'],'backend':'nfs','server':cfg['server'],'share':cfg['share'],
        'nas_volume_instance_no':cfg['nas_volume_instance_no']}
    return {'storage_backend_ready':True,'storage_pvc_ready':False,'variables':{'railshot_storage':safe}}
