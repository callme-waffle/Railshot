"""Durable intent and owned native resource observations."""
import copy
from contextlib import contextmanager
from pathlib import Path
import re
import time

from infrastructure.providers.environments.control import private_dir, read_private, save, locked, digest
from infrastructure.providers.environments.contract import ProviderError, READINESS
from .api import require

# family, create, list, rows, identifier, state field, terminal ready code
KINDS = {
 'vpc': ('vpc','createVpc','getVpcList','vpcList','vpcNo','vpcStatus','RUN'),
 'subnet': ('vpc','createSubnet','getSubnetList','subnetList','subnetNo','subnetStatus','RUN'),
 'route_table': ('vpc','createRouteTable','getRouteTableList','routeTableList','routeTableNo','routeTableStatus','RUN'),
 'acg': ('vserver','createAccessControlGroup','getAccessControlGroupList','accessControlGroupList','accessControlGroupNo','accessControlGroupStatus','RUN'),
 'init_script': ('vserver','createInitScript','getInitScriptList','initScriptList','initScriptNo',None,None),
 'server': ('vserver','createServerInstances','getServerInstanceList','serverInstanceList','serverInstanceNo','serverInstanceStatus','RUN'),
 'block_storage': ('vserver','createBlockStorageInstance','getBlockStorageInstanceList','blockStorageInstanceList','blockStorageInstanceNo','blockStorageInstanceStatus','CREAT'),
 'nas': ('vnas','createNasVolumeInstance','getNasVolumeInstanceList','nasVolumeInstanceList','nasVolumeInstanceNo','nasVolumeInstanceStatus','CREAT'),
 'target_group': ('vloadbalancer','createTargetGroup','getTargetGroupList','targetGroupList','targetGroupNo',None,None),
 'load_balancer': ('vloadbalancer','createLoadBalancerInstance','getLoadBalancerInstanceList','loadBalancerInstanceList','loadBalancerInstanceNo','loadBalancerInstanceStatus','USED'),
}

@contextmanager
def state(config, context):
    home=private_dir(str(Path(context['state_dir'])/'ncp'))
    with locked(home/'lock'):
        binding={k:context[k] for k in ('environment_id','owner_id','account_id','credential_ref','policy_revision')}
        binding.update(config=digest(config),spec=digest(context['spec']))
        value=read_private(home/'state.json') if (home/'state.json').exists() else {
            'binding':binding,'steps':{},'resources':[],'readiness':{k:False for k in READINESS},'status':'blocked'}
        require(value['binding']==binding,'NCP_STATE_BINDING_CHANGED')
        yield home,value


def once(client,home,value,key,family,action,params,*,kind=None,sensitive=False):
    fingerprint=digest({'family':family,'action':action,'params':params})
    old=value['steps'].get(key)
    if old:
        require(old['fingerprint']==fingerprint,'NCP_MUTATION_INPUT_CHANGED')
        if old['status']!='succeeded':raise ProviderError('NCP_RECONCILIATION_REQUIRED',outcome_unknown=True)
        return old.get('id')
    if action.startswith('create') and 'cost_approval' in value:
        from .budget import permit
        permit(value['cost_approval'])
    value['steps'][key]={'fingerprint':fingerprint,'status':'unknown'};save(home/'state.json',value)
    try:
        response=client.call(family,action,params,mutation=True,sensitive=sensitive)
        ident=None
        if kind:
            descriptor=KINDS[kind];rows=response.get(descriptor[3],[])
            require(len(rows)==1,'NCP_CREATE_ID_UNCERTAIN')
            ident=str(rows[0].get(descriptor[4],''));require(re.fullmatch(r'[0-9]+',ident),'NCP_CREATE_ID_UNCERTAIN')
            value['resources'].append({'kind':kind,'id':ident,'status':'created','stage':key,
                'expected':{k:v for k,v in params.items() if k in ('vpcNo','subnetNo','serverName','serverDescription','nasVolumeDescription','vpcName','subnetName','routeTableName','accessControlGroupName','initScriptName','loadBalancerName','targetGroupName','blockStorageName','blockStorageDescription','initScriptDescription','loadBalancerDescription','targetGroupDescription')}})
        value['steps'][key].update(status='succeeded',id=ident);save(home/'state.json',value)
        return ident
    except Exception:
        raise ProviderError('NCP_MUTATION_OUTCOME_UNKNOWN',outcome_unknown=True) from None


def observe(client,resource,*,absent=False):
    descriptor=KINDS[resource['kind']]
    # Use native list filters, and reject ambiguous or truncated observations.
    params={descriptor[4]+'List.1':resource['id']}
    if resource['kind']=='init_script':params={'initScriptNoList.1':resource['id']}
    result=client.call(descriptor[0],descriptor[2],params)
    rows=result.get(descriptor[3],[])
    require(isinstance(rows,list) and result.get('totalRows',len(rows))==len(rows),'NCP_RESOURCE_LIST_TRUNCATED')
    matches=[r for r in rows if str(r.get(descriptor[4]))==resource['id']]
    require(len(matches)<=1,'NCP_RESOURCE_AMBIGUOUS')
    require(len(rows)==len(matches),'NCP_RESOURCE_FILTER_IGNORED')
    if absent:return not matches
    require(len(matches)==1,'NCP_RESOURCE_MISSING')
    row=matches[0]
    for field,expected in resource.get('expected',{}).items():
        require(str(row.get(field))==str(expected),'NCP_RESOURCE_OWNERSHIP_CHANGED')
    require(row.get('regionCode','KR')=='KR','NCP_RESOURCE_REGION_CHANGED')
    return row


def wait(client,resource,*,code=None,absent=False,seconds=900):
    desc=KINDS[resource['kind']];deadline=time.monotonic()+seconds
    while True:
        row=observe(client,resource,absent=absent)
        if absent and row:return True
        operation={'server':'serverInstanceOperation','block_storage':'blockStorageInstanceOperation','nas':'nasVolumeInstanceOperation','load_balancer':'loadBalancerInstanceOperation'}.get(resource['kind'])
        stable=not operation or row.get(operation,{}).get('code')=='NULL'
        if not absent and stable and (desc[5] is None or row.get(desc[5],{}).get('code')==(code or desc[6])):return row
        require(time.monotonic()<deadline,'NCP_NATIVE_WAIT_TIMEOUT');time.sleep(3)


def resource(value,key):
    matches=[r for r in value['resources'] if r['stage']==key];require(len(matches)==1,'NCP_RESOURCE_RECEIPT_REQUIRED')
    return matches[0]
