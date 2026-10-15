"""Version one provider port shared by cloud and on-premises implementations.

Adapters receive only operator-controlled config. ``context`` binds every native
resource to an authenticated owner, a registered account and an environment.
Methods: connection(context), catalog(context), plan(spec, context),
apply(spec, context), describe(context), operate(operation, context), delete(context).
A mutation must return status succeeded/blocked/unknown. Readiness is evidence,
not intended configuration. Never print credentials or raw subprocess errors.
"""
import re

PROVIDERS = {'aws', 'gcp', 'ncp', 'openstack'}
REGIONS = {'aws': 'ap-northeast-2', 'gcp': 'asia-northeast3', 'ncp': 'KR'}
READINESS = ('infrastructure', 'cluster', 'storage', 'public_access', 'database', 'deployment_binding', 'application_http')
REQUIRED_READINESS = ('infrastructure', 'cluster', 'storage', 'database', 'deployment_binding')
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')


class ProviderError(Exception):
    def __init__(self, code, status=409, outcome_unknown=False):
        self.code = code if re.fullmatch(r'[A-Z][A-Z0-9_]{1,95}', str(code)) else 'PROVIDER_FAILED'
        self.status = status
        self.outcome_unknown = outcome_unknown
        super().__init__(self.code)


def identifier(value):
    return isinstance(value, str) and bool(ID.fullmatch(value))


def validate_spec(spec):
    required = {'version', 'name', 'provider', 'account_id', 'region', 'scope', 'runtime', 'database'}
    if (not isinstance(spec, dict) or not required <= spec.keys()
            or spec.keys() - required - {'application_name'} or type(spec['version']) is not int or spec['version'] != 1
            or not re.fullmatch(r'[a-z][a-z0-9-]{1,38}[a-z0-9]', str(spec['name']))
            or not isinstance(spec['provider'], str) or spec['provider'] not in PROVIDERS or not identifier(spec['account_id'])
            or not identifier(spec['region']) or not isinstance(spec['scope'], str) or spec['scope'] not in {'shared', 'dedicated'}
            or not isinstance(spec['runtime'], dict)
            or any(type(spec['runtime'].get(key)) is not int for key in ('control_planes', 'workers'))
            or spec['runtime'] != {'kind': 'kubeadm', 'control_planes': 1, 'workers': 2}
            or spec['database'] not in ({'mode': 'none'}, {'mode': 'standalone'})
            or (spec['scope'] == 'dedicated' and not re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', str(spec.get('application_name', ''))))
            or (spec['scope'] == 'shared' and 'application_name' in spec)):
        raise ProviderError('INVALID_ENVIRONMENT_SPEC', 422)
    if spec['provider'] in REGIONS and spec['region'] != REGIONS[spec['provider']]:
        raise ProviderError('REGION_UNSUPPORTED', 422)
    return spec


def ready(result, spec):
    evidence = result.get('readiness', {})
    return result.get('status') == 'succeeded' and all(
        evidence.get(key) is True for key in REQUIRED_READINESS
        if key != 'database' or spec['database']['mode'] != 'none')
