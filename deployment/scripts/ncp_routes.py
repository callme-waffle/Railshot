"""Typed NCP route publication/lifecycle port; all native credentials stay private."""
import copy
import hashlib
from pathlib import Path
import re
import uuid

import applications
import environment as runtime
from infrastructure.providers.environments.control import digest, read_private, save, private_dir
from infrastructure.providers.ncp import edge
from infrastructure.providers.ncp.api import require


def route(request):
    return {key:request[key] for key in ('environment_id','application_id','hostname','node_port','health_path')}


def ensure(config,publication_request,prepared,request):
    require(request['provider']=='ncp' and prepared['application_id']==request['application_id'],'NCP_APPLICATION_BINDING_CHANGED')
    path=request['ingress']['ncp_route_config_file']
    native=edge.configure(path,route(request))
    directory=Path(config['state_dir'])/prepared['application_id']/'deployments'/publication_request['deployment_id']
    result={**prepared,'phase':'route_configured','public_route_state':'configured','https_verified':False,
        'edge':{'provider':'ncp',**native},'dns_record_id':native['dns_id'],
        'route_request_sha256':hashlib.sha256(runtime.read_private(directory/'route-request.json',raw=True)).hexdigest()}
    runtime.save(directory/'route-result.json',result)
    return result


def inputs(binding,action):
    require(action in ('start','stop','delete') and binding.get('provider')=='ncp' and binding.get('version')==1,'NCP_LIFECYCLE_BINDING_INVALID')
    app=binding['application_id'];require(re.fullmatch(r'app-[a-f0-9]{24}',app),'NCP_APPLICATION_ID_INVALID')
    target=binding['registered']['target'];require(target['id']==target['namespace']==app,'NCP_NAMESPACE_CHANGED')
    path=binding['ingress']['ncp_route_config_file'];cfg=edge.configuration(path)
    state=read_private(Path(cfg['state_dir'])/app/'state.json');request=state['request']
    require(request['environment_id']==binding['environment_id'] and request['application_id']==app and request['hostname']==binding['hostname']
        and request['node_port']==target['node_port'],'NCP_ROUTE_APPLICATION_CHANGED')
    return path,request


def plan(binding,action):
    path,request=inputs(binding,action);state=edge.snapshot(path,request)
    require(state['status']!='deleted' or action=='delete','NCP_ROUTE_DELETED')
    cfg=edge.configuration(path);home=private_dir(str(Path(cfg['state_dir'])/'lifecycle'/uuid.uuid4().hex))
    result={'version':1,'application_id':binding['application_id'],'provider':'ncp','action':action,
        'resources':[{'kind':item['kind'],'name':binding['hostname']} for item in state['resources']],
        'retained':[{'kind':'shared_runtime','name':binding['environment_id']},{'kind':'certificate','name':cfg['certificate_no']}],
        'private':{'binding_sha256':digest(binding),'config_sha256':digest(cfg),'state_sha256':digest(state),
            'config_path':path,'request':request,'health_path':request['health_path'],'work':str(home)}}
    result['plan_sha256']=digest(result);save(home/'review.json',result)
    save(home/'intent.json',{'phase':'planned','plan_sha256':result['plan_sha256']})
    return result


def checked(binding,action,expected):
    value=copy.deepcopy(expected);signature=value.pop('plan_sha256')
    require(digest(value)==signature and expected['action']==action and expected['private']['binding_sha256']==digest(binding),'NCP_PLAN_CHANGED')
    path,request=inputs(binding,action);private=expected['private'];home=Path(private['work'])
    require(path==private['config_path'] and request==private['request'] and digest(edge.configuration(path))==private['config_sha256'],'NCP_PLAN_AUTHORITY_CHANGED')
    require(read_private(home/'review.json')==expected,'NCP_SAVED_PLAN_CHANGED')
    intent=read_private(home/'intent.json');require(intent['plan_sha256']==signature,'NCP_PLAN_INTENT_CHANGED')
    return path,request,home,intent


def validate(binding,action,expected_plan):
    path,request,home,intent=checked(binding,action,expected_plan)
    require(intent['phase']=='planned' and digest(edge.snapshot(path,request))==expected_plan['private']['state_sha256'],'NCP_ROUTE_CHANGED_AFTER_PLAN')
    return expected_plan


def execute(binding,action,expected_plan):
    path,request,home,intent=checked(binding,action,expected_plan)
    require(intent['phase']=='planned','NCP_LIFECYCLE_RECONCILIATION_REQUIRED')
    validate(binding,action,expected_plan)
    save(home/'intent.json',{'phase':'unknown','plan_sha256':expected_plan['plan_sha256']})
    if action=='delete':edge.revoke(path,request,expected_state=expected_plan['private']['state_sha256'])
    else:edge.power(path,request,action,expected_state=expected_plan['private']['state_sha256'],operation_id=home.name)
    value=edge.snapshot(path,request)
    require(value['status']=={'start':'configured','stop':'stopped','delete':'deleted'}[action],'NCP_LIFECYCLE_READBACK_FAILED')
    save(home/'intent.json',{'phase':'succeeded','plan_sha256':expected_plan['plan_sha256'],'result_sha256':digest(value)})
    return {'status':'succeeded'}


def observe(binding,action,expected_plan):
    path,request,home,intent=checked(binding,action,expected_plan)
    value=edge.snapshot(path,request)
    if intent['phase']=='succeeded' and digest(value)==intent['result_sha256']:return 'succeeded'
    require(intent['phase']=='planned' and digest(value)==expected_plan['private']['state_sha256'],'NCP_LIFECYCLE_RECONCILIATION_REQUIRED')
    return 'not_started'


def teardown(config,environment_id):
    profile=config['environments'][environment_id];require(profile['provider']=='ncp','NCP_ENVIRONMENT_CHANGED')
    path=profile['ingress']['ncp_route_config_file'];cfg=edge.configuration(path)
    require(cfg['cluster_id']==environment_id,'NCP_ENVIRONMENT_CHANGED')
    for state_file in Path(cfg['state_dir']).glob('*/state.json'):
        value=read_private(state_file);request=value.get('request',{})
        require(request.get('environment_id')==environment_id,'NCP_ROUTE_OWNER_CHANGED')
        if value.get('status')=='deleted':edge.snapshot(path,request);continue
        require(request.get('application_id')=='bootstrap-'+digest(environment_id)[:20],'NCP_APPLICATION_ROUTES_REMAIN')
        edge.revoke(path,request)
    return {'status':'succeeded','environment_id':environment_id,'data_policy':'retain',
        'retained_resources':[{'kind':'certificate','id':cfg['certificate_no']}]}
