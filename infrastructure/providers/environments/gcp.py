"""GCP implementation of the environment control port."""
from infrastructure.providers.gcp.adapter import Adapter as NativeAdapter
from infrastructure.providers.gcp.native import GcpError
from infrastructure.providers.gcp.config import config_template, validate_config

class Adapter(NativeAdapter):
    pass

def _port(name):
    def invoke(self, *args, **kwargs):
        from .contract import ProviderError
        try:
            return getattr(NativeAdapter, name)(self, *args, **kwargs)
        except GcpError as error:
            raise ProviderError(error.code, error.status, error.outcome_unknown) from None
    return invoke

for _name in ('connection', 'catalog', 'plan', 'apply', 'describe', 'operate', 'delete'):
    setattr(Adapter, _name, _port(_name))
