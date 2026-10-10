"""Read-only capability evidence; listings never prove create permission."""
from .cli import ProviderError


def _probe(cli, args):
    try:
        result = cli.run(args)
        return {'status': 'query_succeeded', 'data': result, 'creation_verified': False}
    except ProviderError as exc:
        return {'status': 'unknown', 'reason': exc.code, 'creation_verified': False}


def discover_capabilities(cli):
    catalog = _probe(cli, ['catalog', 'list'])
    report = {'catalog': catalog, 'creation_verified': False, 'services': {}}
    if catalog['status'] != 'query_succeeded':
        return report
    types = {str(row.get('Type', row.get('type', ''))) for row in catalog['data']}
    probes = {
        'compute': (('compute',), {'flavors':['flavor','list'], 'servers':['server','list']}),
        'image': (('image',), {'images':['image','list','--status','active']}),
        'network': (('network',), {'networks':['network','list'], 'subnets':['subnet','list'], 'external_networks':['network','list','--external']}),
        'volume': (('volumev3','volumev2','block-storage'), {'volumes':['volume','list'], 'volume_types':['volume','type','list']}),
        'load_balancer': (('load-balancer',), {'load_balancers':['loadbalancer','list']}),
    }
    for name, (aliases, commands) in probes.items():
        if not types.intersection(aliases):
            report['services'][name] = {'status':'not_in_catalog', 'creation_verified':False}
        else:
            results = {key:_probe(cli, args) for key,args in commands.items()}
            report['services'][name] = {'status':'query_succeeded' if all(r['status']=='query_succeeded' for r in results.values()) else 'unknown', 'checks':results, 'creation_verified':False}
    report['quotas'] = _probe(cli, ['quota','show','--usage'])
    report['interpretation'] = 'Catalog absence is not proof of non-installation. Quota limits/usage and resource lists are evidence only; creation and VM access remain unverified.'
    return report
