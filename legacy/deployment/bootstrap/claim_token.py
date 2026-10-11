"""Submit a one-time Railshot linkage token from the customer node."""

import argparse
import getpass
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def service_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise argparse.ArgumentTypeError("서비스 주소는 경로·인증정보 없는 HTTPS 주소여야 합니다.")
    return value.rstrip("/")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Railshot 일회성 연계 토큰 접수")
    parser.add_argument("--service-url", type=service_url, required=True)
    parser.add_argument("--ca-file", help="사설 CA 인증서 경로")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.error("토큰은 대화형 터미널에서만 입력할 수 있습니다.")
    token = getpass.getpass("일회성 연계 토큰: ")
    if not re.fullmatch(r"rsl_[A-Za-z0-9_-]{43}", token):
        parser.error("연계 토큰 형식이 잘못되었습니다.")

    context = ssl.create_default_context(cafile=args.ca_file)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect())
    request = urllib.request.Request(
        args.service_url + "/api/v1/registrations/claim",
        data=json.dumps({"linkage_token": token}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        print(f"연계 토큰 접수 실패 (HTTP {error.code}). 토큰·만료 시각을 확인하세요.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        print("연계 토큰 접수 실패. 서비스 주소와 TLS 연결을 확인하세요.", file=sys.stderr)
        return 1
    if result.get("status") != "claimed" or not isinstance(result.get("registration_id"), str):
        print("연계 토큰 응답을 확인할 수 없습니다.", file=sys.stderr)
        return 1
    print(f"연계 토큰 접수 완료: {result['registration_id']}")
    print("WireGuard 연결은 아직 구성되지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
