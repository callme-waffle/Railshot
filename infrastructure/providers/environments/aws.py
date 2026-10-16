"""AWS registry entry; translate native failures to the shared error contract."""
from infrastructure.providers.aws.adapter import Adapter as NativeAdapter
from infrastructure.providers.aws.native import AwsError
from infrastructure.providers.aws.config import validate_config, config_template


class Adapter(NativeAdapter):
    pass


def _port(name):
    def invoke(self, *args, **kwargs):
        from .contract import ProviderError
        try:
            return getattr(NativeAdapter, name)(self, *args, **kwargs)
        except AwsError as error:
            raise ProviderError(error.code, error.status, error.outcome_unknown) from None
    invoke.__name__ = name
    return invoke


for _name in ('connection', 'catalog', 'plan', 'apply', 'describe', 'operate', 'delete'):
    setattr(Adapter, _name, _port(_name))
