"""Owned NCP Application Load Balancer/TLS and Global DNS routes.

Each app owns a separate LB/TG, allowing independent stop/delete on a shared
cluster. TLS certificate is pre-enrolled and retained. No app HTTP is inferred.
"""
import copy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re
import time

from infrastructure.providers.environments.control import read_private, save, private_dir, locked, digest
from .api import Client, require
from .state import once, observe, wait, resource


def configuration(path):
    cfg=read_private(path)
    required={'version','cluster_id','provider','member_no','credentials_file','access_key_sha256','region','zone','vpc_no',
        'lb_subnet_no','worker_instance_nos','certificate_no','base_domain','dns_config_file','state_dir','cost_approval'}
    require(set(cfg)==required and cfg['version']==1 and cfg['provider']=='ncp' and cfg['region']=='KR','NCP_EDGE_CONFIG_INVALID')
    require(all(re.fullmatch(r'[0-9]+',str(cfg[k])) for k in ('member_no','vpc_no','lb_subnet_no','certificate_no')),'NCP_EDGE_NATIVE_ID_INVALID')
    require(len(cfg['worker_instance_nos'])==2 and len(set(cfg['worker_instance_nos']))==2,'NCP_EDGE_WORKERS_INVALID')
    return cfg


def client_for(cfg,*,valid_certificate=False):
    client=Client(cfg['credentials_file']);require(client.fingerprint==cfg['access_key_sha256'],'NCP_EDGE_CREDENTIAL_CHANGED')
    cert=client.account(cfg['certificate_no'],cfg['member_no'])
    require(cert.get('domainAddress')=='*.'+cfg['base_domain'],'NCP_EDGE_CERTIFICATE_CHANGED')
    if valid_certificate:
        require(not cert.get('revokeDate'),'NCP_CERTIFICATE_REVOKED')
        try:
            dates=[datetime.strptime(cert[field],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=9))) for field in ('validStartDate','validEndDate')]
        except (ValueError,TypeError,KeyError):require(False,'NCP_CERTIFICATE_VALIDITY_UNPROVEN')
        require(dates[0]<=datetime.now(timezone.utc)<dates[1],'NCP_CERTIFICATE_OUTSIDE_VALIDITY')
    return client


def validate_cluster(request):
    cfg=configuration(request['runtime']['edge']);target=request['target']
    require((cfg['cluster_id'],cfg['member_no'],cfg['region'])==(target['id'],target['account_id'],target['placement']),'NCP_EDGE_CLUSTER_CHANGED')
    require(sorted(cfg['worker_instance_nos'])==sorted(n['resource_id'] for n in request['inventory']['workers']),'NCP_EDGE_WORKER_CHANGED')
    return {'config':cfg,'binding_sha256':digest(cfg),'fixed_routes':[{'hostname':'bootstrap-'+digest(cfg['cluster_id'])[:12]+'.'+cfg['base_domain'],'node_port':30080,'health_path':'/health','path_prefix':'/'}]}


def _validate_request(cfg,request):
    require(request['environment_id']==cfg['cluster_id'] and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}',request['application_id']),'NCP_ROUTE_OWNER_INVALID')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}\.'+re.escape(cfg['base_domain']),request['hostname']),'NCP_ROUTE_HOST_INVALID')
    require(type(request['node_port']) is int and 30000<=request['node_port']<=32767,'NCP_NODE_PORT_INVALID')
    require(re.fullmatch(r'/[A-Za-z0-9/_-]*',request['health_path']),'NCP_HEALTH_PATH_INVALID')


def _dns(cfg,client):
    dns=read_private(cfg['dns_config_file']);require(set(dns)=={'domain_id','base_domain','credentials_file'}
        and dns['base_domain']==cfg['base_domain'] and dns['credentials_file']==cfg['credentials_file']
        and re.fullmatch(r'[0-9]+',str(dns['domain_id'])),'NCP_DNS_AUTHORITY_INVALID')
    zone=client.request('dns','/dns/v1/ncpdns/domain/'+str(dns['domain_id']))
    require(str(zone.get('id'))==str(dns['domain_id']) and zone.get('name')==cfg['base_domain'],'NCP_DNS_ZONE_CHANGED')
    return dns


def _records(client,dns,hostname):
    result=client.request('dns','/dns/v1/ncpdns/record/'+str(dns['domain_id']),{'page':0,'size':100,'searchContent':hostname})
    require(result.get('last') is True,'NCP_DNS_RESULT_TRUNCATED')
    host=hostname[:-(len(dns['base_domain'])+1)]
    return [row for row in result.get('content',[]) if row.get('host')==host]


def _rest_once(client,home,value,key,service,path,method,document=None):
    expected=digest([service,path,method,document]);old=value['steps'].get(key)
    if old:
        require(old=={'fingerprint':expected,'status':'succeeded'},'NCP_DNS_RECONCILIATION_REQUIRED');return
    value['steps'][key]={'fingerprint':expected,'status':'unknown'};save(home/'state.json',value)
    result=client.request(service,path,method=method,document=document,mutation=True)
    require(not isinstance(result,dict) or result.get('result')!='FAIL','NCP_DNS_MUTATION_REJECTED')
    value['steps'][key]['status']='succeeded';save(home/'state.json',value)


def _owned_rows(client,cfg,value,*,enabled=True):
    lb=observe(client,resource(value,'lb'));tg=observe(client,resource(value,'tg'))
    require(lb.get('vpcNo')==cfg['vpc_no'] and tg.get('vpcNo')==cfg['vpc_no'],'NCP_EDGE_VPC_CHANGED')
    listeners=client.call('vloadbalancer','getLoadBalancerListenerList',{'loadBalancerInstanceNo':lb['loadBalancerInstanceNo']}).get('loadBalancerListenerList',[])
    require(len(listeners)==1 and listeners[0].get('protocolType',{}).get('code')=='HTTPS'
        and listeners[0].get('port')==443 and str(listeners[0].get('sslCertificateNo'))==cfg['certificate_no']
        and listeners[0].get('tlsMinVersionType',{}).get('code')=='TLSV12','NCP_TLS_LISTENER_CHANGED')
    rules=client.call('vloadbalancer','getLoadBalancerRuleList',{'loadBalancerListenerNo':listeners[0]['loadBalancerListenerNo']}).get('loadBalancerRuleList',[])
    require(len(rules)==1 and len(rules[0].get('loadBalancerRuleConditionList',[]))==1 and rules[0]['loadBalancerRuleConditionList'][0].get('ruleConditionType',{}).get('code')=='DEFAULT','NCP_LISTENER_RULE_CHANGED')
    actions=rules[0].get('loadBalancerRuleActionList',[])
    require(len(actions)==1 and actions[0].get('ruleActionType',{}).get('code')=='TARGET_GROUP' and actions[0].get('targetGroupAction',{}).get('targetGroupWeightList')==[{'targetGroupNo':tg['targetGroupNo'],'weight':1}],'NCP_LISTENER_TARGET_CHANGED')
    request=value.get('request')
    if request:
        require(tg.get('targetGroupProtocolType',{}).get('code')=='HTTP' and tg.get('targetGroupPort')==request['node_port'] and tg.get('healthCheckPort')==request['node_port'] and tg.get('healthCheckUrlPath')==request['health_path'] and tg.get('healthCheckProtocolType',{}).get('code')=='HTTP','NCP_TARGET_GROUP_CHANGED')
    targets=client.call('vloadbalancer','getTargetList',{'targetGroupNo':tg['targetGroupNo']}).get('targetList',[])
    require(sorted(str(t['targetNo']) for t in targets)==(sorted(cfg['worker_instance_nos']) if enabled else []),'NCP_ROUTE_TARGET_CHANGED')
    return lb,tg,all(t.get('healthCheckStatus',{}).get('code')=='UP' for t in targets)


def configure(config_path,request,*,read_only=False):
    cfg=configuration(config_path);_validate_request(cfg,request);client=client_for(cfg,valid_certificate=not read_only)
    root=private_dir(cfg['state_dir'])
    # DNS commits are zone-wide. Serialize all Railshot routes in this account
    # and require an operator-dedicated zone; other writers need external coordination.
    with locked(root/'routes.lock'):
        home=private_dir(str(root/request['application_id']))
        binding=digest([cfg,request]);path=home/'state.json'
        value=read_private(path) if path.exists() else {'binding':binding,'steps':{},'resources':[]}
        require(value['binding']==binding,'NCP_ROUTE_BINDING_CHANGED')
        require(not read_only or value.get('status')=='configured','NCP_ROUTE_RECEIPT_REQUIRED')
        require(value.get('status') not in ('deleted','stopped'),'NCP_ROUTE_LIFECYCLE_REQUIRED')
        value['request']=request
        dns=_dns(cfg,client)
        if not read_only:
            from .budget import permit
            permit(cfg['cost_approval'])
            value['cost_approval']=cfg['cost_approval']
            if not request['application_id'].startswith('bootstrap-') and 'tg' not in value['steps']:
                allocated=[p for p in root.glob('*/state.json') if not p.parent.name.startswith('bootstrap-') and read_private(p).get('status')!='deleted']
                require(len(allocated)<cfg['cost_approval']['max_applications'],'NCP_APPROVED_APPLICATION_LIMIT')
            name='ra-'+digest([cfg['cluster_id'],request['application_id']])[:20]
            tg_params={'vpcNo':cfg['vpc_no'],'targetGroupName':name,'targetGroupDescription':request['application_id'],
                'targetTypeCode':'VSVR','targetGroupProtocolTypeCode':'HTTP','targetGroupPort':request['node_port'],
                'healthCheckProtocolTypeCode':'HTTP','healthCheckPort':request['node_port'],
                'healthCheckUrlPath':request['health_path'],'healthCheckHttpMethodTypeCode':'GET',
                'targetNoList.1':cfg['worker_instance_nos'][0],'targetNoList.2':cfg['worker_instance_nos'][1]}
            tg=once(client,home,value,'tg','vloadbalancer','createTargetGroup',tg_params,kind='target_group')
            lb=once(client,home,value,'lb','vloadbalancer','createLoadBalancerInstance',{
                'vpcNo':cfg['vpc_no'],'loadBalancerName':name,'loadBalancerDescription':request['application_id'],
                'loadBalancerTypeCode':'APPLICATION','loadBalancerNetworkTypeCode':'PUBLIC','throughputTypeCode':'SMALL',
                'subnetNoList.1':cfg['lb_subnet_no'],'loadBalancerListenerList.1.protocolTypeCode':'HTTPS',
                'loadBalancerListenerList.1.port':443,'loadBalancerListenerList.1.targetGroupNo':tg,
                'loadBalancerListenerList.1.sslCertificateNo':cfg['certificate_no'],
                'loadBalancerListenerList.1.tlsMinVersionTypeCode':'TLSV12'},kind='load_balancer')
            wait(client,resource(value,'lb'))
        lb,tg,healthy=_owned_rows(client,cfg,value)
        expected=lb['loadBalancerDomain'].rstrip('.')
        rows=_records(client,dns,request['hostname'])
        if 'dns-create' not in value['steps']:
            require(not read_only and not rows,'NCP_DNS_NAME_ALREADY_OWNED')
            host=request['hostname'][:-(len(cfg['base_domain'])+1)]
            _rest_once(client,home,value,'dns-create','dns','/dns/v1/ncpdns/record/'+str(dns['domain_id']),'POST',
                [{'host':host,'type':'CNAME','content':expected,'ttl':300,'aliasId':None,'lbId':None}])
            rows=_records(client,dns,request['hostname'])
        require(len(rows)==1 and rows[0].get('type')=='CNAME' and rows[0].get('content','').rstrip('.')==expected,'NCP_DNS_RECORD_CHANGED')
        if 'dns_id' in value:require(value['dns_id']==rows[0]['id'],'NCP_DNS_RECORD_REPLACED')
        else:value['dns_id']=rows[0]['id'];value['dns_target']=expected;save(path,value)
        if not read_only:_rest_once(client,home,value,'dns-apply','dns','/dns/v1/ncpdns/record/apply/'+str(dns['domain_id']),'PUT')
        domain=client.request('dns','/dns/v1/ncpdns/domain/'+str(dns['domain_id']))
        require(domain.get('completeYn') is True and domain.get('status')=='SUCCESS','NCP_DNS_APPLY_PENDING')
        value['status']='configured';value['request']=request;save(path,value)
        return {'edge_ready':True,'public_http_verified':False,'backend_healthy':healthy,'hostname':request['hostname'],
                'load_balancer_id':lb['loadBalancerInstanceNo'],'target_group_id':tg['targetGroupNo'],'dns_id':value['dns_id']}


def provision_cluster(request,**kwargs):
    prepared=validate_cluster(request);cfg=prepared['config']
    require(kwargs.get('expected_binding_sha256') in (None,prepared['binding_sha256']),'NCP_EDGE_CONFIG_CHANGED')
    route={'environment_id':cfg['cluster_id'],'application_id':'bootstrap-'+digest(cfg['cluster_id'])[:20],
        'hostname':'bootstrap-'+digest(cfg['cluster_id'])[:12]+'.'+cfg['base_domain'],'node_port':30080,'health_path':'/health'}
    return configure(request['runtime']['edge'],route)


def revoke(config_path,request,*,expected_state=None):
    cfg=configuration(config_path);_validate_request(cfg,request);client=client_for(cfg);root=private_dir(cfg['state_dir'])
    with locked(root/'routes.lock'):
        home=root/request['application_id'];value=read_private(home/'state.json')
        require(value['binding']==digest([cfg,request]),'NCP_ROUTE_BINDING_CHANGED');dns=_dns(cfg,client)
        require(expected_state is None or digest(value)==expected_state,'NCP_ROUTE_CHANGED_AFTER_PLAN')
        if value.get('status')!='deleted':_owned_rows(client,cfg,value,enabled=value.get('status')!='stopped')
        rows=_records(client,dns,request['hostname'])
        if rows:
            require(len(rows)==1 and rows[0]['id']==value.get('dns_id') and rows[0].get('type')=='CNAME' and rows[0].get('content','').rstrip('.')==value.get('dns_target'),'NCP_DNS_RECORD_REPLACED')
            _rest_once(client,home,value,'dns-delete','dns','/dns/v1/ncpdns/record/'+str(dns['domain_id']),'DELETE',[value['dns_id']])
        _rest_once(client,home,value,'dns-delete-apply','dns','/dns/v1/ncpdns/record/apply/'+str(dns['domain_id']),'PUT')
        require(not _records(client,dns,request['hostname']),'NCP_DNS_DELETE_PENDING')
        for stage,action,key in [('lb','deleteLoadBalancerInstances','loadBalancerInstanceNoList.1'),('tg','deleteTargetGroups','targetGroupNoList.1')]:
            item=resource(value,stage)
            if not observe(client,item,absent=True):
                observe(client,item)
                once(client,home,value,'delete-'+stage,'vloadbalancer',action,{key:item['id']})
                wait(client,item,absent=True)
            item['status']='deleted';save(home/'state.json',value)
        value['status']='deleted';save(home/'state.json',value)
        return {'status':'succeeded'}


def snapshot(config_path,request):
    cfg=configuration(config_path);_validate_request(cfg,request)
    value=read_private(Path(cfg['state_dir'])/request['application_id']/'state.json')
    require(value['binding']==digest([cfg,request]),'NCP_ROUTE_BINDING_CHANGED')
    require(all(step['status']=='succeeded' for step in value['steps'].values()),'NCP_ROUTE_RECONCILIATION_REQUIRED')
    client=client_for(cfg);dns=_dns(cfg,client);rows=_records(client,dns,request['hostname'])
    if value.get('status')=='deleted':
        require(not rows and all(observe(client,item,absent=True) for item in value['resources']),'NCP_ROUTE_DELETE_UNPROVEN')
    else:
        require(value.get('status') in ('configured','stopped'),'NCP_ROUTE_NOT_CONFIGURED')
        _owned_rows(client,cfg,value,enabled=value['status']=='configured')
        require(len(rows)==1 and rows[0]['id']==value.get('dns_id') and rows[0].get('type')=='CNAME' and rows[0].get('content','').rstrip('.')==value.get('dns_target'),'NCP_DNS_RECORD_CHANGED')
    return value


def power(config_path,request,action,*,expected_state,operation_id):
    require(action in ('start','stop'),'NCP_ROUTE_ACTION_INVALID')
    cfg=configuration(config_path);_validate_request(cfg,request);client=client_for(cfg,valid_certificate=action=='start');root=private_dir(cfg['state_dir'])
    with locked(root/'routes.lock'):
        home=root/request['application_id'];value=snapshot(config_path,request)
        require(digest(value)==expected_state,'NCP_ROUTE_CHANGED_AFTER_PLAN')
        if action=='start':
            from .budget import permit
            permit(cfg['cost_approval'])
        enabled=action=='start';desired='configured' if enabled else 'stopped'
        if value['status']!=desired:
            tg=resource(value,'tg')
            once(client,home,value,'power-'+operation_id,'vloadbalancer','addTarget' if enabled else 'removeTarget',
                {'targetGroupNo':tg['id'],'targetNoList.1':cfg['worker_instance_nos'][0],'targetNoList.2':cfg['worker_instance_nos'][1]})
            deadline=time.monotonic()+180
            while True:
                targets=client.call('vloadbalancer','getTargetList',{'targetGroupNo':tg['id']}).get('targetList',[])
                if sorted(str(row['targetNo']) for row in targets)==(sorted(cfg['worker_instance_nos']) if enabled else []):break
                require(time.monotonic()<deadline,'NCP_TARGET_WAIT_TIMEOUT');time.sleep(3)
            _owned_rows(client,cfg,value,enabled=enabled)
            value['status']=desired;save(home/'state.json',value)
        return {'status':'succeeded'}
