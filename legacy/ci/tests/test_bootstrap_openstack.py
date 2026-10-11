import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
cli_module = importlib.import_module('infrastructure.providers.openstack.cli')
identity = importlib.import_module('infrastructure.providers.openstack.identity')
discovery = importlib.import_module('infrastructure.providers.openstack.discovery')
access = importlib.import_module('infrastructure.providers.openstack.access')


def test_cli_secrets_private_config_not_argv_and_cleanup(monkeypatch):
    monkeypatch.setenv('OS_PASSWORD','ambient-secret')
    seen = []
    def runner(args, **kwargs):
        path = Path(kwargs['env']['OS_CLIENT_CONFIG_FILE'])
        seen.append(path)
        assert 'secret-value' not in repr(args)
        assert 'OS_PASSWORD' not in kwargs['env']
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())['clouds']['railshot']['auth']['application_credential_secret']=='secret-value'
        assert kwargs['shell'] is False
        return SimpleNamespace(returncode=0, stdout='[]',stderr='')
    assert cli_module.OpenStackCLI({'application_credential_id':'id','application_credential_secret':'secret-value'},runner=runner).run(['server','list']) == []
    assert not seen[0].exists()


def test_cli_redacts_errors():
    def runner(*args,**kwargs):
        return SimpleNamespace(returncode=1,stdout='',stderr='403 password=secret-value')
    with pytest.raises(cli_module.ProviderError) as exc:
        cli_module.OpenStackCLI({},runner=runner).run(['server','list'])
    assert str(exc.value)=='forbidden'


CONFIG = {'auth_url':'https://cloud.example/v3','admin_username':'admin','user_domain_name':'Default','project_id':'p','service_username':'jasmin','role_id':'r'}
class FakeKeystone:
    calls = []
    existing = False
    def __init__(self,endpoint):
        self.calls.clear()
    def request(self,method,path,body=None,token=None):
        self.calls.append((method,path,body,token))
        if path.startswith('/users?'):
            return {'users':[{'id':'existing'}] if self.existing else []},None
        if path=='/users':
            return {'user':{'id':'u'}},None
        if path.endswith('/application_credentials'):
            return {'application_credential':{'id':'a','secret':'app-secret'}},None
        return {'token':{'user':{'domain':{'id':'d'}},'project':{'id':'p'}}},'ephemeral-token'


def test_identity_unscoped_then_project_and_records_no_secrets():
    resources=[]
    result=identity.configure_identity(CONFIG,'admin-secret',resources.append,keystone_factory=FakeKeystone)
    assert result['application_credential_secret']=='app-secret'
    assert FakeKeystone.calls[0][2]['auth']['scope']=='unscoped'
    assert FakeKeystone.calls[1][2]['auth']['scope']=={'project':{'id':'p'}}
    assert all(secret not in repr(resources) for secret in ('app-secret','admin-secret','ephemeral-token'))
    assert [r['type'] for r in resources]==['user','role_assignment','application_credential']


def test_identity_existing_user_never_reset(monkeypatch):
    monkeypatch.setattr(FakeKeystone,'existing',True)
    with pytest.raises(cli_module.ProviderError,match='Existing service user'):
        identity.configure_identity(CONFIG,'admin-secret',keystone_factory=FakeKeystone)
    assert not any(call[0] in ('PUT','PATCH','DELETE') or call[1]=='/users' for call in FakeKeystone.calls)


def test_catalog_missing_distinguished_from_forbidden():
    class CLI:
        def run(self,args):
            if args==['catalog','list']:
                return [{'Type':'compute'}]
            raise cli_module.ProviderError('forbidden')
    report=discovery.discover_capabilities(CLI())
    assert report['services']['compute']['status']=='unknown'
    assert report['services']['network']['status']=='not_in_catalog'
    assert report['creation_verified'] is False


def test_access_rejects_unrestricted_before_calls(tmp_path):
    class CLI:
        def run(self,args):
            pytest.fail('No remote command expected')
    with pytest.raises(ValueError):
        access.prepare_vm_access(CLI(),{'network_id':'n','ssh_username':'ubuntu','ssh_source_cidr':'0.0.0.0/0'},tmp_path)


def test_ssh_requires_trusted_host_and_fixed_command(tmp_path):
    key=tmp_path/'key';key.write_text('private');key.chmod(0o600)
    known=tmp_path/'known';known.write_text('192.0.2.1 ssh-ed25519 AAAA');known.chmod(0o600)
    def runner(args,**kwargs):
        assert 'StrictHostKeyChecking=yes' in args
        assert args[-1]=='true'
        assert args[1:3]==['-F','/dev/null']
        return SimpleNamespace(returncode=0)
    profile={'host':'192.0.2.1','ssh_username':'ubuntu','private_key_path':str(key)}
    assert access.verify_vm_access(profile,known,runner=runner)['vm_access_status']=='verified'
    profile['host']='-oProxyCommand=bad'
    with pytest.raises(ValueError):
        access.verify_vm_access(profile,known,runner=runner)


def test_https_only_no_redirects():
    for endpoint in ('http://cloud/v3','https://user:pw@cloud/v3','https://cloud/v3?x=1'):
        with pytest.raises(ValueError):
            identity.Keystone(endpoint)
    assert identity.NoRedirect().redirect_request(None,None,302,'',{},'https://other') is None


def test_identity_partial_failure_preserves_resource_evidence():
    class FailedRole(FakeKeystone):
        def request(self,method,path,body=None,token=None):
            if method=='PUT':
                raise cli_module.ProviderError('timeout')
            return super().request(method,path,body,token)
    resources=[]
    with pytest.raises(cli_module.ProviderError,match='timeout'):
        identity.configure_identity(CONFIG,'pw',resources.append,keystone_factory=FailedRole)
    assert resources==[{'type':'user','id':'u','name':'jasmin','domain_id':'d'}]


def test_identity_malformed_response():
    class Broken(FakeKeystone):
        def request(self,*args,**kwargs):
            return {},None
    with pytest.raises(cli_module.ProviderError,match='identity_invalid_response'):
        identity.configure_identity(CONFIG,'pw',keystone_factory=Broken)


def test_identity_project_mismatch():
    class WrongProject(FakeKeystone):
        def request(self,method,path,body=None,token=None):
            data,token=super().request(method,path,body,token)
            if body and body.get('auth',{}).get('identity',{}).get('methods')==['application_credential']:
                data['token']['project']['id']='wrong'
            return data,token
    with pytest.raises(cli_module.ProviderError,match='project_mismatch'):
        identity.configure_identity(CONFIG,'pw',keystone_factory=WrongProject)


def test_access_create_resume_and_detect_rule_drift(tmp_path):
    resources=[]
    class CLI:
        drift=False
        def run(self,args):
            if args[:3]==['security','group','list']:
                return []
            if args[:4]==['security','group','rule','list']:
                return [{'ID':'rule'}]
            if args[:4]==['security','group','rule','show']:
                return {'direction':'ingress','protocol':'tcp','port_range_min':22,'port_range_max':22,
                        'remote_ip_prefix':'0.0.0.0/0' if self.drift else '192.0.2.1/32','remote_group_id':None,'ethertype':'IPv4'}
            if args[:2]==['keypair','show']:
                return {'public_key':'ssh-ed25519 AAAA'}
            return {'id':'group' if args[:3]==['security','group','create'] else 'rule'}
    def keygen(args,**kwargs):
        key=Path(args[-1]);key.write_text('private');key.chmod(0o600)
        key.with_suffix('.pub').write_text('ssh-ed25519 AAAA')
        return SimpleNamespace(returncode=0)
    config={'network_id':'n','ssh_username':'ubuntu','ssh_source_cidr':'192.0.2.1/32'}
    cli=CLI()
    result=access.prepare_vm_access(cli,config,tmp_path,resources.append,runner=keygen)
    assert result['vm_access_status']=='unverified'
    assert 'private' not in Path(result['cloud_init_path']).read_text()
    config['existing_resources']=resources
    assert access.prepare_vm_access(cli,config,tmp_path,resources.append,runner=keygen)==result
    cli.drift=True
    with pytest.raises(cli_module.ProviderError,match='security_group_rules_changed'):
        access.prepare_vm_access(cli,config,tmp_path,resources.append,runner=keygen)


def test_access_rejects_symlink_directory(tmp_path):
    target=tmp_path/'actual';target.mkdir()
    link=tmp_path/'link';link.symlink_to(target,target_is_directory=True)
    class CLI:
        def run(self,args):
            return {}
    with pytest.raises(ValueError,match='Unsafe directory'):
        access.prepare_vm_access(CLI(),{'network_id':'n','ssh_username':'ubuntu','ssh_source_cidr':'192.0.2.1/32'},link)


@pytest.mark.parametrize('unsafe',['hardlink','symlink_parent','writable_parent','fifo','public_mode'])
def test_ssh_rejects_untrusted_files_before_read_or_execution(tmp_path,unsafe):
    directory=tmp_path/'private';directory.mkdir(mode=0o700)
    key=directory/'key';key.write_text('private');key.chmod(0o600)
    known=directory/'known';known.write_text('trusted');known.chmod(0o600)
    if unsafe=='hardlink':
        os.link(key,directory/'other')
    elif unsafe=='symlink_parent':
        link=tmp_path/'link';link.symlink_to(directory,target_is_directory=True)
        known=link/'known'
    elif unsafe=='writable_parent':
        directory.chmod(0o777)
    elif unsafe=='fifo':
        known.unlink();os.mkfifo(known,mode=0o600)
    else:
        known.chmod(0o644)
    with pytest.raises(ValueError):
        access.verify_vm_access({'host':'192.0.2.1','ssh_username':'ubuntu','private_key_path':str(key)},known,
                                runner=lambda *args,**kwargs: pytest.fail('No SSH execution expected'))


def test_ssh_rejects_wrong_owner(tmp_path,monkeypatch):
    known=tmp_path/'known';known.write_text('trusted');known.chmod(0o600)
    actual=Path.lstat
    def wrong_owner(path,*args,**kwargs):
        result=actual(path,*args,**kwargs)
        if path==known:
            values=list(result);values[4]=987654
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path,'lstat',wrong_owner)
    with pytest.raises(ValueError,match='Unsafe credential'):
        access.verify_vm_access({'host':'192.0.2.1','ssh_username':'ubuntu','private_key_path':str(known)},known)
