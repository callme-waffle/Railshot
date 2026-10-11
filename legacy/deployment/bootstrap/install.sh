#!/usr/bin/env bash
set +x
set -euo pipefail
PERSONAL_REGISTRATION_MODE=false
PERSONAL_ENROLLMENT_SECRET=''
if [[ ${1:-} == '--personal-registration' ]]; then
    PERSONAL_REGISTRATION_MODE=true
    shift
    PERSONAL_ENROLLMENT_SECRET="${RAILSHOT_ENROLLMENT_TOKEN:-}"
fi
unset RAILSHOT_ENROLLMENT_TOKEN JASMIN_ENROLLMENT_TOKEN
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

personal_registration() {
    local enrollment_secret="$1"
    shift
    local api_url='' artifact_url='' artifact_sha256='' source_root='' test_allow_http=0
    local -a forwarded=("$@")
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --api-url|--artifact-url|--artifact-sha256|--source-root)
                [[ $# -ge 2 ]] || { echo '개인 환경 등록 인자 값이 필요합니다.' >&2; return 1; }
                case "$1" in
                    --api-url) api_url="$2" ;;
                    --artifact-url) artifact_url="$2" ;;
                    --artifact-sha256) artifact_sha256="$2" ;;
                    --source-root) source_root="$2" ;;
                esac
                shift 2 ;;
            --enrollment-id|--profile-id|--project-id|--runtime-config)
                [[ $# -ge 2 ]] || { echo '개인 환경 등록 인자 값이 필요합니다.' >&2; return 1; }
                shift 2 ;;
            --test-allow-http) test_allow_http=1; shift ;;
            --help|-h)
                echo 'Ubuntu 24.04/26.04: sudo bash install.sh --personal-registration --api-url HTTPS_URL --enrollment-id ID --artifact-url HTTPS_TGZ --artifact-sha256 SHA256 [--profile-id ID] [--test-allow-http]'
                return 0 ;;
            *) echo '지원하지 않는 개인 환경 등록 인자입니다.' >&2; return 1 ;;
        esac
    done
    [[ -n "$api_url" ]] || { echo '--api-url이 필요합니다.' >&2; return 1; }
    if [[ -z "$source_root" ]]; then
        [[ -n "$artifact_url" && "$artifact_sha256" =~ ^[a-fA-F0-9]{64}$ ]] || {
            echo '고정 배포본 주소와 SHA256 검증값이 필요합니다.' >&2; return 1;
        }
    fi

    # This public-server check must pass before package installation or cloud/account mutation.
    python3 - "$api_url" "$test_allow_http" "$artifact_url" "$source_root" <<'PY'
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

base = sys.argv[1]
allow_http = sys.argv[2] == '1'
parsed = urllib.parse.urlsplit(base)
schemes = ('https', 'http') if allow_http else ('https',)
if (parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or any(ch.isspace() for ch in base)):
    raise SystemExit('인증정보 및 질의문자열 없는 HTTPS API 주소가 필요합니다.')
if not sys.argv[4]:
    artifact = urllib.parse.urlsplit(sys.argv[3])
    if (artifact.scheme not in schemes or not artifact.hostname or artifact.username or artifact.password
            or artifact.query or artifact.fragment or any(ch.isspace() for ch in sys.argv[3])):
        raise SystemExit('인증정보 및 질의문자열 없는 HTTPS 배포본 주소가 필요합니다.')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('redirect rejected')

url = base.rstrip('/') + '/api/v1/readiness?scope=personal'
request = urllib.request.Request(url, method='GET', headers={'Accept': 'application/json'})
try:
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        raw = response.read(1048577)
        if response.status != 200 or len(raw) > 1048576:
            raise RuntimeError('invalid readiness response')
except Exception as exc:
    raise SystemExit('RailShot 서버 선행조건을 확인하지 못했습니다: ' + type(exc).__name__) from None
try:
    document = json.loads(raw)
    result = document.get('data', document)
    if not isinstance(result, dict) or result.get('scope') != 'personal' or result.get('ready') is not True:
        blockers = result.get('blockers', []) if isinstance(result, dict) else []
        details = []
        for row in blockers[:8]:
            if not isinstance(row, dict) or not isinstance(row.get('code'), str):
                continue
            message = ' '.join(row.get('message', '').split())[:256] if isinstance(row.get('message'), str) else ''
            details.append(row['code'] + (': ' + message if message else ''))
        raise RuntimeError('; '.join(details) or 'SERVER_NOT_READY')
except Exception as exc:
    detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    raise SystemExit('RailShot 서버 선행조건이 충족되지 않았습니다: ' + detail) from None
PY

    local work_dir=''
    if [[ -z "$source_root" ]]; then
        command -v curl >/dev/null || { echo '배포본 다운로드에 curl이 필요합니다.' >&2; return 1; }
        work_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/railshot-personal-bootstrap.XXXXXXXX")"
        local curl_protocols='=https'
        [[ "$test_allow_http" == 0 ]] || curl_protocols='=http,https'
        local status
        status="$(curl --disable --fail --silent --show-error --proto "$curl_protocols" --max-redirs 0 \
            --connect-timeout 15 --max-time 300 --output "$work_dir/payload.tgz" --write-out '%{http_code}' "$artifact_url")" || {
            rm -rf -- "$work_dir"; echo '개인 환경 배포본 다운로드에 실패했습니다.' >&2; return 1;
        }
        [[ "$status" == 200 ]] || { rm -rf -- "$work_dir"; echo '개인 환경 배포본 응답 오류입니다.' >&2; return 1; }
        if ! python3 - "$work_dir" "$artifact_sha256" <<'PY'
import hashlib
import sys
import tarfile
from pathlib import Path
root = Path(sys.argv[1])
archive = root / 'payload.tgz'
if hashlib.sha256(archive.read_bytes()).hexdigest() != sys.argv[2].lower():
    raise SystemExit('개인 환경 배포본 검증값이 일치하지 않습니다.')
with tarfile.open(archive) as source:
    members = source.getmembers()
    if any(member.issym() or member.islnk() or member.isdev()
           or not (member.isfile() or member.isdir()) or member.name.startswith('/')
           or '..' in Path(member.name).parts for member in members):
        raise SystemExit('안전하지 않은 개인 환경 배포본입니다.')
    source.extractall(root / 'source', filter='data')
PY
        then
            rm -rf -- "$work_dir"
            echo '개인 환경 배포본 검증 또는 압축 해제에 실패했습니다.' >&2
            return 1
        fi
        source_root="$work_dir/source"
        forwarded+=(--source-root "$source_root")
    fi
    local helper="$source_root/deployment/bootstrap/personal-registration.sh"
    [[ -f "$helper" && ! -L "$helper" ]] || {
        [[ -z "$work_dir" ]] || rm -rf -- "$work_dir"
        echo '개인 환경 등록 구현 파일이 없습니다.' >&2
        return 1
    }
    local result=0
    RAILSHOT_ENROLLMENT_TOKEN="$enrollment_secret" bash "$helper" "${forwarded[@]}" || result=$?
    [[ -z "$work_dir" ]] || rm -rf -- "$work_dir"
    return "$result"
}

if [[ "$PERSONAL_REGISTRATION_MODE" == true ]]; then
    personal_registration "$PERSONAL_ENROLLMENT_SECRET" "$@"
    unset PERSONAL_ENROLLMENT_SECRET
    exit 0
fi

# 폐기한 등록 키를 하위 프로세스에 전달하지 않습니다. 개인 등록 모드만 셸 변수로 제한적으로 보존합니다.
SOURCE_RUN=false
INSTALL_DEPS=false
DOWNLOAD_BASE_URL="${JASMIN_DOWNLOAD_BASE_URL:-}"
DOWNLOAD_DIR=''
cleanup_download() {
    local status=$?
    if [[ -n "$DOWNLOAD_DIR" ]]; then rm -rf -- "$DOWNLOAD_DIR"; fi
    return "$status"
}
trap cleanup_download EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-run) SOURCE_RUN=true; shift ;;
        --install-dependencies) INSTALL_DEPS=true; shift ;;
        --download-base-url)
            [[ $# -ge 2 && -n "$2" ]] || { echo '--download-base-url에는 HTTPS 배포 주소가 필요합니다.' >&2; exit 1; }
            DOWNLOAD_BASE_URL="$2"; shift 2 ;;
        *) break ;;
    esac
done
# 동일 배포에 필요한 파일 목록입니다. 테스트와 개발용 파일은 내려받지 않습니다.
required_files=(
    deployment/bootstrap/install.sh
    deployment/bootstrap/uninstall.sh
    deployment/bootstrap/install_payload.py
    deployment/bootstrap/requirements.lock
    deployment/bootstrap/client_setup/__init__.py
    deployment/bootstrap/client_setup/main.py
    deployment/bootstrap/client_setup/preflight.py
    deployment/bootstrap/client_setup/wireguard.py
    deployment/bootstrap/client_setup/credentials.py
    deployment/bootstrap/client_setup/state.py
    deployment/bootstrap/client_setup/report.py
    deployment/bootstrap/templates/agent-authorized-keys.README
    infrastructure/providers/openstack/__init__.py
    infrastructure/providers/openstack/cli.py
    infrastructure/providers/openstack/identity.py
    infrastructure/providers/openstack/discovery.py
    infrastructure/providers/openstack/access.py
    infrastructure/providers/openstack/templates/cloud-init.yaml.tmpl
    apps/agent/__init__.py
    apps/agent/install_forced_command.py
    apps/agent/protocol.py
    apps/agent/runner.py
    apps/agent/sender.py
)
if [[ -n "$DOWNLOAD_BASE_URL" ]]; then
    # 인증정보, 질의 문자열, fragment 및 제어문자가 포함된 URL은 받지 않습니다.
    url_pattern='^https://([A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$'
    [[ "$DOWNLOAD_BASE_URL" =~ $url_pattern ]] || { echo '배포 주소는 인증정보·쿼리·공백 없는 HTTPS 주소여야 합니다.' >&2; exit 1; }
    command -v curl >/dev/null || { echo '배포 파일 다운로드에 curl이 필요합니다.' >&2; exit 1; }
    DOWNLOAD_BASE_URL="${DOWNLOAD_BASE_URL%/}"
    DOWNLOAD_DIR="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/jasmin-bootstrap.XXXXXXXX")"
    chmod 700 "$DOWNLOAD_DIR"
    for relative in "${required_files[@]}"; do
        destination="$DOWNLOAD_DIR/$relative"
        mkdir -p -- "${destination%/*}"
        # curl 설정 파일 및 리다이렉트를 사용하지 않으며 TLS 검증을 유지합니다.
        if ! http_status="$(curl --disable --fail --show-error --silent --proto '=https' \
            --connect-timeout 15 --max-time 120 --max-redirs 0 \
            --output "$destination" --write-out '%{http_code}' \
            "$DOWNLOAD_BASE_URL/$relative")"; then
            echo "배포 파일 다운로드 실패: $relative" >&2
            exit 1
        fi
        if [[ "$http_status" != 200 || ! -f "$destination" ]]; then
            echo "배포 파일 응답 오류: $relative (HTTP $http_status; 200만 허용)" >&2
            exit 1
        fi
    done
    SCRIPT_DIR="$DOWNLOAD_DIR/deployment/bootstrap"
else
    LOCAL_ROOT="$SCRIPT_DIR/../.."
    for relative in "${required_files[@]}"; do
        if [[ ! -f "$LOCAL_ROOT/$relative" ]]; then
            echo "로컬 배포 파일 누락: $relative. --download-base-url 또는 JASMIN_DOWNLOAD_BASE_URL로 HTTPS 배포 루트를 지정하십시오." >&2
            exit 1
        fi
    done
fi
# Validate the shared CLI/config before package, payload or virtualenv changes.
for argument in "$@"; do
    if [[ "$argument" == "--help" || "$argument" == "-h" ]]; then
        PYTHONPATH="$SCRIPT_DIR" python3 -m client_setup.preflight "$@"
        exit 0
    fi
done
PYTHONPATH="$SCRIPT_DIR" python3 -m client_setup.preflight "$@"
if [[ "$INSTALL_DEPS" == true ]]; then
    [[ ${EUID} -eq 0 ]] || { echo '의존성 설치는 root 권한이 필요합니다.' >&2; exit 1; }
    python3 - <<'PY'
from pathlib import Path
values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
if values.get('ID', '').strip('"') != 'ubuntu' or values.get('VERSION_ID', '').strip('"') != '24.04':
    raise SystemExit('Ubuntu 24.04만 지원합니다.')
PY
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iproute2 openssh-client python3-venv python3-openstackclient
fi
if [[ "$SOURCE_RUN" == false ]]; then
    python3 "$SCRIPT_DIR/install_payload.py"
    SCRIPT_DIR=/opt/jasmin/bootstrap/deployment/bootstrap
fi
PYTHON=python3
if [[ "$SOURCE_RUN" == false || "$INSTALL_DEPS" == true ]]; then
    [[ ! -L "$SCRIPT_DIR/.venv" ]] || { echo '가상환경 심볼릭 링크를 허용하지 않습니다.' >&2; exit 1; }
    python3 - "$SCRIPT_DIR/.venv" <<'PY'
import os, stat, sys
from pathlib import Path
path = Path(sys.argv[1])
if path.exists():
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise SystemExit('기존 가상환경 소유권 또는 접근 권한이 안전하지 않습니다.')
PY
    python3 -m venv "$SCRIPT_DIR/.venv"
    "$SCRIPT_DIR/.venv/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.lock"
fi
if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then PYTHON="${SCRIPT_DIR}/.venv/bin/python"; fi
export PYTHONPATH="${SCRIPT_DIR}"
"$PYTHON" -m client_setup.main "$@"
