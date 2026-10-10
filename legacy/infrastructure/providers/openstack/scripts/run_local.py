"""Start the local API with a private SSH SOCKS tunnel; clean up both on exit."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def stop(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    private = ROOT / ".local"
    required = [
        ROOT / ".env",
        private / "tunnel.json",
        private / "id_ed25519",
        private / "known_hosts",
    ]
    if any(not path.is_file() for path in required):
        print("준비된 .env 및 .local 접속 설정이 필요합니다.", file=sys.stderr)
        return 2
    connection = json.loads((private / "tunnel.json").read_text())
    env = {key: value for key, value in os.environ.items() if not key.startswith("CP_")}
    env.update(
        {key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None}
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        proxy_port = reservation.getsockname()[1]
    proxy = f"socks5h://127.0.0.1:{proxy_port}"
    for key in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
        env[key] = proxy
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    tunnel = None
    server = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        tunnel = subprocess.Popen(
            [
                "ssh",
                "-F",
                "/dev/null",
                "-N",
                "-D",
                f"127.0.0.1:{proxy_port}",
                "-i",
                str(private / "id_ed25519"),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={private / 'known_hosts'}",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                f"{connection['user']}@{connection['host']}",
            ]
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if tunnel.poll() is not None:
                raise RuntimeError("SSH 터널을 열지 못했습니다.")
            try:
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("SSH 연결 대기 시간이 초과되었습니다.")
        print("API: http://127.0.0.1:8000 | 문서: http://127.0.0.1:8000/docs", flush=True)
        print("종료: Ctrl+C (API와 이 명령이 생성한 SSH 터널을 함께 종료합니다.)", flush=True)
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "control_plane.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            cwd=ROOT,
            env=env,
        )
        while server.poll() is None:
            if tunnel.poll() is not None:
                raise RuntimeError("SSH 연결이 끊겨 API를 종료합니다.")
            time.sleep(0.3)
        return server.returncode
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        stop(server)
        stop(tunnel)


if __name__ == "__main__":
    raise SystemExit(main())
