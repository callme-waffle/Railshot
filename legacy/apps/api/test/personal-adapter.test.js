import test from 'node:test';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPersonalAdapter } from '../src/personal-adapter.js';
import { createServer } from 'node:http';

async function fixture(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'personal-native-'))); t.after(() => rm(root, { recursive:true, force:true }));
  const configPath=join(root,'config.json');
  await writeFile(configPath,JSON.stringify({version:1,public_url:'https://api.example.test',installer_url:'https://release.example.test/install.sh',artifact_url:'https://release.example.test/client.tgz',artifact_sha256:'a'.repeat(64),gateway:{config_path:'/etc/railshot/gateway.json',command:'/usr/local/sbin/railshot-personal-gateway'}}),{mode:0o600});
  const calls=[], ssh=[];
  const runner=async(command,args,options)=>{const request=JSON.parse(await readFile(args[args.indexOf('--request')+1],'utf8'));calls.push({command,args,options,request});return {status:'succeeded',reachable:true,tunnel:{address:'10.80.0.2/32',server_public_key:'B'.repeat(43)+'=',endpoint:'gateway.example.test:51820',allowed_ips:'10.80.0.1/32'}};};
  const transport=async(command,args,options)=>{const request=JSON.parse(options.input);ssh.push({command,args,request});return JSON.stringify({version:1,job_id:request.job_id,action:request.action,ok:true,result:[],error:null});};
  const options={configPath,stateDirectory:join(root,'personal'),runner,transport,env:{}};
  const target={id:'personal-11111111-1111-4111-8111-111111111111',generation:1,project_id:'project-one',public_key:'A'.repeat(43)+'=',runtime:{ssh_host_key:'ssh-ed25519 '+'A'.repeat(68)},tunnel:{address:'10.80.0.2/32',allowed_ips:'10.80.0.1/32'}};
  return {root,options,target,calls,ssh};
}

test('HTTP endpoints require explicit operator test mode and never weaken URL validation',async(t)=>{
  const f=await fixture(t), original=JSON.parse(await readFile(f.options.configPath,'utf8'));
  const configure=async(value)=>writeFile(f.options.configPath,JSON.stringify(value),{mode:0o600});
  assert.equal((await createPersonalAdapter(f.options)).config.test_allow_http,false);
  for(const key of ['public_url','installer_url','artifact_url']){
    await configure({...original,[key]:original[key].replace('https:','http:'),test_allow_http:true});
    await assert.rejects(()=>createPersonalAdapter(f.options),/require HTTPS/,'config field alone cannot enable HTTP');
    await assert.rejects(()=>createPersonalAdapter({...f.options,env:{RAILSHOT_PERSONAL_TEST_ALLOW_HTTP:'0'}}),/require HTTPS/);
    assert.equal((await createPersonalAdapter({...f.options,env:{RAILSHOT_PERSONAL_TEST_ALLOW_HTTP:'1'}})).config.test_allow_http,true);
  }
  for(const value of ['true','yes','',1]){
    await assert.rejects(()=>createPersonalAdapter({...f.options,env:{RAILSHOT_PERSONAL_TEST_ALLOW_HTTP:value}}),/must be 0 or 1/);
  }
  for(const public_url of ['ftp://api.example.test','http://user:password@api.example.test','http://api.example.test/?token=secret','http://api.example.test/#fragment',' http://api.example.test']){
    await configure({...original,public_url});
    await assert.rejects(()=>createPersonalAdapter({...f.options,env:{RAILSHOT_PERSONAL_TEST_ALLOW_HTTP:'1'}}),/credential-free URLs/);
  }
});

test('registration pins SSH identity and verifies OpenStack CLI without a profile or K3s runtime',async(t)=>{
  const f=await fixture(t), adapter=await createPersonalAdapter(f.options);
  await adapter.register(f.target);assert.equal(f.calls[0].command,'/usr/bin/sudo');assert.equal(f.calls[0].request.target_id,f.target.id);
  assert.equal(await adapter.prepare(f.target),false,'app deployment is separate from CLI connectivity');
  assert.match(adapter.runtimeAccess(f.target).public_key,/^ssh-ed25519 /);
  const verification=await adapter.verify(f.target);assert.equal(verification.openstack_verified,true);
  assert.deepEqual(f.ssh[0].request.params.argv,['server','list']);assert.equal(f.ssh[0].args.at(-1),'railshot-openstack-v1');
  assert.ok(f.ssh[0].args.includes('railshot-openstack@10.80.0.2'));assert.ok(f.ssh[0].args.includes('IdentityAgent=none'));
  assert.ok(f.ssh[0].args.includes('10.80.0.1'));assert.match(await readFile(join(f.root,'personal',f.target.id,'known_hosts'),'utf8'),/^\[10\.80\.0\.2\]:2222 ssh-ed25519 /);
  await adapter.prepare(f.target);
  await assert.rejects(()=>adapter.prepare({...f.target,runtime:{ssh_host_key:'ssh-ed25519 '+'B'.repeat(68)}}),/identity changed/);
});

test('response binding, output bounds and CLI failures cannot fabricate a verified connection',async(t)=>{
  const f=await fixture(t);
  for(const transport of [async()=>JSON.stringify({version:1,job_id:'foreign',action:'openstack.execute',ok:true,result:[],error:null}),async()=> 'x'.repeat(1024*1024+1),async()=>{throw new Error('timeout');}]){
    const adapter=await createPersonalAdapter({...f.options,transport}); await adapter.prepare(f.target);
    assert.equal((await adapter.verify(f.target)).openstack_verified,false);
  }
  const adapter=await createPersonalAdapter(f.options);
  await assert.rejects(()=>adapter.execute(f.target,{job_id:'unsafe;job',argv:['server','list']}),/Invalid/);
  const result=await adapter.execute(f.target,{job_id:'fixed-job',argv:['server','delete','owned-id'],delete_data:true});
  assert.equal(result.job_id,'fixed-job');assert.deepEqual(f.ssh.at(-1).request.params,{argv:['server','delete','owned-id'],delete_data:true});
});

test('normal shared cloud applications keep using the existing common adapter', async(t) => {
  const f = await fixture(t), calls = [];
  const base = { targets: { aws: { provider: 'aws', automaticDelivery: true } },
    describe: (id, app) => ({ id: `common-${id}-${app}`, environment_target_id: id }),
    register: async (app) => { calls.push(app.id); return { ...app, status: 'ready' }; },
    observePublished: async (app) => ({ application_id: app.id, public_http: { state: 'succeeded' } }) };
  const adapter = await createPersonalAdapter({ ...f.options, base });
  assert.equal(adapter.application.targets.aws.provider, 'aws');
  const app = adapter.application.describe('aws', 'demo');
  assert.equal((await adapter.application.register(app)).status, 'ready');
  assert.deepEqual(calls, ['common-aws-demo']);
  assert.equal((await adapter.application.observePublished(app)).public_http.state, 'succeeded');
});


test('server adapter exchanges real JSON bytes with the customer Python CLI protocol (cloud calls simulated)',async(t)=>{
  const f=await fixture(t), script=join(f.root,'protocol-fixture.py');
  await writeFile(script,`import sys, types, json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
stub=types.ModuleType('client_setup.credentials'); stub.CredentialStore=object
sys.modules['client_setup.credentials']=stub
from apps.agent.openstack_control import execute_raw
class Cloud:
    def __init__(self,auth): pass
    def run(self,argv):
        if argv == ['token','issue']: return {'project_id':'project-one','id':'never-publish-this-token'}
        if argv == ['server','list']: return [{'ID':'server-one','Name':'customer-node','Status':'ACTIVE'}]
        raise RuntimeError('cloud call intentionally not implemented')
result=execute_raw(sys.stdin.buffer.read(),sys.argv[3],lambda:{'password':'never-publish-this-password'}, {'project_id':'project-one','target_id':sys.argv[4]},cli_factory=Cloud,home=Path(sys.argv[2]))
print(json.dumps(result)); sys.exit(0 if result['ok'] else 1)
`,{mode:0o600});
  const transport=(_command,args,options)=>new Promise((resolve,reject)=>{
    const child=spawn(process.env.RAILSHOT_TEST_PYTHON || 'python3',[script,fileURLToPath(new URL('../../../',import.meta.url)),join(f.root,'cli'),args.at(-1),f.target.id],{stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='';child.stdout.on('data',(b)=>stdout+=b);child.stderr.on('data',(b)=>stderr+=b);child.on('error',reject);child.on('close',(code)=>{if(code>1)reject(new Error(stderr));else resolve({stdout,code});});child.stdin.end(options.input);
  });
  const adapter=await createPersonalAdapter({...f.options,transport});await adapter.prepare(f.target);
  assert.equal((await adapter.verify(f.target)).openstack_verified,true);
  const success=await adapter.execute(f.target,{job_id:'protocol-read',argv:['server','list']});assert.equal(success.result[0].ID,'server-one');
  await assert.rejects(()=>adapter.execute(f.target,{job_id:'protocol-denied',argv:['server','list','--os-password','secret']}),/response binding invalid/);
  assert.ok(!JSON.stringify(success).includes('never-publish'));
});


test('runtime binding requires authoritative cloud scope and a fresh native receipt before dynamic application routing', async(t)=>{
  const f=await fixture(t), common=join(f.root,'runtime.json');
  const config=JSON.parse(await readFile(f.options.configPath,'utf8'));
  await writeFile(common,JSON.stringify({state_dir:join(f.root,'bindings')}),{mode:0o600});
  await writeFile(f.options.configPath,JSON.stringify({...config,runtime:{config_path:common}}),{mode:0o600});
  let project='project-one', portProject='project-one', portDevice='vm-one', nativeResult, lastRequest, factoryCalls=0, calls=0;
  const cloudCalls=[];
  const evidence={resource_id:'vm-one',private_ipv4:'10.0.0.2',management_network:'private',placement:'nova',architecture:'amd64',initialization:'preconfigured',ssh_user:'railshot-runtime',ssh_port:2223,ssh_host_key:'ssh-ed25519 '+'B'.repeat(68),server:{project_id:'untrusted'}};
  const options={...f.options,service:{identity:{tenant:'demo',sourceRepository:'owner/source'},publishedFiles:async()=>[]},
    transport:async(_cmd,_args,options)=>{const q=JSON.parse(options.input), argv=q.params.argv;
      cloudCalls.push(argv);
      const result=argv[0]==='port'?(argv[1]==='list'?[{ID:'port-one','Fixed IP Addresses':[{ip_address:'10.0.0.2'}]}]
        :{id:'port-one',project_id:portProject,device_id:portDevice,fixed_ips:[{ip_address:'10.0.0.2'}],security_group_ids:['sg-one']})
        :{id:'vm-one',project_id:project,status:'ACTIVE',addresses:{private:['10.0.0.2']}};
      return JSON.stringify({version:1,job_id:q.job_id,action:q.action,ok:true,error:null,result});},
    runner:async(cmd,args,options)=>{if(!args[0]?.endsWith('/personal_runtime.py'))return f.options.runner(cmd,args,options);calls++;lastRequest=JSON.parse(await readFile(args.at(-1),'utf8'));return nativeResult;},
    applicationFactory:async({configPath})=>{factoryCalls++;assert.equal(configPath,join(f.root,'bindings',f.target.id,'applications.json'));return {targets:{[f.target.id]:{automaticDelivery:true}},register:async(app)=>({routed:app.id}),describe:()=>({id:f.target.id})};}};
  const adapter=await createPersonalAdapter(options);await adapter.prepare(f.target);
  assert.equal(adapter.deploymentReady(f.target),false);
  project='foreign';assert.deepEqual((await adapter.prepareRuntime(f.target,evidence)).blockers,['RUNTIME_RESOURCE_MISMATCH']);assert.equal(calls,0);
  project='project-one';nativeResult={target_id:f.target.id,generation:1,status:'succeeded',application_config_path:join(f.root,'bindings',f.target.id,'applications.json'),binding_sha256:'a'.repeat(64),verified_at:new Date().toISOString()};
  assert.equal((await adapter.prepareRuntime(f.target,evidence)).status,'succeeded');
  assert.equal(lastRequest.evidence.server.project_id,'project-one');assert.equal(lastRequest.evidence.ssh_host_key,undefined);
  assert.deepEqual(lastRequest.evidence.provider_binding,{port_id:'port-one',security_group_id:'sg-one'});
  assert.ok(cloudCalls.some((argv)=>JSON.stringify(argv)==='["port","show","port-one"]'));
  assert.equal(await readFile(lastRequest.known_hosts_file,'utf8'),'10.0.0.2 '+evidence.ssh_host_key+'\n');
  assert.equal(adapter.deploymentReady(f.target),true);assert.deepEqual(await adapter.application.register({id:'app-one',environment_target_id:f.target.id}),{routed:'app-one'});
  nativeResult={...nativeResult,target_id:'foreign'};await assert.rejects(()=>adapter.verifyRuntime(f.target,evidence),/binding invalid/);
  nativeResult={...nativeResult,target_id:f.target.id,verified_at:'2020-01-01T00:00:00Z'};await assert.rejects(()=>adapter.verifyRuntime(f.target,evidence),/receipt invalid/);
  assert.equal(factoryCalls,1);
  nativeResult={target_id:f.target.id,generation:1,status:'blocked',stage:'permission_verification',cluster_verified:true,blockers:[{code:'RUNTIME_DEPLOYMENT_PERMISSION_MISMATCH'}]};
  assert.equal((await adapter.verifyRuntime(f.target,evidence)).cluster_verified,true);assert.equal(adapter.deploymentReady(f.target),false);
  const before=calls;
  portProject='foreign';assert.deepEqual((await adapter.prepareRuntime(f.target,evidence)).blockers,['RUNTIME_NETWORK_BINDING_UNVERIFIED']);
  portProject='project-one';portDevice='other-vm';assert.deepEqual((await adapter.prepareRuntime(f.target,evidence)).blockers,['RUNTIME_NETWORK_BINDING_UNVERIFIED']);
  assert.equal(calls,before,'summary never substitutes for project-bound detail');
});

test('isolated gateway socket authenticates bounded tokens and readiness is a read-only health request', async(t)=>{
  const f=await fixture(t), folder=await realpath(await mkdtemp('/tmp/rs-gw-'));
  t.after(()=>rm(folder,{recursive:true,force:true}));
  const socket=join(folder,'gateway.sock'), tokenFile=join(f.root,'gateway-token'), requests=[];
  let expectedToken;
  const server=createServer(async(request,response)=>{
    assert.equal(request.headers.authorization,'Bearer '+expectedToken);
    let body='';for await(const chunk of request)body+=chunk;
    requests.push({method:request.method,path:request.url,body});
    response.setHeader('Content-Type','application/json');
    response.end(JSON.stringify(request.url==='/healthz'?{status:'ready'}:{status:'succeeded'}));
  });
  await new Promise(resolve=>server.listen(socket,resolve));t.after(()=>new Promise(resolve=>server.close(resolve)));
  const original=JSON.parse(await readFile(f.options.configPath,'utf8'));
  await writeFile(f.options.configPath,JSON.stringify({...original,gateway:{config_path:'/etc/railshot/gateway.json',socket_path:socket,token_file:tokenFile}}));
  const adapter=await createPersonalAdapter(f.options);
  for(const token of ['A'.repeat(32),'A'.repeat(508)+'._~-']){
    expectedToken=token;await writeFile(tokenFile,token,{mode:0o600});
    assert.equal((await adapter.register(f.target)).status,'succeeded');
  }
  const ready=await adapter.readiness();
  assert.ok(!ready.blockers.some(row=>row.code==='PERSONAL_GATEWAY_UNAVAILABLE'));
  assert.deepEqual(requests.at(-1),{method:'GET',path:'/healthz',body:''});
  const count=requests.length;
  for(const token of ['A'.repeat(31),'A'.repeat(513),'A'.repeat(32)+' secret']){
    await writeFile(tokenFile,token);await assert.rejects(()=>adapter.register(f.target),/Invalid gateway token/);
  }
  assert.equal(requests.length,count);
});
