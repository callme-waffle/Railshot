#!/usr/bin/env bash
# Compatibility entrypoint; the canonical installer owns registration mode.
set +x
set -euo pipefail
ENROLLMENT_SECRET="${RAILSHOT_ENROLLMENT_TOKEN:-}"
unset RAILSHOT_ENROLLMENT_TOKEN JASMIN_ENROLLMENT_TOKEN
[[ ${1:-} != '--personal-registration' ]] || shift
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/install.sh" && ! -L "$SCRIPT_DIR/install.sh" ]]; then
    RAILSHOT_ENROLLMENT_TOKEN="$ENROLLMENT_SECRET" bash "$SCRIPT_DIR/install.sh" --personal-registration "$@"
    exit $?
fi

# A legacy curl command may have downloaded only this wrapper. Resolve the
# canonical installer beside the already pinned artifact URL without sourcing it.
ARTIFACT_URL=''
ALLOW_HTTP=0
arguments=("$@")
while [[ $# -gt 0 ]]; do
    case "$1" in
        --artifact-url) [[ $# -ge 2 ]] || exit 1; ARTIFACT_URL="$2"; shift 2 ;;
        --test-allow-http) ALLOW_HTTP=1; shift ;;
        --api-url|--enrollment-id|--artifact-sha256|--profile-id|--source-root|--project-id|--runtime-config)
            [[ $# -ge 2 ]] || exit 1; shift 2 ;;
        --help|-h) shift ;;
        *) echo '지원하지 않는 개인 환경 등록 인자입니다.' >&2; exit 1 ;;
    esac
done
[[ -n "$ARTIFACT_URL" ]] || { echo 'canonical install.sh를 찾으려면 --artifact-url이 필요합니다.' >&2; exit 1; }
INSTALL_URL="$(python3 - "$ARTIFACT_URL" "$ALLOW_HTTP" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
value = sys.argv[1]
parsed = urlsplit(value)
schemes = ('https', 'http') if sys.argv[2] == '1' else ('https',)
if (parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or any(ch.isspace() for ch in value)):
    raise SystemExit('인증정보 및 질의문자열 없는 HTTPS 배포본 주소가 필요합니다.')
path = parsed.path.rsplit('/', 1)[0] + '/install.sh'
print(urlunsplit((parsed.scheme, parsed.netloc, path, '', '')))
PY
)"
temporary="$(umask 077; mktemp "${TMPDIR:-/tmp}/railshot-install.XXXXXXXX")"
trap 'rm -f -- "$temporary"' EXIT
protocols='=https'
[[ "$ALLOW_HTTP" == 0 ]] || protocols='=http,https'
status="$(curl --disable --fail --silent --show-error --proto "$protocols" --max-redirs 0 \
    --connect-timeout 15 --max-time 120 --output "$temporary" --write-out '%{http_code}' "$INSTALL_URL")"
[[ "$status" == 200 ]] || { echo 'canonical install.sh 다운로드에 실패했습니다.' >&2; exit 1; }
RAILSHOT_ENROLLMENT_TOKEN="$ENROLLMENT_SECRET" bash "$temporary" --personal-registration "${arguments[@]}"
