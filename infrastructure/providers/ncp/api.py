"""Fixed-endpoint NCP API Gateway HMAC transport; never expose signed requests."""
import base64
import hashlib
import hmac
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from infrastructure.providers.environments.contract import ProviderError
from infrastructure.providers.environments.control import read_private

HOSTS = {'cloud': 'https://ncloud.apigw.ntruss.com',
         'certificate': 'https://certificatemanager.apigw.ntruss.com',
         'dns': 'https://globaldns.apigw.ntruss.com'}


def require(ok, code='NCP_CONFIGURATION_INVALID'):
    if not ok:
        raise ProviderError(code)


def signature(method, uri, timestamp, access_key, secret_key):
    message = f'{method} {uri}\n{timestamp}\n{access_key}'.encode()
    return base64.b64encode(hmac.new(secret_key.encode(), message, hashlib.sha256).digest()).decode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProviderError('NCP_REDIRECT_REFUSED', outcome_unknown=True)


class Client:
    def __init__(self, credentials_file, *, opener=None, clock=time.time):
        data = read_private(credentials_file)
        require(set(data) == {'access_key', 'secret_key', 'member_no'}
                and all(isinstance(v, str) and v for v in data.values()), 'NCP_CREDENTIALS_INVALID')
        require(re.fullmatch(r'[0-9]+', data['member_no']), 'NCP_MEMBER_INVALID')
        self.credentials = data
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    @property
    def fingerprint(self):
        return hashlib.sha256(self.credentials['access_key'].encode()).hexdigest()

    def request(self, service, path, params=None, *, method='GET', document=None, mutation=False):
        require(service in HOSTS and re.fullmatch(r'/[A-Za-z0-9/_-]+', path), 'NCP_ENDPOINT_INVALID')
        require(method in ('GET', 'POST', 'PUT', 'DELETE'), 'NCP_METHOD_INVALID')
        params = params or {}
        require(all(isinstance(k, str) and isinstance(v, (str, int, bool)) for k, v in params.items()), 'NCP_PARAMETERS_INVALID')
        values = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()}
        query = urllib.parse.urlencode(sorted(values.items()))
        body = None
        uri = path
        content = 'application/json'
        if method == 'POST' and document is None:
            body = query.encode(); content = 'application/x-www-form-urlencoded'
        elif query:
            uri += '?' + query
        if document is not None:
            body = json.dumps(document, separators=(',', ':'), allow_nan=False).encode()
        timestamp = str(int(self.clock() * 1000))
        headers = {'x-ncp-apigw-timestamp': timestamp, 'x-ncp-iam-access-key': self.credentials['access_key'],
            'x-ncp-apigw-signature-v2': signature(method, uri, timestamp, self.credentials['access_key'], self.credentials['secret_key']),
            'Accept': 'application/json', 'Content-Type': content}
        request = urllib.request.Request(HOSTS[service] + uri, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=60) as response:
                require(response.status in (200,201,202,204), 'NCP_HTTP_STATUS_INVALID')
                raw = response.read(4_000_001)
            require(len(raw) <= 4_000_000, 'NCP_RESPONSE_TOO_LARGE')
            result = json.loads(raw) if raw.strip() else {}
            require(isinstance(result, (dict, list)), 'NCP_RESPONSE_INVALID')
            return result
        except urllib.error.HTTPError as error:
            code = 'NCP_UNAUTHORIZED' if error.code in (401, 403) else 'NCP_RATE_LIMITED' if error.code == 429 else 'NCP_HTTP_FAILED'
            raise ProviderError(code, outcome_unknown=mutation and error.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError):
            raise ProviderError('NCP_TRANSPORT_FAILED', outcome_unknown=mutation) from None

    def call(self, family, action, params=None, *, mutation=False, sensitive=False):
        require(family in ('vpc', 'vserver', 'vnas', 'vloadbalancer') and re.fullmatch(r'[a-z][A-Za-z]+', action))
        payload = self.request('cloud', f'/{family}/v2/{action}',
            {'regionCode': 'KR', 'responseFormatType': 'json', **(params or {})},
            method='POST' if sensitive else 'GET', mutation=mutation)
        result = payload.get(action + 'Response', {})
        require(isinstance(result, dict), 'NCP_RESPONSE_INVALID')
        if str(result.get('returnCode')) != '0':
            raise ProviderError('NCP_NATIVE_REJECTED', outcome_unknown=mutation)
        return result

    def account(self, certificate_no, member_no):
        result = self.request('certificate', '/api/v1/certificates', {'certificateNo': certificate_no})
        require(str(result.get('returnCode')) == '0', 'NCP_CERTIFICATE_QUERY_FAILED')
        rows = result.get('sslCertificateList', [])
        require(len(rows) == 1 and str(rows[0].get('certificateNo')) == str(certificate_no)
                and str(rows[0].get('memberNo')) == member_no == self.credentials['member_no'], 'NCP_ACCOUNT_MISMATCH')
        return rows[0]
