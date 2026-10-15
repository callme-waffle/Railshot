"""Offline native-boundary tests; synthetic fixtures never contact a cloud account."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
import urllib.error

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/'infrastructure/ansible'),str(ROOT/'deployment/scripts')]
from infrastructure.providers.ncp import api,state,edge,runtime,volume
from infrastructure.providers.environments import ncp
from infrastructure.providers.environments.contract import ProviderError
from infrastructure.providers.environments.control import save,digest,read_private
import kubeadm
import environment


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.home=Path(self.temp.name)
    def write(self,name,value):
        path=self.home/name
        if isinstance(value,str):path.write_text(value);path.chmod(0o600)
        else:save(path,value)
        return str(path)
    def client(self,opener=None):
        return api.Client(self.write('credentials.json',{'access_key':'test-access','secret_key':'test-secret','member_no':'12345'}),opener=opener,clock=lambda:1700000000)
    def cluster(self):
        key=self.write('guest-key','synthetic-private-key');known=self.write('hosts','synthetic-pinned-hosts')
        members=[]
        for i,role in enumerate(('control_plane','workers','workers'),1):
            ssh={'user':'railshot','port':22,'identity_file':key,'known_hosts_file':known,'connect_host':f'8.8.4.{i}'}
            descriptor={'version':1,'provider':'ncp','member_no':'12345','environment_id':'cluster-test','region':'KR','availability_zone':'KR-2',
                'resource_id':str(i),'private_ipv4':f'10.84.1.{i+10}','public_ipv4':ssh['connect_host'],'ssh':ssh}
            members.append({'id':f'node-{i}','role':role,'availability_zone':'KR-2','descriptor_file':self.write(f'node-{i}.json',descriptor),'ssh':ssh})
        binding={'cluster_id':'cluster-test','provider':'ncp','account_id':'12345','region':'KR'}
        storage={'backend':'nfs','mode':'reuse','binding':binding,'credentials_file':self.write('credentials.json',{'access_key':'test-access','secret_key':'test-secret','member_no':'12345'}),
            'ncp':{'nas_volume_instance_no':'99','server':'169.254.1.2','share':'/n12345_volume','zone':'KR-2','worker_instance_nos':['2','3'],'owner_marker':'railshot:cluster-test'}}
        edgecfg={'version':1,'provider':'ncp','cluster_id':'cluster-test','member_no':'12345','region':'KR','zone':'KR-2','vpc_no':'10','lb_subnet_no':'12',
            'worker_instance_nos':['2','3'],'credentials_file':storage['credentials_file'],'access_key_sha256':hashlib.sha256(b'test-access').hexdigest(),
            'certificate_no':'22','base_domain':'apps.test.invalid','dns_config_file':str(self.home/'dns.json'),'state_dir':str(self.home/'edge'),'cost_approval':{'deadline':'2099-01-01T00:00:00Z','max_applications':2}}
        config={'version':1,'cluster_id':'cluster-test','provider':'ncp','account_id':'12345','region':'KR','members':members,
            'runtime':{'wait_timeout_seconds':600,'storage':storage,'edge':self.write('edge.json',edgecfg)}}
        return config


class TransportTests(Fixture):
    def test_signature_fixed_vector(self):
        self.assertEqual(api.signature('GET','/vserver/v2/getRegionList?responseFormatType=json','1700000000000','test-access','test-secret'),
            'Am02Z7/xT2yr6bBN1RLw/jiIl1N7+ZDTUH2rXfYjuKg=')
    def test_secret_in_post_body_never_url_and_fixed_host(self):
        response=Mock();response.status=200;response.read.return_value=b'{"createInitScriptResponse":{"returnCode":"0"}}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response;client=self.client(opener)
        client.call('vserver','createInitScript',{'initScriptContent':'PRIVATE-HOSTKEY'},mutation=True,sensitive=True)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://ncloud.apigw.ntruss.com/vserver/v2/createInitScript')
        self.assertIn(b'PRIVATE-HOSTKEY',request.data);self.assertEqual(request.method,'POST')
    def test_timeout_is_sanitized_and_unknown(self):
        opener=Mock();opener.open.side_effect=urllib.error.URLError('secret-token-do-not-output')
        with self.assertRaises(ProviderError) as caught:self.client(opener).call('vpc','createVpc',{},mutation=True)
        self.assertTrue(caught.exception.outcome_unknown);self.assertNotIn('secret',str(caught.exception))
    def test_redirect_refuses_signed_key_forwarding(self):
        with self.assertRaises(ProviderError):api.NoRedirect().redirect_request(None,None,None,None,None,None)
    def test_account_member_binding(self):
        client=self.client();client.request=Mock(return_value={'returnCode':'0','sslCertificateList':[{'certificateNo':'22','memberNo':'999'}]})
        with self.assertRaisesRegex(ProviderError,'NCP_ACCOUNT_MISMATCH'):client.account('22','12345')


class JournalTests(Fixture):
    def test_lost_create_reply_never_replayed_or_secret_persisted(self):
        value={'steps':{},'resources':[]};client=Mock();client.call.side_effect=TimeoutError()
        for _ in range(2):
            with self.assertRaises(ProviderError):state.once(client,self.home,value,'init','vserver','createInitScript',{'initScriptContent':'private-key'},kind='init_script',sensitive=True)
        self.assertEqual(client.call.call_count,1);self.assertNotIn('private-key',(self.home/'state.json').read_text())
    def test_owned_id_required_and_truncation_not_absence(self):
        item={'kind':'vpc','id':'10','expected':{'vpcName':'owned'}};client=Mock()
        client.call.return_value={'totalRows':1,'vpcList':[{'vpcNo':'10','vpcName':'foreign'}]}
        with self.assertRaisesRegex(ProviderError,'OWNERSHIP'):state.observe(client,item)
        client.call.return_value={'totalRows':2,'vpcList':[]}
        with self.assertRaisesRegex(ProviderError,'TRUNCATED'):state.observe(client,item,absent=True)
    def test_server_running_with_pending_operation_is_not_ready(self):
        item={'kind':'server','id':'1'};client=Mock()
        client.call.return_value={'totalRows':1,'serverInstanceList':[{'serverInstanceNo':'1','serverInstanceStatus':{'code':'RUN'},'serverInstanceOperation':{'code':'STOP'}}]}
        with self.assertRaisesRegex(ProviderError,'WAIT_TIMEOUT'):state.wait(client,item,seconds=-1)
    def test_new_resource_replay_returns_same_owned_id(self):
        value={'steps':{},'resources':[]};client=Mock();client.call.return_value={'vpcList':[{'vpcNo':'10'}]}
        self.assertEqual(state.once(client,self.home,value,'vpc','vpc','createVpc',{'vpcName':'owned'},kind='vpc'),'10')
        self.assertEqual(state.once(client,self.home,value,'vpc','vpc','createVpc',{'vpcName':'owned'},kind='vpc'),'10')
        self.assertEqual(client.call.call_count,1)
        with self.assertRaisesRegex(ProviderError,'INPUT_CHANGED'):state.once(client,self.home,value,'vpc','vpc','createVpc',{'vpcName':'other'},kind='vpc')


class RuntimeTests(Fixture):
    def test_real_kubeadm_registry_and_pinned_public_ssh_inventory(self):
        config=self.cluster();request=kubeadm.from_registry(config,request_id='test-native-contract')
        inventory=kubeadm.inventory(request)['all']['children']['kubeadm_control']['hosts']['node-1']
        self.assertEqual(inventory['ansible_host'],'8.8.4.1')
        self.assertIn('StrictHostKeyChecking=yes',inventory['ansible_ssh_common_args']);self.assertIn('HostKeyAlias=10.84.1.11',inventory['ansible_ssh_common_args'])
        selected={'profile':'kubeadm-1c2w','purpose':'runtime','cluster_file':self.write('cluster.json',config),
            'management_endpoint':'https://8.8.4.1:6443','metrics_endpoints':{f'node-{i}':f'8.8.4.{i}' for i in (1,2,3)}}
        request,descriptor=environment.registered_node(selected,'cluster-test','test-registration')
        self.assertEqual(descriptor['management_endpoint'],'https://8.8.4.1:6443');self.assertEqual(descriptor['addresses']['metrics'],'8.8.4.1')
        self.assertEqual(descriptor['node_metrics_addresses'],['8.8.4.1','8.8.4.2','8.8.4.3'])
    def test_descriptor_cannot_redirect_guest_ssh(self):
        config=self.cluster();config['members'][0]['ssh']['connect_host']='8.8.8.8'
        with self.assertRaisesRegex(ProviderError,'IDENTITY_CHANGED'):kubeadm.from_registry(config,request_id='test-native-contract')
    def test_cross_zone_nas_rejected(self):
        request=kubeadm.from_registry(self.cluster(),request_id='test-native-contract')
        request['inventory']['workers'][0]['availability_zone']='KR-1'
        with self.assertRaisesRegex(ProviderError,'ZONE_OR_WORKER'):runtime.validate_storage(request)
    def test_nas_native_acl_drift_rejected(self):
        request=kubeadm.from_registry(self.cluster(),request_id='test-native-contract');cfg=request['runtime']['storage']['ncp']
        client=Mock();client.credentials={'member_no':'12345'};client.call.return_value={'nasVolumeInstanceList':[{'nasVolumeInstanceNo':'99','zoneCode':'KR-2',
            'nasVolumeDescription':cfg['owner_marker'],'mountInformation':cfg['server']+':'+cfg['share'],'volumeAllotmentProtocolType':{'code':'NFS'},
            'nasVolumeInstanceStatus':{'code':'CREAT'},'nasVolumeServerInstanceNoList':['2','foreign']}]}
        with patch.object(runtime,'Client',return_value=client),self.assertRaisesRegex(ProviderError,'NATIVE_BINDING'):runtime.prepare_storage(request)
    def test_nfs_csi_retains_pvc_subdirectories(self):
        path=ROOT/'deployment/shared-storage/install.py';spec=importlib.util.spec_from_file_location('ncp_storage_install',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        cfg=self.cluster()['runtime']['storage'];cfg['workers']=['node-2','node-3'];cfg['backend_descriptor']={**cfg['binding'],'server':cfg['ncp']['server'],'share':cfg['ncp']['share']}
        rendered=module.render(cfg)
        self.assertIn('retain',json.dumps(rendered));self.assertIn('nfs.csi.k8s.io',json.dumps(rendered))
    def test_blank_setup_is_never_valid_and_no_secret_values(self):
        template=ncp.config_template(str(self.home));cfg=template['adapter']
        for name,value in template['files'].items():self.write(name,value)
        result=ncp.validate_config(cfg)
        self.assertFalse(result['valid']);self.assertIn('account_id',result['missing']);self.assertNotIn('secret_key',json.dumps(result))




class EdgeCloud:
    """Documented NCP fixture boundary, with mutable native target and DNS state."""
    def __init__(self):
        self.rows={};self.targets=['2','3'];self.records=[];self.calls=[]
    def call(self,family,action,params=None,**kwargs):
        params=params or {};self.calls.append((action,copy.deepcopy(params)))
        for kind,desc in state.KINDS.items():
            if action==desc[1]:
                ident='101' if kind=='target_group' else '102'
                row={desc[4]:ident,**{k:v for k,v in params.items() if '.' not in k},'regionCode':'KR'}
                if kind=='target_group':row.update(targetGroupProtocolType={'code':'HTTP'},healthCheckProtocolType={'code':'HTTP'})
                else:row.update(loadBalancerDomain='fixture.lb.ncloud.com',loadBalancerInstanceStatus={'code':'USED'},loadBalancerInstanceOperation={'code':'NULL'})
                self.rows[kind]=row;return {desc[3]:[copy.deepcopy(row)]}
            if action==desc[2]:
                rows=[copy.deepcopy(self.rows[kind])] if kind in self.rows else []
                return {desc[3]:rows,'totalRows':len(rows)}
        if action=='getLoadBalancerListenerList':return {'loadBalancerListenerList':[{'loadBalancerListenerNo':'111','protocolType':{'code':'HTTPS'},'port':443,'sslCertificateNo':'22','tlsMinVersionType':{'code':'TLSV12'}}]}
        if action=='getLoadBalancerRuleList':return {'loadBalancerRuleList':[{'loadBalancerRuleConditionList':[{'ruleConditionType':{'code':'DEFAULT'}}],
            'loadBalancerRuleActionList':[{'ruleActionType':{'code':'TARGET_GROUP'},'targetGroupAction':{'targetGroupWeightList':[{'targetGroupNo':'101','weight':1}]}}]}]}
        if action=='getTargetList':return {'targetList':[{'targetNo':target,'healthCheckStatus':{'code':'DOWN'}} for target in self.targets]}
        if action=='removeTarget':self.targets=[];return {}
        if action=='addTarget':self.targets=['2','3'];return {}
        if action=='deleteLoadBalancerInstances':self.rows.pop('load_balancer');return {}
        if action=='deleteTargetGroups':self.rows.pop('target_group');return {}
        raise AssertionError(action)
    def request(self,service,path,params=None,*,method='GET',document=None,**kwargs):
        if '/domain/' in path:return {'id':77,'name':'apps.test.invalid','completeYn':True,'status':'SUCCESS'}
        if '/apply/' in path:return {}
        if method=='POST':self.records=[{**document[0],'id':88}];return {}
        if method=='DELETE':self.records=[];return {}
        return {'last':True,'content':copy.deepcopy(self.records)}


class EdgeLifecycleTests(Fixture):
    def setup_edge(self):
        self.cluster();cfg=edge.configuration(str(self.home/'edge.json'))
        self.write('dns.json',{'domain_id':'77','base_domain':cfg['base_domain'],'credentials_file':cfg['credentials_file']})
        self.cloud=EdgeCloud();mock=patch.object(edge,'client_for',return_value=self.cloud);mock.start();self.addCleanup(mock.stop)
        self.path=str(self.home/'edge.json');self.request={'environment_id':'cluster-test','application_id':'app-'+'a'*24,'hostname':'web.apps.test.invalid','node_port':30120,'health_path':'/health'}
        self.binding={'version':1,'provider':'ncp','environment_id':'cluster-test','application_id':self.request['application_id'],'hostname':self.request['hostname'],
            'ingress':{'ncp_route_config_file':self.path},'registered':{'target':{'id':self.request['application_id'],'namespace':self.request['application_id'],'node_port':30120}}}
    def test_route_native_publication_stop_start_delete_and_retained_certificate(self):
        import ncp_routes
        self.setup_edge();result=edge.configure(self.path,self.request)
        self.assertTrue(result['edge_ready']);self.assertFalse(result['public_http_verified']);self.assertFalse(result['backend_healthy'])
        for action,expected in [('stop',[]),('start',['2','3']),('delete',[])]:
            plan=ncp_routes.plan(self.binding,action)
            self.assertEqual(ncp_routes.observe(self.binding,action,plan),'not_started')
            ncp_routes.execute(self.binding,action,plan)
            self.assertEqual(ncp_routes.observe(self.binding,action,plan),'succeeded')
            if action!='delete':self.assertEqual(self.cloud.targets,expected)
        self.assertFalse(self.cloud.rows);self.assertFalse(self.cloud.records)
        self.assertFalse(any('Certificate' in action for action,_ in self.cloud.calls))
    def test_listener_target_drift_prevents_destructive_plan(self):
        import ncp_routes
        self.setup_edge();edge.configure(self.path,self.request)
        old=self.cloud.call
        def changed(family,action,params=None,**kwargs):
            result=old(family,action,params,**kwargs)
            if action=='getLoadBalancerRuleList':result['loadBalancerRuleList'][0]['loadBalancerRuleActionList'][0]['targetGroupAction']['targetGroupWeightList'][0]['targetGroupNo']='foreign'
            return result
        self.cloud.call=changed
        with self.assertRaisesRegex(ProviderError,'LISTENER_TARGET_CHANGED'):ncp_routes.plan(self.binding,'delete')
        self.assertTrue(self.cloud.rows)
    def test_dns_drift_never_deleted(self):
        self.setup_edge();edge.configure(self.path,self.request);self.cloud.records[0]['content']='foreign.invalid'
        with self.assertRaisesRegex(ProviderError,'DNS_RECORD_REPLACED'):edge.revoke(self.path,self.request)
        self.assertEqual(self.cloud.records[0]['content'],'foreign.invalid')
    def test_changed_plan_blocks_mutation(self):
        import ncp_routes
        self.setup_edge();edge.configure(self.path,self.request);plan=ncp_routes.plan(self.binding,'stop')
        plan['action']='delete'
        with self.assertRaisesRegex(ProviderError,'PLAN_CHANGED'):ncp_routes.execute(self.binding,'delete',plan)
        self.assertEqual(self.cloud.targets,['2','3'])


class VolumeSafetyTests(Fixture):
    def node(self):
        return {'private_ipv4':'10.84.1.14','ssh':{'known_hosts_file':'/private/hosts','identity_file':'/private/key','user':'railshot','connect_host':'8.8.4.4'},
            'data_disk':{'resource_id':'900','device':'/dev/vdb','mount_path':'/var/lib/postgresql'}}
    def test_existing_filesystem_is_never_formatted(self):
        response=Mock(returncode=0,stdout=json.dumps({'disk':{'fstype':'ext4','name':'/dev/vdb','type':'disk'},'mount':[]}).encode())
        with patch.object(volume.subprocess,'run',return_value=response) as call,self.assertRaisesRegex(ProviderError,'NOT_BLANK'):
            volume.prepare(self.home,{'steps':{}},self.node())
        self.assertEqual(call.call_count,1);self.assertNotIn('mkfs',call.call_args.kwargs['input'].decode())
    def test_root_disk_name_rejected_before_ssh(self):
        node=self.node();node['data_disk']['device']='/dev/vda'
        with patch.object(volume.subprocess,'run') as call,self.assertRaisesRegex(ProviderError,'DEVICE_UNPROVEN'):volume.prepare(self.home,{'steps':{}},node)
        call.assert_not_called()
    def test_format_intent_is_durable_before_remote_mutation(self):
        probe={'disk':{'name':'/dev/vdb','type':'disk','fstype':None},'mount':[]};calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            if len(calls)==1:return Mock(returncode=0,stdout=json.dumps(probe).encode())
            self.assertEqual(read_private(self.home/'state.json')['steps']['database-format']['status'],'unknown')
            raise subprocess.TimeoutExpired(argv,180)
        value={'steps':{}}
        with patch.object(volume.subprocess,'run',side_effect=run),self.assertRaises(subprocess.TimeoutExpired):volume.prepare(self.home,value,self.node())
        with patch.object(volume.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(probe).encode())) as call,self.assertRaisesRegex(ProviderError,'RECONCILIATION'):
            volume.prepare(self.home,value,self.node())
        self.assertEqual(call.call_count,1)


class NetworkCloud:
    def __init__(self):self.rows={};self.rules={'INBND':[],'OTBND':[]};self.calls=[]
    def call(self,family,action,params=None,**kwargs):
        params=params or {};self.calls.append((action,copy.deepcopy(params)))
        for kind,desc in state.KINDS.items():
            if action==desc[1]:
                ident=str(10+sum(len(rows) for rows in self.rows.values()));row={desc[4]:ident,**params}
                if desc[5]:row[desc[5]]={'code':desc[6]}
                self.rows.setdefault(kind,[]).append(row);return {desc[3]:[row]}
            if action==desc[2]:
                rows=[row for row in self.rows.get(kind,[]) if str(row[desc[4]])==params.get(desc[4]+'List.1')]
                return {desc[3]:rows,'totalRows':len(rows)}
        if action=='getNetworkAclList':return {'networkAclList':[{'isDefault':True,'vpcNo':params['vpcNo'],'networkAclNo':'99'}]}
        if action=='addRouteTableSubnet':return {}
        if action=='getRouteTableSubnetList':return {'totalRows':2,'subnetList':self.rows['subnet']}
        if action=='getRouteList':return {'routeList':[{'destinationCidrBlock':'0.0.0.0/0','targetType':{'code':'IGW'}}]}
        if action=='getAccessControlGroupRuleList':
            rows=self.rules[params['accessControlGroupRuleTypeCode']];return {'totalRows':len(rows),'accessControlGroupRuleList':rows}
        if action in ('addAccessControlGroupInboundRule','addAccessControlGroupOutboundRule'):
            direction='INBND' if 'Inbound' in action else 'OTBND'
            indexes=sorted({key.split('.')[1] for key in params if key.startswith('accessControlGroupRuleList.')})
            for index in indexes:
                prefix='accessControlGroupRuleList.'+index+'.';values={key[len(prefix):]:value for key,value in params.items() if key.startswith(prefix)}
                values['protocolType']={'code':values.pop('protocolTypeCode')};values.update(accessControlGroupNo=params['accessControlGroupNo'],accessControlGroupRuleType={'code':direction})
                self.rules[direction].append(values)
            return {}
        raise AssertionError(action)


class NetworkTests(Fixture):
    def test_new_network_real_native_graph_and_scoped_management_rules(self):
        profile=ncp.config_template(str(self.home))['adapter'];profile['network']['management_cidr']='8.8.8.8/32'
        self.write('applications.json',{'environments':{'generated':{'ingress':{'observability_config_file':self.write('observer.json',{'observer_ip':'8.8.4.4','node_metrics_port':30910,'cluster_metrics_port':30911})}}}})
        adapter=ncp.Adapter(profile);value={'resources':[],'steps':{}};client=NetworkCloud()
        adapter._network(client,self.home,value,{'environment_id':'env-test','owner_id':'account:test'})
        self.assertEqual(len(client.rows['subnet']),2)
        for row in client.rules['INBND']:
            if row.get('portRange') in ('22','6443'):self.assertEqual(row['ipBlock'],'8.8.8.8/32')
        self.assertEqual({row['portRange'] for row in client.rules['INBND'] if row.get('ipBlock')=='8.8.4.4/32'},{'30910','30911'})
        count=len([action for action,_ in client.calls if action.startswith('create') or action.startswith('add')])
        adapter._network(client,self.home,value,{'environment_id':'env-test','owner_id':'account:test'})
        self.assertEqual(count,len([action for action,_ in client.calls if action.startswith('create') or action.startswith('add')]))
        client.rules['INBND'].append({'accessControlGroupNo':value['network']['acg_no'],'accessControlGroupRuleType':{'code':'INBND'},'protocolType':{'code':'TCP'},'portRange':'22','ipBlock':'0.0.0.0/0'})
        with self.assertRaisesRegex(ProviderError,'ACG_FOREIGN_RULE'):adapter._network(client,self.home,value,{'environment_id':'env-test','owner_id':'account:test'})


class BudgetTests(Fixture):
    def test_scope_estimate_expiry_and_duration_are_bound_before_creation(self):
        from datetime import datetime,timezone,timedelta
        from infrastructure.providers.ncp import budget
        now=datetime.now(timezone.utc);spec={'account_id':'ncp-test','region':'KR','runtime':{'workers':2}}
        cfg={'account_id':'ncp-test','region':'KR','nodes':{'size':'c2'}}
        quote={'account_id':'ncp-test','region':'KR','spec_sha256':digest(spec),'configuration_sha256':digest(cfg),
            'estimated_monthly_krw':100000,'approved_at':(now-timedelta(minutes=1)).isoformat().replace('+00:00','Z'),
            'expires_at':(now+timedelta(hours=1)).isoformat().replace('+00:00','Z'),'test_duration_seconds':600,'max_applications':1,'data_retention':'retain'}
        cfg['budget']={'approved':True,'max_monthly_krw':120000,'quote':quote}
        reservation=budget.reserve(cfg,spec,{})
        self.assertLessEqual((budget.utc(reservation['deadline'])-now).total_seconds(),601)
        bad=copy.deepcopy(cfg);bad['nodes']['size']='expensive'
        with self.assertRaisesRegex(ProviderError,'SCOPE_CHANGED'):budget.validate(bad,spec)
        bad=copy.deepcopy(cfg);bad['budget']['max_monthly_krw']=90000
        with self.assertRaisesRegex(ProviderError,'LIMIT_EXCEEDED'):budget.validate(bad,spec)
        with self.assertRaisesRegex(ProviderError,'WINDOW_EXPIRED'):budget.permit({'deadline':'2000-01-01T00:00:00Z'})
    def test_kubernetes_cidr_overlap_rejected_offline(self):
        cfg=ncp.config_template(str(self.home))['adapter'];cfg['network'].update(vpc_cidr='10.244.0.0/16',vm_subnet_cidr='10.244.1.0/24',lb_subnet_cidr='10.244.2.0/24',management_cidr='8.8.8.8/32')
        self.assertIn('NCP_NETWORK_INVALID',ncp.validate_config(cfg)['errors'])
