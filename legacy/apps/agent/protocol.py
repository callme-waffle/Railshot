"""Versioned allowlisted protocol. No shell commands or credentials on the wire."""
import json
import re

MAX_REQUEST_BYTES = 16384
MAX_RESPONSE_BYTES = 1024 * 1024
COMMAND = 'jasmin-job-v1'
ERROR_CODES = frozenset({'invalid_request','request_too_large','command_rejected','credentials_unavailable',
                         'execution_failed','invalid_upstream_result','response_too_large','transport_failed'})


class ProtocolError(ValueError):
    def __init__(self, code='invalid_request'):
        self.code = code
        super().__init__(code)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError()
        result[key] = value
    return result


def decode_request(raw):
    if not isinstance(raw, bytes):
        raise ProtocolError()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError('request_too_large')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError() from None
    if not isinstance(value, dict) or set(value) != {'version','job_id','action','params'}:
        raise ProtocolError()
    if type(value['version']) is not int or value['version'] != 1:
        raise ProtocolError()
    if not isinstance(value['job_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value['job_id']):
        raise ProtocolError()
    if value['action'] != 'instance.list' or type(value['params']) is not dict or value['params']:
        raise ProtocolError()
    return value


def sanitize_instances(rows, secrets=()):
    if not isinstance(rows,list) or len(rows) > 10000:
        raise ProtocolError('invalid_upstream_result')
    result = []
    for row in rows:
        if not isinstance(row,dict):
            raise ProtocolError('invalid_upstream_result')
        item = {}
        for field, maximum in (('id',128),('name',255),('status',64)):
            value = row.get(field,row.get(field.upper() if field=='id' else field.capitalize()))
            if (not isinstance(value,str) or len(value)>maximum
                    or any(ord(char)<32 or ord(char)==127 for char in value)
                    or any(secret and secret in value for secret in secrets)):
                raise ProtocolError('invalid_upstream_result')
            item[field] = value
        result.append(item)
    return result


def success_response(request,result):
    response = {'version':1,'job_id':request['job_id'],'action':request['action'],
                'ok':True,'result':result,'error':None}
    if len(json.dumps(response).encode()) > MAX_RESPONSE_BYTES:
        raise ProtocolError('response_too_large')
    return response


def error_response(code,request=None):
    if code not in ERROR_CODES:
        code = 'execution_failed'
    return {'version':1,'job_id':request['job_id'] if request else None,
            'action':request['action'] if request else None,'ok':False,'result':None,'error':{'code':code}}
