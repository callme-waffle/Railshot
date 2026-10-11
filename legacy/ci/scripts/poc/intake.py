#!/usr/bin/env python3
"""Intake (deterministic): checks an upload, makes a sanitized agent workspace, writes ir.json.

usage: intake.py <upload_dir> <work_dir> <run_dir>
"""
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.database import scan_workspace

MAX_BYTES = 100 * 1024 * 1024
MAX_FILES = 20_000
# Instructions for other agents are an injection surface; hooks in .claude/settings.json run commands.
AGENT_FILES = ["CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules",
               ".claude", ".cursor", ".codex", ".github/copilot-instructions.md"]
SECRET_NAME = re.compile(r"(^|/)(\.env[^/]*|.*\.pem|.*\.key|id_(rsa|ed25519|ecdsa)[^/]*)$")
SECRET_TEXT = re.compile(rb"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{20,}"
                         rb"|xox[bpas]-[0-9A-Za-z-]{10,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")
MANIFESTS = ["package.json", "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml", "Gemfile", "build.gradle", "build.gradle.kts"]
LOCKS = ["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock", "go.sum", "Cargo.lock"]
FRAMEWORKS = {"fastapi": "fastapi", "flask": "flask", "django": "django", "express": "express",
              "vite": "vite", "next": "next", "react": "react", "uvicorn": "uvicorn"}
PORT_HINT = re.compile(r"""(?:--port['"]?\s*,?.*?default\s*=\s*|PORT['"]?\s*[,:=]\s*|port\s*[:=]\s*|--port\s+)(\d{4,5})""")


def fail(msg):
    print(json.dumps({"ok": False, "reason": msg}))
    sys.exit(2)


def check_upload(root):
    files, total = [], 0
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if ".git" in p.relative_to(root).parts:
            continue
        if p.is_symlink():
            target = (p.parent / os.readlink(p)).resolve()
            if root.resolve() not in target.parents:
                fail(f"symlink escapes upload: {rel}")
            continue
        if p.is_file():
            files.append(rel)
            total += p.stat().st_size
    if len(files) > MAX_FILES or total > MAX_BYTES:
        fail(f"upload too large: {len(files)} files, {total} bytes")
    leaks = [f for f in files if SECRET_NAME.search(f)]
    for f in files:
        with open(root / f, "rb") as fh:
            if SECRET_TEXT.search(fh.read(2_000_000)):
                leaks.append(f)
    if leaks:
        fail(f"secrets found, remove them and upload again: {sorted(set(leaks))}")
    return files, total


def inventory(root, files, total):
    ext = Counter(Path(f).suffix.lower() for f in files if Path(f).suffix)
    manifests = [f for f in files if Path(f).name in MANIFESTS or re.search(r"requirements[^/]*\.txt$", f)]
    frameworks, scripts, ports, entry = set(), {}, Counter(), []
    for m in manifests:
        text = (root / m).read_text(errors="ignore").lower()
        frameworks |= {v for k, v in FRAMEWORKS.items() if re.search(rf'["\s/]{re.escape(k)}["\s=<>~^@]', text)}
        if m.endswith("package.json"):
            try:
                scripts[m] = json.loads((root / m).read_text()).get("scripts", {})
            except ValueError:
                pass
    for f in files:
        if Path(f).suffix in {".py", ".js", ".mjs", ".ts", ".tsx", ".go", ".toml", ".json", ".yaml", ".yml"} and "/test" not in f:
            text = (root / f).read_text(errors="ignore")
            ports.update(PORT_HINT.findall(text))
            if f.endswith(".py") and "__main__" in text and ("argparse" in text or "serve" in text):
                entry.append(f)
    return {
        "files": len(files), "bytes": total,
        "extensions": dict(ext.most_common(12)),
        "manifests": manifests,
        "lockfiles": [f for f in files if Path(f).name in LOCKS],
        "frameworks": sorted(frameworks),
        "package_scripts": scripts,
        "dockerfiles": [f for f in files if Path(f).name == "Dockerfile" or f.endswith(".Dockerfile")],
        "python_entrypoints": entry[:20],
        "port_hints": [p for p, _ in ports.most_common(8)],
    }


def main():
    source = Path(sys.argv[1])
    if source.is_symlink():
        fail('upload root must not be a symlink')
    upload, work, run = (Path(a).resolve() for a in sys.argv[1:4])
    if (upload == work or upload in work.parents or work in upload.parents
            or upload == run or upload in run.parents or run in upload.parents
            or work == run or work in run.parents):
        fail('upload, workspace and run paths overlap unsafely')
    files, total = check_upload(upload)
    if work.exists():
        shutil.rmtree(work)
    # Never execute uploaded git hooks, filters, config, or credentials during git add.
    shutil.copytree(upload, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    removed = []
    names = {n for n in AGENT_FILES if "/" not in n}
    for p in sorted(work.rglob("*")):
        rel = p.relative_to(work).as_posix()
        if (p.name in names or rel in AGENT_FILES) and (p.exists() or p.is_symlink()):
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            removed.append(rel)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    # The imported baseline is every sanitized file, including user-ignored source.
    subprocess.run(["git", "add", "-f", "-A"], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.name=railshot", "-c", "user.email=railshot@localhost",
                    "commit", "-qm", "import"], cwd=work, check=True)
    ir = inventory(work, [f for f in files if (work / f).is_file()], total)
    ir["removed_agent_files"] = sorted(set(removed))
    # Static source evidence only; the scanner never approves or provisions a DB.
    ir["database"] = scan_workspace(work)
    run.mkdir(parents=True, exist_ok=True)
    (run / "ir.json").write_text(json.dumps(ir, indent=2, ensure_ascii=False))
    print(json.dumps({"ok": True, "ir": str(run / "ir.json"), "removed": ir["removed_agent_files"]}))


if __name__ == "__main__":
    main()
