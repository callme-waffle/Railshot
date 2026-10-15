"""NAVER Cloud public KR VPC VM environments; native observations gate readiness."""
import base64
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

from .contract import ProviderError, READINESS
from .control import read_private, save, private_dir, digest
from infrastructure.providers.ncp.api import Client, require
from infrastructure.providers.ncp.state import state, once, observe, wait, resource, KINDS

ROOT=Path(__file__).resolve().parents[3]

def key_file(path):
    path=Path(path);require(path.is_absolute() and path.resolve()==path,'NCP_PRIVATE_PATH_INVALID')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
        info=os.fstat(stream.fileno());require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
            and not info.st_mode&0o077 and info.st_nlink==1 and 0<info.st_size<65536,'NCP_PRIVATE_FILE_INVALID')
        return stream.read(65536)

def validate_config(config):
    missing=[];errors=[]
    fields=('account_id','credential_ref','member_no','access_key_sha256','credentials_file','region','zone',
        'network.vpc_cidr','network.vm_subnet_cidr','network.lb_subnet_cidr','network.management_cidr',
        'nodes.server_image_no','nodes.server_spec_code','nodes.login_key_name','nodes.ssh_user','nodes.identity_file','nodes.public_key_file',
        'nas.size_gib','edge.certificate_no','edge.base_domain','edge.dns_config_file','application_template_file')
    def at(field):
        node=config
        for part in field.split('.'):
            if not isinstance(node,dict):return None
            node=node.get(part)
        return node
    for field in fields:
        if at(field) in (None,'',[]):missing.append(field)
    if not isinstance(config,dict):return {'valid':False,'missing':list(fields),'errors':['NCP_PROFILE_REQUIRED']}
    if config.get('region')!='KR' or config.get('zone') not in ('KR-1','KR-2'):errors.append('NCP_PUBLIC_KR_ZONE_REQUIRED')
    try:
        nets=[ipaddress.IPv4Network(at(k),strict=True) for k in ('network.vpc_cidr','network.vm_subnet_cidr','network.lb_subnet_cidr')]
        require(all(n.is_private and 16<=n.prefixlen<=28 for n in nets) and all(n.subnet_of(nets[0]) for n in nets[1:])
            and not nets[1].overlaps(nets[2]) and not any(nets[0].overlaps(ipaddress.IPv4Network(cidr)) for cidr in ('10.244.0.0/16','10.96.0.0/12')),'NCP_CIDR_INVALID')
        management=ipaddress.IPv4Network(at('network.management_cidr'),strict=True)
        require(management.prefixlen==32 and management.network_address.is_global,'NCP_MANAGEMENT_CIDR_TOO_BROAD')
    except (TypeError,ValueError,ProviderError):errors.append('NCP_NETWORK_INVALID')
    if not isinstance(at('nas.size_gib'),int) or not 500<=at('nas.size_gib')<=20000 or at('nas.size_gib')%100:errors.append('NCP_NAS_SIZE_INVALID')
    if not re.fullmatch(r'[a-f0-9]{64}',str(config.get('access_key_sha256',''))):errors.append('NCP_CREDENTIAL_FINGERPRINT_REQUIRED')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}',str(at('nodes.ssh_user'))) or at('nodes.ssh_user')=='root':errors.append('NCP_NONROOT_SSH_USER_REQUIRED')
    for field in ('credentials_file','nodes.identity_file','nodes.public_key_file','edge.dns_config_file','application_template_file','database.config_file'):
        path=at(field)
        if not path:continue
        try:
            if field in ('nodes.identity_file','nodes.public_key_file'):key_file(path)
            else:
                value=read_private(path);require(isinstance(value,dict) and value,'NCP_FILE_OBJECT_REQUIRED')
                def populated(item):
                    if isinstance(item,dict):return bool(item) and all(populated(v) for v in item.values())
                    if isinstance(item,list):return bool(item) and all(populated(v) for v in item)
                    return not isinstance(item,str) or bool(item) and 'REPLACE_' not in item and 'example.com' not in item
                require(populated(value),'NCP_FILE_INPUT_REQUIRED')
                if field=='credentials_file':
                    client=Client(path);require(client.fingerprint==config['access_key_sha256'] and value['member_no']==config['member_no'],'NCP_CREDENTIAL_BINDING_CHANGED')
                if field=='application_template_file':
                    require(all(value.get(k) for k in ('state_dir','cd','environments')) and len(value['environments'])==1,'NCP_APPLICATION_TEMPLATE_REQUIRED')
                    # Validate the exact product template schema after replacing only
                    # the generated registry reference, without opening cloud sockets.
                    sys.path.insert(0,str(ROOT/'deployment/scripts'))
                    apps=importlib.import_module('applications')
                    with tempfile.TemporaryDirectory(prefix='railshot-ncp-validate-') as directory:
                        normalized=copy.deepcopy(value);normalized['registry_file']=str(Path(directory)/'registry.json')
                        candidate=Path(directory)/'applications.json';save(candidate,normalized);apps.load_config(str(candidate))
                    profile=next(iter(value['environments'].values()))
                    pull=read_private(profile['pull_secret_file']);auth=base64.b64decode(pull['auths']['ghcr.io']['auth'],validate=True).decode()
                    require(':' in auth and all(auth.split(':',1)),'NCP_PULL_AUTH_REQUIRED')
                    ingress=profile['ingress'];require(ingress['base_domain']==config['edge']['base_domain'],'NCP_BASE_DOMAIN_CHANGED')
                    ci=read_private(ingress['ci_credentials_file']);require(set(ci)=={'token'} and isinstance(ci['token'],str) and ci['token'],'NCP_CI_TOKEN_REQUIRED')
                    observer=read_private(ingress['observability_config_file'])
                    source=ipaddress.IPv4Network(observer.get('observer_source_cidr',observer['observer_ip']+'/32'),strict=True)
                    require(source.prefixlen==32 and source.network_address.is_global,'NCP_OBSERVER_SOURCE_INVALID')
                    require(all(type(observer.get(k)) is int and 30000<=observer[k]<=32767 for k in ('node_metrics_port','cluster_metrics_port')),'NCP_OBSERVER_PORT_INVALID')
                if field=='database.config_file':
                    sys.path.insert(0,str(ROOT/'infrastructure/ansible'))
                    try:database=importlib.import_module('standalone')
                    except ImportError:raise ProviderError('NCP_SHARED_DATABASE_RUNTIME_REQUIRED') from None
                    require(database.validate_config(value).get('valid') is True,'NCP_DATABASE_CONFIG_INVALID')
        except (OSError,TypeError,ValueError,KeyError,ProviderError,UnicodeError):errors.append('NCP_INVALID_REFERENCE_'+field.upper().replace('.','_'))
    return {'valid':not missing and not errors,'missing':missing,'errors':sorted(set(errors)),'verification':'configuration_only'}

def config_template(directory):
    home=Path(directory);require(home.is_absolute())
    profile={'account_id':'','credential_ref':'','member_no':'','access_key_sha256':'','credentials_file':str(home/'credentials.json'),
        'region':'KR','zone':'KR-2','network':{'vpc_cidr':'10.84.0.0/16','vm_subnet_cidr':'10.84.1.0/24','lb_subnet_cidr':'10.84.2.0/24','management_cidr':''},
        'nodes':{'server_image_no':'','server_spec_code':'','login_key_name':'','ssh_user':'railshot','identity_file':str(home/'guest_ed25519'),'public_key_file':str(home/'guest_ed25519.pub')},
        'nas':{'size_gib':500},'edge':{'certificate_no':'','base_domain':'','dns_config_file':str(home/'dns.json')},
        'application_template_file':str(home/'applications.json'),'database':{'server_spec_code':'','size_gib':100,'config_file':str(home/'postgres.json')},
        'budget':{'approved':False,'max_monthly_krw':0,'quote':{'account_id':'','region':'KR','spec_sha256':'','configuration_sha256':'','estimated_monthly_krw':0,'approved_at':'','expires_at':'','test_duration_seconds':3600,'max_applications':1,'data_retention':'retain'}}}
    return {'adapter':profile,'files':{'credentials.json':{'access_key':'','secret_key':'','member_no':''},
        'dns.json':{'domain_id':'','base_domain':'','credentials_file':str(home/'credentials.json')},
        'applications.json':{'version':1,'state_dir':str(home/'app-state'),'registry_file':'generated',
            'cd':{'version':1,'state_dir':str(home/'cd-state'),'repository':'','branch':'dev/v1.0.0','context':''},
            'environments':{'generated':{'provider':'ncp','tenant':'','source_repository':'','pull_secret_file':str(home/'pull.json'),
                'target':{'repo_url':'','architecture':'amd64','ingress_cidrs':[],'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'500m','memory':'512Mi'}}},'ingress':{'base_domain':'','ncp_route_config_file':'generated','observability_config_file':str(home/'observability.json'),'ci_credentials_file':str(home/'ci.json')}}}},
        'pull.json':{'auths':{'ghcr.io':{'auth':''}}},'ci.json':{'token':''},
        'observability.json':{'version':1,'owner':'','lifecycle':'shared','expires_at':'','state_dir':str(home/'observer-state'),
            'prometheus_url':'','observer_ip':'','observer_source_cidr':'','observer_registry_file':'','observer_target_id':'',
            'observer_directory':'','node_metrics_port':30100,'cluster_metrics_port':30101},
        'postgres.json':{'version':1,'client_cidrs':[],'tls':{'server_name':'','ca_file':'','certificate_file':'','key_file':''},
            'probe_password_file':str(home/'postgres-probe-password.txt'),'backup':{'type':'s3','bucket':'','prefix':'/railshot/postgres','region':'','endpoint':'','identity':'secret','retention_full':7,'credentials_file':str(home/'backup-credentials.json')},
            'restore_approval_file':str(home/'restore-approval.json'),'timeout_seconds':1800},
        'backup-credentials.json':{'access_key':'','secret_key':''},
        'restore-approval.json':{'approval_id':'','environment_id':'','backup_id':'','expires_at':''}},'notes':[
        'Public NAVER Cloud KR VPC only; use an approved same-zone NAS/node placement. Gov/financial/NKS are not this profile.',
        'Fill private executor-owned files outside the repository; never paste API/SSH/database credentials into chat.',
        'Bind access_key_sha256 to SHA256 of the enrolled access_key. Certificate memberNo must match member_no on live checks.',
        'Provide separate NCP login key name and guest SSH keypair. New host keys are individually generated, injected through initScript, pinned, then initScript is removed.',
        'Budget approval is required before apply. VM/public IP/NAS(minimum 500GB)/LB/storage/egress continue to incur charges; max_monthly_krw records approval, not a cloud billing cap.',
        'Standalone PostgreSQL config and durable data volume/backup references are supplied through the shared database runtime.']}

def template(value,replacements):
    if isinstance(value,dict):return {template(k,replacements):template(v,replacements) for k,v in value.items()}
    if isinstance(value,list):return [template(v,replacements) for v in value]
    if isinstance(value,str) and re.fullmatch(r'\$\{[a-z_]+\}',value):
        require(value[2:-1] in replacements,'NCP_TEMPLATE_REFERENCE_INVALID');return replacements[value[2:-1]]
    return value

class Adapter:
    def __init__(self,config):self.config=copy.deepcopy(config)

    def _client(self,context,*,valid_certificate=False):
        cfg=self.config
        require(context['account_id']==cfg.get('account_id') and context['credential_ref']==cfg.get('credential_ref'),'NCP_ACCOUNT_BINDING_MISMATCH')
        require(cfg.get('region')=='KR' and (not context.get('spec') or context['spec']['region']=='KR'),'NCP_REGION_UNSUPPORTED')
        client=Client(cfg['credentials_file']);require(client.fingerprint==cfg['access_key_sha256'],'NCP_CREDENTIAL_CHANGED')
        cert=client.account(cfg['edge']['certificate_no'],cfg['member_no'])
        if valid_certificate:
            require(not cert.get('revokeDate'),'NCP_CERTIFICATE_REVOKED')
            for key,compare in (('validStartDate',lambda t:t<=datetime.now(timezone.utc)),('validEndDate',lambda t:t>datetime.now(timezone.utc))):
                try:date=datetime.strptime(cert[key],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=9)))
                except (KeyError,TypeError,ValueError):raise ProviderError('NCP_CERTIFICATE_VALIDITY_UNPROVEN') from None
                require(compare(date),'NCP_CERTIFICATE_OUTSIDE_VALIDITY')
        require(cert.get('domainAddress')=='*.'+cfg['edge']['base_domain'],'NCP_WILDCARD_CERTIFICATE_REQUIRED')
        return client

    def connection(self,context):
        self._client(context);return {'status':'succeeded','connected':True,'provider':'ncp'}

    def catalog(self,context):
        client=self._client(context)
        return {'zones':client.call('vserver','getZoneList').get('zoneList',[]),
            'images':client.call('vserver','getServerImageList').get('serverImageList',[]),
            'sizes':client.call('vserver','getServerSpecList').get('serverSpecList',[])}

    def plan(self,spec,context):
        report=validate_config(self.config);require(report['valid'],'NCP_PROFILE_INCOMPLETE')
        client=self._client(context,valid_certificate=True);cfg=self.config
        zones=client.call('vserver','getZoneList').get('zoneList',[])
        require(any(z.get('zoneCode')==cfg['zone'] for z in zones),'NCP_ZONE_UNAVAILABLE')
        images=client.call('vserver','getServerImageList',{'serverImageNoList.1':cfg['nodes']['server_image_no']}).get('serverImageList',[])
        require(len(images)==1 and str(images[0].get('serverImageNo'))==cfg['nodes']['server_image_no']
            and images[0].get('osType',{}).get('code')=='UBUNTU' and '.UBNTU.SVR24.' in images[0].get('serverImageProductCode','') and images[0].get('cpuArchitectureType',{}).get('code')=='X86_64' and images[0].get('serverImageStatus',{}).get('code')=='CREAT' and images[0].get('hypervisorType',{}).get('code')=='KVM','NCP_UBUNTU_IMAGE_REQUIRED')
        for size in {cfg['nodes']['server_spec_code']} | ({cfg['database']['server_spec_code']} if spec['database']['mode']=='standalone' else set()):
            sizes=client.call('vserver','getServerSpecList',{'zoneCode':cfg['zone'],'serverImageNo':cfg['nodes']['server_image_no'],'serverSpecCodeList.1':size}).get('serverSpecList',[])
            require(len(sizes)==1 and sizes[0].get('serverSpecCode')==size and sizes[0].get('hypervisorType',{}).get('code')=='KVM' and sizes[0].get('cpuArchitectureType',{}).get('code')=='X86_64' and sizes[0].get('cpuCount',0)>=2 and sizes[0].get('memorySize',0)>=4*1024**3,'NCP_SERVER_SIZE_UNAVAILABLE')
        private_pub=subprocess.run(['ssh-keygen','-y','-f',cfg['nodes']['identity_file']],capture_output=True,timeout=10)
        require(private_pub.returncode==0 and private_pub.stdout.decode().strip().split()[:2]==key_file(cfg['nodes']['public_key_file']).decode().split()[:2],'NCP_GUEST_KEYPAIR_MISMATCH')
        require(spec['runtime']=={'kind':'kubeadm','control_planes':1,'workers':2},'NCP_RUNTIME_UNSUPPORTED')
        return {'status':'planned','changes':[{'kind':k,'action':'create','count':n} for k,n in
            [('vpc',1),('subnet',2),('route_table',1),('acg',1),('server',4 if spec['database']['mode']=='standalone' else 3),('nas',1)]],
            'budget':{'approval_required':not cfg.get('budget',{}).get('approved'),'estimate_verified':False},
            'readiness':{k:False for k in READINESS}}

    def _create(self,client,home,value,key,kind,params):
        family,action=KINDS[kind][:2]
        ident=once(client,home,value,key,family,action,params,kind=kind,sensitive=kind=='init_script')
        wait(client,resource(value,key));return ident

    def _network(self,client,home,value,context):
        cfg=self.config;name=context['environment_id'];short='rs-'+hashlib.sha256(name.encode()).hexdigest()[:20]
        vpc=self._create(client,home,value,'vpc','vpc',{'vpcName':short,'ipv4CidrBlock':cfg['network']['vpc_cidr']})
        acls=client.call('vpc','getNetworkAclList',{'vpcNo':vpc}).get('networkAclList',[])
        defaults=[a for a in acls if a.get('isDefault') is True and a.get('vpcNo')==vpc]
        require(len(defaults)==1,'NCP_DEFAULT_ACL_UNPROVEN')
        subnets={}
        for role,usage in (('vm','GEN'),('lb','LOADB')):
            subnets[role]=self._create(client,home,value,'subnet-'+role,'subnet',{'vpcNo':vpc,'zoneCode':cfg['zone'],
                'subnetName':short+'-'+role,'subnet':cfg['network'][role+'_subnet_cidr'],'networkAclNo':defaults[0]['networkAclNo'],
                'subnetTypeCode':'PUBLIC','usageTypeCode':usage})
        table=self._create(client,home,value,'route','route_table',{'vpcNo':vpc,'routeTableName':short,'supportedSubnetTypeCode':'PUBLIC'})
        once(client,home,value,'route-bind','vpc','addRouteTableSubnet',{'vpcNo':vpc,'routeTableNo':table,
            'subnetNoList.1':subnets['vm'],'subnetNoList.2':subnets['lb']})
        associated=client.call('vpc','getRouteTableSubnetList',{'routeTableNo':table})
        require(associated.get('totalRows')==2 and sorted(row['subnetNo'] for row in associated.get('subnetList',[]))==sorted(subnets.values()),'NCP_ROUTE_ASSOCIATION_UNPROVEN')
        routes=client.call('vpc','getRouteList',{'vpcNo':vpc,'routeTableNo':table}).get('routeList',[])
        require(any(r.get('destinationCidrBlock')=='0.0.0.0/0' and r.get('targetType',{}).get('code')=='IGW' for r in routes),'NCP_EGRESS_ROUTE_UNPROVEN')
        group=self._create(client,home,value,'acg','acg',{'vpcNo':vpc,'accessControlGroupName':short,
            'accessControlGroupDescription':name})
        profiles=read_private(cfg['application_template_file'])['environments']
        observer=read_private(next(iter(profiles.values()))['ingress']['observability_config_file'])
        source=observer.get('observer_source_cidr',observer['observer_ip']+'/32')
        require(ipaddress.IPv4Network(source).prefixlen==32 and ipaddress.IPv4Network(source).network_address.is_global,'NCP_OBSERVER_PUBLIC_SOURCE_REQUIRED')
        ports=[observer['node_metrics_port'],observer['cluster_metrics_port']]
        require(all(type(port) is int and 30000<=port<=32767 for port in ports) and len(set(ports))==2,'NCP_OBSERVER_PORT_INVALID')
        rules=[{'protocolTypeCode':'TCP','ipBlock':source,'portRange':str(port)} for port in ports]
        for port in ('22','6443'):rules.append({'protocolTypeCode':'TCP','ipBlock':cfg['network']['management_cidr'],'portRange':port})
        for protocol in ('TCP','UDP'):
            rules.append({'protocolTypeCode':protocol,'accessControlGroupSequence':group,'portRange':'1-65535'})
        rules.append({'protocolTypeCode':'ICMP','accessControlGroupSequence':group})
        rules.append({'protocolTypeCode':'TCP','ipBlock':cfg['network']['lb_subnet_cidr'],'portRange':'30000-32767'})
        outbound_rules=[{'protocolTypeCode':protocol,'ipBlock':'0.0.0.0/0',**({'portRange':'1-65535'} if protocol!='ICMP' else {})} for protocol in ('TCP','UDP','ICMP')]
        def normalized(row):
            return (row['protocolType']['code'],row.get('ipBlock',''),row.get('accessControlGroupSequence',''),row.get('portRange',''))
        for direction,expected_rules in (('INBND',rules),('OTBND',outbound_rules)):
            def observed_rules():
                observed=client.call('vserver','getAccessControlGroupRuleList',{'accessControlGroupNo':group,'accessControlGroupRuleTypeCode':direction})
                rows=observed.get('accessControlGroupRuleList',[])
                require(observed.get('totalRows')==len(rows) and all(row.get('accessControlGroupNo')==group and row.get('accessControlGroupRuleType',{}).get('code')==direction for row in rows),'NCP_ACG_RULE_RESPONSE_INVALID')
                return {normalized(row) for row in rows}
            expected={(rule['protocolTypeCode'],rule.get('ipBlock',''),rule.get('accessControlGroupSequence',''),rule.get('portRange','')):rule for rule in expected_rules}
            current=observed_rules();require(current<=set(expected),'NCP_ACG_FOREIGN_RULE')
            missing=[expected[key] for key in sorted(set(expected)-current)]
            if missing:
                params={'accessControlGroupNo':group}
                for i,rule in enumerate(missing,1):params.update({f'accessControlGroupRuleList.{i}.{k}':v for k,v in rule.items()})
                once(client,home,value,'acg-'+direction,'vserver','addAccessControlGroupInboundRule' if direction=='INBND' else 'addAccessControlGroupOutboundRule',params)
            require(observed_rules()==set(expected),'NCP_ACG_RULES_UNPROVEN')
        value['network']={'vpc_no':vpc,'vm_subnet_no':subnets['vm'],'lb_subnet_no':subnets['lb'],'acg_no':group,'route_table_no':table}
        save(home/'state.json',value)
        return short

    def _guest(self,client,home,value,context,role,short):
        cfg=self.config;step='server-'+role;network=value['network'];nodes=cfg['nodes']
        marker='railshot:'+context['environment_id']+':'+digest(context['owner_id'])[:20]+':'+role
        pub=key_file(nodes['public_key_file']).decode().strip()
        require(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?',pub),'NCP_GUEST_KEY_INVALID')
        host_pin=home/(role+'-hostkey.json')
        if not host_pin.exists():
            require('init-'+role not in value['steps'],'NCP_HOSTKEY_RECONCILIATION_REQUIRED')
            with tempfile.TemporaryDirectory(prefix='railshot-ncp-hostkey-',dir='/dev/shm') as temporary:
                path=Path(temporary)/'hostkey'
                result=subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(path)],capture_output=True,timeout=30)
                require(result.returncode==0,'NCP_HOSTKEY_GENERATION_FAILED')
                host_private=path.read_bytes();host_public=path.with_suffix('.pub').read_text().strip()
                # Fixed script only; values below are validated or base64. It does
                # not print credentials, private keys or shell-expanded input.
                user=nodes['ssh_user'];require(re.fullmatch(r'[a-z][a-z0-9_-]{0,30}',user) and user!='root')
                script='#!/bin/bash\nset -eu\numask 077\n'
                script+=f'id {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}\ninstall -d -m 0700 -o {user} -g {user} /home/{user}/.ssh\n'
                script+=f'printf %s {shlex.quote(base64.b64encode((pub+chr(10)).encode()).decode())} | base64 -d > /home/{user}/.ssh/authorized_keys\nchown {user}:{user} /home/{user}/.ssh/authorized_keys\n'
                script+=f'printf %s {shlex.quote(base64.b64encode(host_private).decode())} | base64 -d > /etc/ssh/ssh_host_ed25519_key\n'
                script+=f'printf %s {shlex.quote(base64.b64encode((host_public+chr(10)).encode()).decode())} | base64 -d > /etc/ssh/ssh_host_ed25519_key.pub\n'
                script+=f"printf '%s\\n' '{user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/railshot\nchmod 0440 /etc/sudoers.d/railshot\n"
                script+="mkdir -p /etc/ssh/sshd_config.d\nprintf '%s\\n' 'PasswordAuthentication no' 'PermitRootLogin no' 'HostKey /etc/ssh/ssh_host_ed25519_key' > /etc/ssh/sshd_config.d/00-railshot.conf\nsshd -t\nsystemctl restart ssh\n"
                script_no=self._create(client,home,value,'init-'+role,'init_script',{'initScriptName':short+'-'+digest(role)[:4],
                    'initScriptDescription':marker,'initScriptContent':script,'osTypeCode':'LNX'})
                save(host_pin,{'public_key':host_public,'init_script_no':script_no})
        host=read_private(host_pin)
        if step not in value['steps']:
            params={'vpcNo':network['vpc_no'],'subnetNo':network['vm_subnet_no'],'serverImageNo':nodes['server_image_no'],
                'serverSpecCode':cfg['database']['server_spec_code'] if role=='database' else nodes['server_spec_code'],
                'serverName':short+'-'+role,'serverDescription':marker,'serverCreateCount':1,
                'networkInterfaceList.1.networkInterfaceOrder':0,'networkInterfaceList.1.accessControlGroupNoList.1':network['acg_no'],
                'loginKeyName':nodes['login_key_name'],'initScriptNo':host['init_script_no'],'associateWithPublicIp':True,'feeSystemTypeCode':'MTRAT',
                'blockStorageMappingList.1.order':0,'blockStorageMappingList.1.blockStorageSize':50,
                'blockStorageMappingList.1.blockStorageVolumeTypeCode':'CB1'}
            if role=='database':
                params['networkInterfaceList.1.ip']=str(ipaddress.IPv4Network(cfg['network']['vm_subnet_cidr']).network_address+14)
            self._create(client,home,value,step,'server',params)
        row=wait(client,resource(value,step));require(row.get('vpcNo')==network['vpc_no'] and row.get('subnetNo')==network['vm_subnet_no'],'NCP_SERVER_NETWORK_CHANGED')
        public=str(ipaddress.IPv4Address(row['publicIp']));require(ipaddress.IPv4Address(public).is_global,'NCP_MANAGEMENT_PUBLIC_IP_REQUIRED')
        nics=row.get('networkInterfaceNoList',[]);require(len(nics)==1,'NCP_NIC_BINDING_CHANGED')
        result=client.call('vserver','getNetworkInterfaceList',{'networkInterfaceNoList.1':nics[0]})
        nic=result.get('networkInterfaceList',[]);require(len(nic)==1 and nic[0].get('instanceNo')==row['serverInstanceNo'] and nic[0].get('subnetNo')==network['vm_subnet_no'] and nic[0].get('accessControlGroupNoList')==[network['acg_no']],'NCP_NIC_OWNER_CHANGED')
        if role=='database':
            require(nic[0].get('ip')==str(ipaddress.IPv4Network(cfg['network']['vm_subnet_cidr']).network_address+14),'NCP_DATABASE_ADDRESS_CHANGED')
        require(nic[0].get('deleteOnTermination') is True,'NCP_NIC_AUTODELETE_REQUIRED')
        ip_result=client.call('vserver','getPublicIpInstanceList',{'publicIp':public,'vpcNo':network['vpc_no']})
        ips=ip_result.get('publicIpInstanceList',[])
        require(len(ips)==1 and ip_result.get('totalRows')==1 and ips[0].get('publicIp')==public and ips[0].get('serverInstanceNo')==row['serverInstanceNo'],'NCP_PUBLIC_IP_OWNER_CHANGED')
        public_no=ips[0]['publicIpInstanceNo']
        private=str(ipaddress.IPv4Address(nic[0]['ip']));require(ipaddress.IPv4Address(private) in ipaddress.IPv4Network(cfg['network']['vm_subnet_cidr']),'NCP_PRIVATE_IP_CHANGED')
        known=home/(role+'-known-hosts')
        content=private+' '+host['public_key']+'\n'
        if known.exists():require(key_file(known).decode()==content,'NCP_HOSTKEY_CHANGED')
        else:
            fd=os.open(known,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(content)
        ssh={'user':nodes['ssh_user'],'port':22,'identity_file':nodes['identity_file'],'known_hosts_file':str(known),'connect_host':public}
        argv=['ssh','-F','/dev/null','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
            '-o','UserKnownHostsFile='+str(known),'-o','HostKeyAlias='+private,'-o','ConnectTimeout=10','-i',nodes['identity_file'],
            nodes['ssh_user']+'@'+public,'/usr/bin/true']
        deadline=time.monotonic()+600
        while True:
            try:connected=subprocess.run(argv,capture_output=True,timeout=15).returncode==0
            except subprocess.TimeoutExpired:connected=False
            if connected:break
            require(time.monotonic()<deadline,'NCP_PINNED_SSH_UNREACHABLE');time.sleep(3)
        once(client,home,value,'remove-init-'+role,'vserver','deleteInitScripts',{'initScriptNoList.1':host['init_script_no']})
        require(observe(client,resource(value,'init-'+role),absent=True),'NCP_INIT_SCRIPT_REMOVAL_UNPROVEN')
        resource(value,'init-'+role)['status']='deleted'
        descriptor={'version':1,'provider':'ncp','member_no':cfg['member_no'],'environment_id':context['environment_id'],
            'resource_id':row['serverInstanceNo'],'vpc_no':network['vpc_no'],'subnet_no':network['vm_subnet_no'],
            'region':'KR','availability_zone':cfg['zone'],'private_ipv4':private,'public_ipv4':public,'ssh':ssh,
            'network_interface_no':nics[0],'public_ip_instance_no':public_no}
        path=home/(role+'-node.json');save(path,descriptor)
        value.setdefault('nodes',{})[role]=descriptor;save(home/'state.json',value)
        return {'id':context['environment_id']+'-'+role,'role':'control_plane' if role=='control' else 'workers',
                'availability_zone':cfg['zone'],'descriptor_file':str(path),'ssh':ssh}

    def _storage(self,client,home,value,context,short):
        marker='railshot:'+context['environment_id']+':'+digest(context['owner_id'])[:20]
        workers=[value['nodes'][r]['resource_id'] for r in ('worker-a','worker-b')]
        params={'zoneCode':self.config['zone'],'volumeAllotmentProtocolTypeCode':'NFS','volumeSize':self.config['nas']['size_gib'],
            'volumeName':'rs'+digest(context['environment_id'])[:16],'nasVolumeDescription':marker,
            'isEncryptedVolume':True,'isReturnProtection':True,'isAsync':True}
        for i,node in enumerate(workers,1):params.update({f'accessControlRuleList.{i}.serverInstanceNo':node,f'accessControlRuleList.{i}.writeAccess':True})
        ident=self._create(client,home,value,'nas','nas',params);row=observe(client,resource(value,'nas'))
        require(row.get('volumeAllotmentProtocolType',{}).get('code')=='NFS' and row.get('isEncryptedVolume') is True,'NCP_NAS_PROPERTIES_CHANGED')
        mount=row['mountInformation'].split(':',1);require(len(mount)==2,'NCP_NAS_MOUNT_INVALID')
        value['storage']={'backend':'nfs','mode':'reuse','binding':{'cluster_id':context['environment_id'],'provider':'ncp','account_id':self.config['member_no'],'region':'KR'},
            'credentials_file':self.config['credentials_file'],'ncp':{'nas_volume_instance_no':ident,'server':mount[0],'share':mount[1],
                'zone':self.config['zone'],'worker_instance_nos':workers,'owner_marker':marker}}
        save(home/'state.json',value)

    def _data_disk(self,client,home,value,context,short):
        if context['spec']['database']['mode']=='none':return
        cfg=self.config['database'];require(type(cfg['size_gib']) is int and 10<=cfg['size_gib']<=16380 and cfg['size_gib']%10==0,'NCP_DATABASE_VOLUME_SIZE_INVALID')
        if 'database-volume' not in value['steps']:
            self._create(client,home,value,'database-volume','block_storage',{'zoneCode':self.config['zone'],'blockStorageName':short+'-pg',
                'blockStorageVolumeTypeCode':'CB1','blockStorageSize':cfg['size_gib'],'blockStorageDescription':context['environment_id'],
                'isReturnProtection':True})
        item=resource(value,'database-volume');node=value['nodes']['database'];row=observe(client,item)
        if row.get('blockStorageInstanceStatus',{}).get('code')!='ATTAC':
            once(client,home,value,'database-volume-attach','vserver','attachBlockStorageInstance',{'serverInstanceNo':node['resource_id'],'blockStorageInstanceNo':item['id']})
        row=wait(client,item,code='ATTAC')
        require(str(row.get('serverInstanceNo'))==node['resource_id'] and re.fullmatch(r'/dev/(?:xvd|vd|sd)[b-z]',row.get('deviceName','')),'NCP_DATABASE_DEVICE_UNPROVEN')
        node['data_disk']={'resource_id':item['id'],'device':row['deviceName'],'mount_path':'/var/lib/postgresql'}
        save(home/'database-node.json',node);save(home/'state.json',value)
        from infrastructure.providers.ncp.volume import prepare
        prepare(home,value,node)

    def _database(self,home,value,context,operation='install'):
        if context['spec']['database']['mode']=='none':return
        sys.path.insert(0,str(ROOT/'infrastructure/ansible'))
        standalone=importlib.import_module('standalone')
        node=value['nodes']['database']
        descriptor={**node,'version':1,'account_id':context['account_id'],'environment_id':context['environment_id']}
        result=standalone.run(action=operation,descriptor=descriptor,config=read_private(self.config['database']['config_file']),context=context)
        value['readiness']['database']=result.get('ready') is True
        value['database_result']={k:result[k] for k in ('status','ready','binding_file') if k in result};save(home/'state.json',value)
        require(result.get('status')=='succeeded' and result.get('ready') is True,'NCP_DATABASE_NOT_READY')

    def _runtime(self,home,value,context,members):
        cfg=self.config;name=context['environment_id']
        edge={'version':1,'cluster_id':name,'provider':'ncp','member_no':cfg['member_no'],'credentials_file':cfg['credentials_file'],
            'access_key_sha256':cfg['access_key_sha256'],'region':'KR','zone':cfg['zone'],'vpc_no':value['network']['vpc_no'],
            'lb_subnet_no':value['network']['lb_subnet_no'],'worker_instance_nos':[value['nodes'][r]['resource_id'] for r in ('worker-a','worker-b')],
            'certificate_no':cfg['edge']['certificate_no'],'base_domain':cfg['edge']['base_domain'],'dns_config_file':cfg['edge']['dns_config_file'],
            'state_dir':str(home/'edge-routes'),'cost_approval':value['cost_approval']}
        edge_file=home/'edge.json';save(edge_file,edge)
        cluster={'version':1,'cluster_id':name,'provider':'ncp','account_id':cfg['member_no'],'region':'KR','members':members,
            'runtime':{'wait_timeout_seconds':600,'storage':value['storage'],'edge':str(edge_file)}}
        cluster_file=home/'cluster.json';save(cluster_file,cluster)
        sys.path.insert(0,str(ROOT/'infrastructure/ansible'));kubeadm=importlib.import_module('kubeadm')
        request=kubeadm.from_registry(cluster,request_id=context['operation_id'])
        result=kubeadm.run(request,state_dir=str(home/'runtime'))
        value['readiness'].update(cluster=result.get('runtime_ready') is True,storage=result.get('storage_ready') is True,public_access=result.get('edge_ready') is True)
        save(home/'state.json',value);require(result.get('status')=='succeeded','NCP_RUNTIME_NOT_READY')
        registry=home/'registry.json';save(registry,{'version':1,'targets':{name:{'profile':'kubeadm-1c2w','cluster_file':str(cluster_file),'purpose':'runtime',
            'management_endpoint':'https://'+value['nodes']['control']['public_ipv4']+':6443',
            'metrics_endpoints':{name+'-'+role:value['nodes'][role]['public_ipv4'] for role in ('control','worker-a','worker-b')}}}})
        app=template(read_private(cfg['application_template_file']),{'environment_id':name,'registry_file':str(registry),'state_dir':str(home)})
        profiles=app['environments'];require(len(profiles)==1,'NCP_APPLICATION_TEMPLATE_AMBIGUOUS')
        profile=next(iter(profiles.values()));app['environments']={name:profile};app['registry_file']=str(registry)
        profile['provider']='ncp';profile['ingress'].update(base_domain=cfg['edge']['base_domain'],ncp_route_config_file=str(edge_file),cluster_file=str(cluster_file))
        if context['spec']['database']['mode']=='standalone':
            standalone=importlib.import_module('standalone')
            node=value['nodes']['database']
            descriptor={**node,'version':1,'account_id':context['account_id'],'environment_id':name}
            profile['ingress']['standalone_database']=standalone.application_bundle(descriptor,cfg['database']['config_file'],context)
        app_file=home/'applications.json';save(app_file,app)
        value['application_config_path']=str(app_file);value['application_sha256']=digest(app)
        self._observe_runtime(home,value,context,initial=True)
        from .runtime import bootstrap
        result=bootstrap(str(app_file),context);require(result.get('status')=='succeeded','NCP_APPLICATION_BOOTSTRAP_FAILED')
        value['application_binding_receipt_path']=result['application_binding_receipt_path'];save(home/'state.json',value)

    def _observe_runtime(self,home,value,context,*,initial=False):
        sys.path.insert(0,str(ROOT/'infrastructure/ansible'));kubeadm=importlib.import_module('kubeadm');checker=importlib.import_module('kubeadm_readiness')
        request=kubeadm.from_registry(read_private(home/'cluster.json'),request_id=context.get('operation_id') or 'observe-'+context['environment_id'])
        kube=kubeadm.kubectl(request,{},kubeadm.ansible.child_env(),time.monotonic()+600)
        checker.check_nodes(kube('default','get','nodes','-o','json'),{'nodes':kubeadm.nodes(request),'version':'v'+kubeadm.VERSIONS['kubernetes'],
            'workers':[n['id'] for n in request['inventory']['workers']]})
        uid=kube('default','get','namespace','kube-system','-o','json')['metadata']['uid']
        pvc=kube('railshot-storage-proof','get','pvc','shared-filesystem-proof','-o','json')
        pv=kube('default','get','pv',pvc['spec']['volumeName'],'-o','json')
        identity={'cluster_uid':uid,'pvc_uid':pvc['metadata']['uid'],'pv_uid':pv['metadata']['uid']}
        require(initial or value.get('runtime_identity')==identity,'NCP_CLUSTER_IDENTITY_CHANGED')
        attrs=pv['spec']['csi']['volumeAttributes'];storage=value['storage']['ncp']
        require(pvc['status']['phase']=='Bound' and pvc['spec']['storageClassName']=='railshot-persistent'
            and pv['spec']['claimRef']['uid']==pvc['metadata']['uid'] and pv['spec']['csi']['driver']=='nfs.csi.k8s.io'
            and attrs.get('server')==storage['server'] and attrs.get('share')==storage['share'],'NCP_PVC_BINDING_CHANGED')
        workloads=kube('railshot-storage','get','deployment,daemonset','-o','json')['items']
        require(workloads and all(row['status'].get('observedGeneration',0)>=row['metadata']['generation']
            and (row['status'].get('readyReplicas',0)==row['spec'].get('replicas',1) if row['kind']=='Deployment'
            else row['status'].get('numberReady',0)==row['status'].get('desiredNumberScheduled',-1)) for row in workloads),'NCP_CSI_NOT_READY')
        require(digest(read_private(value['application_config_path']))==value['application_sha256'],'NCP_APPLICATION_CONFIG_CHANGED')
        from infrastructure.providers.ncp.edge import configure,configuration
        cfg=configuration(str(home/'edge.json'));route={'environment_id':cfg['cluster_id'],'application_id':'bootstrap-'+digest(cfg['cluster_id'])[:20],
            'hostname':'bootstrap-'+digest(cfg['cluster_id'])[:12]+'.'+cfg['base_domain'],'node_port':30080,'health_path':'/health'}
        edge=configure(str(home/'edge.json'),route,read_only=True)
        value['runtime_identity']=identity
        value['readiness'].update(cluster=True,storage=True,public_access=edge['edge_ready'],deployment_binding=False)

    def _result(self,value):
        return {k:copy.deepcopy(value[k]) for k in ('status','deleted','readiness','resources','retained','residuals','application_config_path','application_binding_receipt_path') if k in value}

    def apply(self,spec,context):
        self.plan(spec,context)
        from infrastructure.providers.ncp.budget import reserve,permit
        client=self._client(context)
        with state(self.config,context) as (home,value):
            require(all(step['status']=='succeeded' for step in value['steps'].values()),'NCP_RECONCILIATION_REQUIRED')
            reserve(self.config,spec,value);save(home/'state.json',value)
            short=self._network(client,home,value,context)
            members=[self._guest(client,home,value,context,r,short) for r in ('control','worker-a','worker-b')]
            if spec['database']['mode']=='standalone':self._guest(client,home,value,context,'database',short)
            value['readiness']['infrastructure']=True
            self._storage(client,home,value,context,short);self._data_disk(client,home,value,context,short)
            permit(value['cost_approval'])
            self._database(home,value,context);self._runtime(home,value,context,members)
            value['status']='succeeded';save(home/'state.json',value);return self._result(value)

    def describe(self,context):
        client=self._client(context)
        with state(self.config,context) as (home,value):
            value['readiness']={k:False for k in READINESS};value['status']='blocked'
            if any(s['status']=='unknown' for s in value['steps'].values()):
                value['status']='unknown';save(home/'state.json',value);return self._result(value)
            active=bool(value.get('nodes'))
            for item in value['resources']:
                if item['kind']=='server':active=active and observe(client,item).get('serverInstanceStatus',{}).get('code')=='RUN'
            value['readiness']['infrastructure']=active
            if active and value.get('application_config_path'):
                try:
                    self._observe_runtime(home,value,context);self._database(home,value,context,'status');value['status']='succeeded'
                except (OSError,ValueError,KeyError,ProviderError):value['status']='blocked'
            save(home/'state.json',value);return self._result(value)

    def _power(self,client,home,value,role,action,key):
        item=resource(value,'server-'+role);row=observe(client,item);desired='RUN' if action=='start' else 'NSTOP'
        if row.get('serverInstanceStatus',{}).get('code')!=desired:
            require(row.get('serverInstanceStatus',{}).get('code') in ('RUN','NSTOP') and row.get('serverInstanceOperation',{}).get('code')=='NULL','NCP_SERVER_TRANSITION_UNSAFE')
            once(client,home,value,key,'vserver',action+'ServerInstances',{'serverInstanceNoList.1':item['id']})
        wait(client,item,code=desired)

    def operate(self,operation,context):
        action=operation.get('action') if isinstance(operation,dict) else operation
        if action in ('verify','refresh'):return self.describe(context)
        client=self._client(context)
        with state(self.config,context) as (home,value):
            if action in ('backup','restore'):
                self._database(home,value,context,operation);return self._result(value)
            require(action in ('start','stop'),'NCP_OPERATION_UNSUPPORTED')
            if action=='start':
                from infrastructure.providers.ncp.budget import permit
                permit(value['cost_approval'])
            roles=['database','control','worker-a','worker-b']
            if action=='stop':roles.reverse()
            for role in roles:
                if role in value.get('nodes',{}):self._power(client,home,value,role,action,str(context['operation_id'])+'-'+action+'-'+role)
            value['readiness']={k:False for k in READINESS};value['readiness']['infrastructure']=action=='start'
            value['status']='succeeded';save(home/'state.json',value);return self._result(value)

    def _delete_links(self,client,home,value,role):
        node=value['nodes'][role];public=node['public_ip_instance_no']
        ips=client.call('vserver','getPublicIpInstanceList',{'publicIpInstanceNoList.1':public}).get('publicIpInstanceList',[])
        if ips:
            require(len(ips)==1 and ips[0].get('publicIp')==node['public_ipv4'] and not ips[0].get('serverInstanceNo'),'NCP_PUBLIC_IP_REASSIGNED')
            once(client,home,value,'delete-public-'+role,'vserver','deletePublicIpInstances',{'publicIpInstanceNoList.1':public})
        deadline=time.monotonic()+180
        while True:
            ips=client.call('vserver','getPublicIpInstanceList',{'publicIpInstanceNoList.1':public}).get('publicIpInstanceList',[])
            nics=client.call('vserver','getNetworkInterfaceList',{'networkInterfaceNoList.1':node['network_interface_no']}).get('networkInterfaceList',[])
            if not ips and not nics:return
            require(time.monotonic()<deadline,'NCP_VM_NETWORK_DELETE_PENDING');time.sleep(3)

    def delete(self,context):
        client=self._client(context)
        with state(self.config,context) as (home,value):
            from .runtime import teardown
            receipt=teardown(value['application_config_path'],context)
            require(receipt.get('status')=='succeeded' and receipt.get('environment_id')==context['environment_id']
                and receipt.get('data_policy')=='retain','NCP_RUNTIME_REVOCATION_REQUIRED')
            from infrastructure.providers.ncp.edge import configuration,revoke
            cfg=configuration(str(home/'edge.json'));root=private_dir(cfg['state_dir'])
            for path in root.glob('*/state.json'):
                recorded=read_private(path)
                if recorded.get('status')!='deleted':
                    require(recorded.get('request',{}).get('application_id','').startswith('bootstrap-'),'NCP_APPLICATION_ROUTES_REMAIN')
                    revoke(str(home/'edge.json'),recorded['request'])
            retained=[]
            has_db='database' in value.get('nodes',{})
            if has_db:self._power(client,home,value,'database','stop','retain-database-stop')
            # Preserve encrypted NAS and DB data disk. Until DB shutdown/detach
            # approval is in its shared runtime, keep stopped DB and its network.
            for item in reversed(value['resources']):
                kind=item['kind']
                if kind in ('nas','block_storage') or has_db and kind in ('vpc','subnet','route_table','acg') or item['stage']=='server-database':
                    item['status']='retained';retained.append(copy.deepcopy(item));continue
                if observe(client,item,absent=True):
                    if kind=='server':self._delete_links(client,home,value,item['stage'][7:])
                    item['status']='deleted';continue
                observe(client,item)
                if kind=='server':
                    role=item['stage'][7:];self._power(client,home,value,role,'stop','delete-stop-'+role)
                    action='terminateServerInstances';params={'serverInstanceNoList.1':item['id']}
                elif kind=='init_script':action='deleteInitScripts';params={'initScriptNoList.1':item['id']}
                elif kind=='acg':action='deleteAccessControlGroup';params={'vpcNo':value['network']['vpc_no'],'accessControlGroupNo':item['id']}
                elif kind=='route_table':
                    once(client,home,value,'remove-route-subnets','vpc','removeRouteTableSubnet',{'vpcNo':value['network']['vpc_no'],'routeTableNo':item['id'],
                        'subnetNoList.1':value['network']['vm_subnet_no'],'subnetNoList.2':value['network']['lb_subnet_no']})
                    action='deleteRouteTable';params={'vpcNo':value['network']['vpc_no'],'routeTableNo':item['id']}
                else:action='delete'+('Vpc' if kind=='vpc' else 'Subnet');params={KINDS[kind][4]:item['id']}
                once(client,home,value,'delete-'+item['stage'],KINDS[kind][0],action,params)
                wait(client,item,absent=True);item['status']='deleted';save(home/'state.json',value)
                if kind=='server':self._delete_links(client,home,value,role)
            if has_db:
                node=value['nodes']['database']
                retained.extend([{'kind':'public_ip','id':node['public_ip_instance_no'],'status':'retained'},{'kind':'network_interface','id':node['network_interface_no'],'status':'retained'}])
            value['retained']=retained+receipt.get('retained_resources',[]);value['residuals']=[]
            value['readiness']={k:False for k in READINESS};value['status']='succeeded';value['deleted']=True;save(home/'state.json',value)
            return self._result(value)
