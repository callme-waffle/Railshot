"""Read-only HTTP checks. Uses no OpenStack credentials and never changes resources."""

import argparse
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="제어 API 읽기 전용 연결 확인")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.environ.get("CP_API_TOKEN")
    if not token:
        print("CP_API_TOKEN 환경변수가 필요합니다.", file=sys.stderr)
        return 2
    failed = False
    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        follow_redirects=False,
    ) as client:
        for path in (
            "/health/live",
            "/health/ready",
            "/api/v1/servers?limit=1",
            "/api/v1/images?limit=1",
            "/api/v1/flavors?limit=1",
            "/api/v1/networks?limit=1",
        ):
            try:
                response = client.get(path)
            except httpx.HTTPError:
                print(f"FAIL GET {path}: 연결 실패")
                failed = True
                continue
            ok = response.status_code == 200 and bool(response.headers.get("x-request-id"))
            print(f"{'PASS' if ok else 'FAIL'} GET {path}: HTTP {response.status_code}")
            failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
