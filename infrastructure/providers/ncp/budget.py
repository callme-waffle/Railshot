"""Operator-reviewed NCP estimate reservation; not a provider billing cap."""
from datetime import datetime, timezone, timedelta
import copy
from infrastructure.providers.environments.control import digest
from .api import require


def utc(value):
    require(isinstance(value,str) and value.endswith('Z'),'NCP_COST_TIMESTAMP_INVALID')
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def validate(config,spec):
    budget=config.get('budget',{});quote=budget.get('quote',{})
    require(budget.get('approved') is True and type(budget.get('max_monthly_krw')) is int and budget['max_monthly_krw']>0,'NCP_COST_APPROVAL_REQUIRED')
    require(set(quote)=={'account_id','region','spec_sha256','configuration_sha256','estimated_monthly_krw','approved_at','expires_at','test_duration_seconds','max_applications','data_retention'},'NCP_COST_QUOTE_REQUIRED')
    inputs={key:value for key,value in config.items() if key!='budget'}
    require(quote['account_id']==config['account_id']==spec['account_id'] and quote['region']==spec['region']=='KR'
        and quote['spec_sha256']==digest(spec) and quote['configuration_sha256']==digest(inputs),'NCP_COST_QUOTE_SCOPE_CHANGED')
    require(type(quote['estimated_monthly_krw']) is int and 0<quote['estimated_monthly_krw']<=budget['max_monthly_krw'],'NCP_COST_LIMIT_EXCEEDED')
    require(type(quote['test_duration_seconds']) is int and 60<=quote['test_duration_seconds']<=43200
        and type(quote['max_applications']) is int and 1<=quote['max_applications']<=20
        and quote['data_retention']=='retain','NCP_COST_QUOTE_INVALID')
    approved=utc(quote['approved_at']);expiry=utc(quote['expires_at']);now=datetime.now(timezone.utc)
    require(approved<=now<expiry and expiry-approved<=timedelta(hours=24),'NCP_COST_APPROVAL_EXPIRED')
    return quote


def reserve(config,spec,value):
    quote=validate(config,spec);now=datetime.now(timezone.utc)
    if 'cost_approval' not in value:
        value['cost_approval']={'quote_sha256':digest(quote),'deadline':min(utc(quote['expires_at']),now+timedelta(seconds=quote['test_duration_seconds'])).isoformat().replace('+00:00','Z'),
            'estimated_monthly_krw':quote['estimated_monthly_krw'],'max_applications':quote['max_applications'],'data_retention':'retain'}
    require(value['cost_approval']['quote_sha256']==digest(quote),'NCP_COST_RESERVATION_CHANGED')
    permit(value['cost_approval']);return value['cost_approval']


def permit(reservation):
    require(datetime.now(timezone.utc)<utc(reservation['deadline']),'NCP_TEST_WINDOW_EXPIRED')
