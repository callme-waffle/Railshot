import copy
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from infrastructure.providers.environments.onprem import Adapter, ProviderError, private_file, render, save, RemoteCLI, validate_config, config_template


class Native:
    def __init__(self, context):
        self.context = context; self.calls = []; self.rows = {}; self.counter = 0; self.last_ports = []

    def run(self, argv, *, json_output=True):
        self.calls.append(argv)
        if argv == ['token', 'issue']:
            return {'project_id': 'project'}
        kind = ' '.join(argv[:3]) if argv[:3] == ['security', 'group', 'rule'] else ('security group' if argv[:2] == ['security', 'group'] else argv[0])
        at = len(kind.split()); verb = argv[at]
        if verb == 'show':
            return copy.deepcopy(self.rows[kind, argv[-1]])
        if verb == 'list':
            return [copy.deepcopy(value) for (resource, ident), value in self.rows.items() if resource == kind]
        if verb == 'delete':
            del self.rows[kind, argv[-1]]; return None
        if verb in ('start', 'stop'):
            self.rows[kind, argv[-1]]['status'] = 'ACTIVE' if verb == 'start' else 'SHUTOFF'; return None
        if verb in ('add', 'set', 'remove'):
            return None
        if verb == 'create':
            self.counter += 1
            ident = argv[-1] if kind == 'keypair' else f'00000000-0000-4000-8000-{self.counter:012d}'
            row = {'id': ident, 'name': argv[-1], 'project_id': 'project', 'status': 'ACTIVE'}
            if kind == 'keypair':
                row['public_key'] = Path(argv[argv.index('--public-key') + 1]).read_text()
            if kind == 'port':
                net = argv[argv.index('--network') + 1]
                row.update(network_id=net, security_group_ids=[argv[argv.index('--security-group') + 1]],
                    fixed_ips=[{'subnet_id': next((i for (k, i) in self.rows if k == 'subnet'), 'subnet'),
                                'ip_address': ('10.9.0.' if net == 'management' else '10.8.0.') + str(16 + self.counter)}])
            if kind == 'server':
                row['properties'] = {argv[i+1].split('=',1)[0]:argv[i+1].split('=',1)[1] for i in range(len(argv)) if argv[i] == '--property'}
                for i, arg in enumerate(argv):
                    if arg == '--nic':
                        self.rows['port', argv[i+1].split('=')[1]]['device_id'] = ident
            self.rows[kind, ident] = row
            return copy.deepcopy(row)
        raise AssertionError(argv)


class OnpremTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name); self.home.chmod(0o700)
        self.key = self.home/'public'; self.key.write_text('ssh-ed25519 AAAATEST'); self.key.chmod(0o600)
        self.context = {'environment_id': 'env-one', 'owner_id':'owner', 'account_id':'account', 'credential_ref':'credential',
            'policy_revision':1, 'state_dir':str(self.home), 'operation_id':'request-one', 'spec': {'region':'RegionOne','scope':'shared','database':{'mode':'none'}}}
        self.config = {'account_id':'account','credential_ref':'credential','project_id':'project','region':'RegionOne',
            'network':{'cidr':'10.8.0.0/24','management_cidr':'10.9.0.1/32','management_network_id':'management','external_network_id':'external'},
            'nodes':{'image_id':'image','flavor_id':'flavor','availability_zones':['a','b','c'],
                'public_key_file':str(self.key),'ssh':{'user':'ubuntu','identity_file':str(self.home/'private')}}}

    def test_read_only_connection_profile_does_not_require_a_spec(self):
        adapter = Adapter(self.config)
        self.assertEqual(adapter._profile({**self.context,'spec':None,'environment_id':None}), self.config)

    def test_account_binding_cannot_select_other_credential(self):
        with self.assertRaisesRegex(ProviderError,'CREDENTIAL_BINDING'):
            Adapter(self.config)._profile({**self.context,'spec':None,'credential_ref':'other'})

    def test_uncertain_create_is_not_replayed(self):
        adapter = Adapter(self.config)
        with adapter._state(self.context) as (home,state):
            class Lost:
                count=0
                def run(self,*args,**kwargs):
                    self.count+=1; raise TimeoutError()
            cli=Lost()
            with self.assertRaises(ProviderError) as raised:
                adapter._once(cli,home,state,'network','network',['network','create','one'])
            self.assertTrue(raised.exception.outcome_unknown)
            with self.assertRaisesRegex(ProviderError,'RECONCILIATION'):
                adapter._once(cli,home,state,'network','network',['network','create','one'])
            self.assertEqual(cli.count,1)

    def test_state_owner_change_is_rejected(self):
        adapter=Adapter(self.config)
        with adapter._state(self.context) as (home,state): save(home/'state.json',state)
        with self.assertRaisesRegex(ProviderError,'BINDING_CHANGED'):
            with adapter._state({**self.context,'owner_id':'other'}): pass

    def test_native_network_and_three_guests_pin_console_keys(self):
        adapter=Adapter(self.config); cli=Native(self.context)
        with adapter._state(self.context) as (home,state), patch.object(adapter,'_console_key',return_value='ssh-ed25519 HOSTKEY'):
            members=adapter._provision(cli,home,state,self.context)
            self.assertEqual(len(members),3)
            self.assertTrue(state['readiness']['infrastructure'])
            self.assertFalse(state['readiness']['cluster'])
            for member in members:
                self.assertTrue(Path(member['ssh']['known_hosts_file']).read_text().endswith('ssh-ed25519 HOSTKEY\n'))
                self.assertIn('connect_host',member['ssh'])
            creates=[x for x in cli.calls if x[:2]==['server','create']]
            self.assertEqual(len(creates),3)
            self.assertTrue(all(x.count('--nic')==2 and '--user-data' in x for x in creates))
            self.assertFalse(any('0.0.0.0/0' in x for x in cli.calls))
            before=len(creates)
            adapter._provision(cli,home,state,self.context)
            self.assertEqual(len([x for x in cli.calls if x[:2]==['server','create']]),before)

    def test_power_actions_read_back_owned_servers_and_never_claim_runtime_ready(self):
        adapter=Adapter(self.config); cli=Native(self.context)
        with adapter._state(self.context) as (home,state), patch.object(adapter,'_console_key',return_value='ssh-ed25519 HOSTKEY'):
            adapter._provision(cli,home,state,self.context)
        with patch.object(adapter,'_cli',return_value=cli):
            stopped=adapter.operate({'action':'stop'},self.context)
            self.assertFalse(any(stopped['readiness'].values()))
            self.assertTrue(all(row['status']=='SHUTOFF' for (kind,_),row in cli.rows.items() if kind=='server'))
            started=adapter.operate({'action':'start'},{**self.context,'operation_id':'next-request'})
            self.assertTrue(started['readiness']['infrastructure'])
            self.assertFalse(started['readiness']['deployment_binding'])

    def test_delete_requires_receipt_and_removes_graph_in_reverse_dependency_order(self):
        adapter=Adapter(self.config); cli=Native(self.context)
        with adapter._state(self.context) as (home,state), patch.object(adapter,'_console_key',return_value='ssh-ed25519 HOSTKEY'):
            adapter._provision(cli,home,state,self.context)
            state['application_config_path']='private-apps.json'; save(home/'state.json',state)
        runtime=types.ModuleType('infrastructure.providers.environments.runtime')
        runtime.teardown=lambda *args: {'status':'succeeded','environment_id':'other','data_policy':'retain'}
        with patch.dict('sys.modules',{'infrastructure.providers.environments.runtime':runtime}),patch.object(adapter,'_cli',return_value=cli):
            with self.assertRaisesRegex(ProviderError,'REVOCATION'):adapter.delete(self.context)
            self.assertFalse(any('delete' in call for call in cli.calls))
            runtime.teardown=lambda *args: {'status':'succeeded','environment_id':'env-one','data_policy':'retain','retained_resources':[{'kind':'cephfs','id':'kept'}]}
            result=adapter.delete(self.context)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['retained_resources'],[{'kind':'cephfs','id':'kept'}])
        self.assertFalse(cli.rows)
        deletes=[x for x in cli.calls if 'delete' in x]
        self.assertEqual(deletes[0][0],'server'); self.assertEqual(deletes[-1][0],'network')

    def test_delete_stops_and_retains_database_and_network_dependencies(self):
        context=copy.deepcopy(self.context); context['spec']['database']['mode']='standalone'
        config={**self.config,'database':{'flavor_id':'db-flavor'}}
        adapter=Adapter(config); cli=Native(context)
        with adapter._state(context) as (home,state), patch.object(adapter,'_console_key',return_value='ssh-ed25519 HOSTKEY'):
            adapter._provision(cli,home,state,context)
            db=state['snapshots']['database']['id']
            state['application_config_path']='private-apps.json'; save(home/'state.json',state)
        runtime=types.ModuleType('infrastructure.providers.environments.runtime')
        runtime.teardown=lambda *args: {'status':'succeeded','environment_id':'env-one','data_policy':'retain'}
        with patch.dict('sys.modules',{'infrastructure.providers.environments.runtime':runtime}),patch.object(adapter,'_cli',return_value=cli):
            result=adapter.delete(context)
        self.assertEqual(cli.rows['server',db]['status'],'SHUTOFF')
        self.assertEqual(len([row for (kind,_),row in cli.rows.items() if kind=='server']),1)
        self.assertTrue(any(row['kind']=='network' for row in result['retained_resources']))
        self.assertEqual(result['status'],'succeeded')
        self.assertNotIn('residuals',result)

    def test_templates_do_not_expand_embedded_expressions(self):
        self.assertEqual(render({'${id}':'${node}'},{'id':'one','node':{'id':1}}),{'one':{'id':1}})
        self.assertEqual(render('x-${id}',{'id':'one'}),'x-${id}')
        with self.assertRaisesRegex(ProviderError,'TEMPLATE_REFERENCE'): render('${missing}',{})

    def test_private_reference_symlink_is_rejected(self):
        link=self.home/'link';link.symlink_to(self.key)
        with self.assertRaises(ProviderError):private_file(str(link))

    def test_setup_lists_missing_fields_without_values(self):
        value=validate_config({'credentials_file': 'SENSITIVE_VALUE'})
        self.assertFalse(value['valid'])
        self.assertIn('project_id',value['missing'])
        self.assertNotIn('SENSITIVE_VALUE',json.dumps(value))
        self.assertEqual(value['verification'],'configuration_only')

    def test_default_setup_validation_checks_files_and_blank_credentials(self):
        config=copy.deepcopy(self.config)
        config.update(credentials_file=str(self.home/'credentials.json'),storage_template_file=str(self.home/'storage.json'),
            edge_template_file=str(self.home/'edge.json'),application_template_file=str(self.home/'applications.json'))
        report=validate_config(config)
        self.assertFalse(report['valid'])
        self.assertIn('ONPREM_INVALID_REFERENCE_CREDENTIALS_FILE',report['errors'])
        save(self.home/'credentials.json',{'auth_url':'https://cloud.test/v3','project_id':'project',
            'application_credential_id':'credential-id','application_credential_secret':''})
        report=validate_config(config)
        self.assertIn('ONPREM_INVALID_REFERENCE_CREDENTIALS_FILE',report['errors'])
        self.assertNotIn('https://cloud.test',json.dumps(report))

    def test_setup_template_has_no_credentials_and_stays_unconfigured(self):
        result=config_template(str(self.home))
        self.assertFalse(validate_config(result['adapter'])['valid'])
        self.assertEqual(result['files']['credentials.json']['application_credential_secret'],'')
        self.assertEqual(result['adapter']['remote_config_file'],str(self.home/'transport.json'))

    def test_remote_rpc_binds_project_and_does_not_use_shell(self):
        remote=self.home/'remote.json';save(remote,{'version':1})
        cli=RemoteCLI({**self.config,'remote_config_file':str(remote)},self.context)
        result=subprocess.CompletedProcess([],0,json.dumps({'project_id':'project','result':{'id':'n'}}).encode(),b'')
        with patch('subprocess.run',return_value=result) as run:
            self.assertEqual(cli.run(['network','show','n']),{'id':'n'})
            self.assertNotIn('shell',run.call_args.kwargs)
            payload=json.loads(run.call_args.kwargs['input'])
            self.assertEqual(payload['context'],{'owner_id':'owner','account_id':'account'})
        result.stdout=json.dumps({'project_id':'other','result':{}}).encode()
        with patch('subprocess.run',return_value=result),self.assertRaisesRegex(ProviderError,'PROJECT_MISMATCH'):
            cli.run(['network','show','n'])

if __name__=='__main__': unittest.main()
