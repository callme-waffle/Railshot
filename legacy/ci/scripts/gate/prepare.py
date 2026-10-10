"""Read-only build-root discovery and preparation plans; never install or execute."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "pom.xml", "build.gradle", "build.gradle.kts"}
SKIP = {".git", "node_modules", ".venv", "venv", "target", "build", ".next"}
METADATA = {"docs", "examples", "fixtures", "testdata"}
EVIDENCE = MANIFESTS | {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock",
    ".node-version", ".nvmrc", ".python-version", "pnpm-workspace.yaml", "settings.gradle", "settings.gradle.kts",
    ".mvn/wrapper/maven-wrapper.properties", "gradle/wrapper/gradle-wrapper.properties", "mvnw", "gradlew",
    "gradle.lockfile", "gradle/verification-metadata.xml", "tsconfig.json"}


def safe_path(ws, value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("BUILD_ROOT_INVALID: expected a relative path within the upload")
    dest = ws / path
    if not dest.resolve().is_relative_to(ws) or any(p.is_symlink() for p in (dest, *dest.parents) if p.is_relative_to(ws)):
        raise ValueError("BUILD_ROOT_INVALID: symlink or escaped path")
    return dest


def declared_members(root):
    members = []
    if (root / "package.json").exists():
        pkg = json.loads((root / "package.json").read_text())
        members = pkg.get("workspaces", [])
        if isinstance(members, dict):
            members = members.get("packages", [])
        if (root / "pnpm-workspace.yaml").exists():
            import yaml
            try:
                data = yaml.safe_load((root / "pnpm-workspace.yaml").read_text()) or {}
                members = data.get("packages", []) if isinstance(data, dict) else None
            except yaml.YAMLError as exc:
                raise ValueError("BUILD_GRAPH_UNRESOLVED: invalid pnpm workspace YAML") from exc
    elif (root / "pom.xml").exists():
        members = [m.text for m in ET.parse(root / "pom.xml").findall("./{*}modules/{*}module")]
    else:
        settings = next((root / f for f in ("settings.gradle.kts", "settings.gradle") if (root / f).exists()), None)
        if settings:
            text = settings.read_text()
            if re.search(r"\bincludeBuild\b|\bprojectDir\b", text):
                raise ValueError("BUILD_GRAPH_UNRESOLVED: composite/custom Gradle paths need a reviewed profile")
            for match in re.finditer(r"(?m)^\s*include\s*(?:\(([^)]*)\)|([^\n]+))", text):
                body = match.group(1) if match.group(1) is not None else match.group(2)
                paths = re.findall(r"['\"]([^'\"]+)['\"]", body)
                if not paths or re.sub(r"['\"][^'\"]+['\"]|[\s,]", "", body):
                    raise ValueError("BUILD_GRAPH_UNRESOLVED: dynamic Gradle include")
                members.extend(p.lstrip(":").replace(":", "/") for p in paths)
    if not isinstance(members, list) or any(not isinstance(p, str) or not p for p in members):
        raise ValueError("BUILD_GRAPH_UNRESOLVED: invalid member declarations")
    return members


def root_graph(workspace, selected_root=None):
    ws = Path(workspace).resolve()
    selected = safe_path(ws, selected_root) if selected_root is not None else None
    manifests = [p for p in ws.rglob("*") if p.is_file() and p.name in MANIFESTS
                 and not set(p.relative_to(ws).parts) & SKIP]
    for path in manifests:
        safe_path(ws, path.relative_to(ws))
    roots = sorted({p.parent for p in manifests})
    groups, owners = {}, {}
    for root in roots:
        safe_path(ws, root.relative_to(ws))
        for name in EVIDENCE:
            if (root / name).exists() or (root / name).is_symlink():
                safe_path(ws, (root / name).relative_to(ws))
        if set(root.relative_to(ws).parts) & METADATA and root not in owners and not (selected and (root == selected or root in selected.parents or selected in root.parents)):
            groups[root] = []
            continue
        patterns = declared_members(root)
        members = set()
        for pattern in patterns:
            raw = pattern.removeprefix("!")
            safe_path(ws, root.relative_to(ws) / raw)
            if "${" in raw:
                raise ValueError("BUILD_GRAPH_UNRESOLVED: interpolated member path")
            found = {p for p in roots if p != root and p in root.glob(raw)}
            if not found and not pattern.startswith("!"):
                raise ValueError("BUILD_GRAPH_UNRESOLVED: declared member has no supported manifest")
            members = members - found if pattern.startswith("!") else members | found
        groups[root] = sorted(members)
        for member in members:
            if member in owners and owners[member] != root:
                raise ValueError("BUILD_GRAPH_UNRESOLVED: member belongs to multiple build roots")
            owners[member] = root
    return ws, roots, groups, owners


def select_build_roots(workspace, selected_root=None):
    """Group declared children under their native root; explicit selection overrides metadata exclusion."""
    ws, roots, _, owners = root_graph(workspace, selected_root)
    if selected_root is not None:
        chosen = safe_path(ws, selected_root)
        if chosen not in roots:
            raise ValueError("BUILD_ROOT_INVALID: selected root has no supported manifest")
        seen = set()
        while chosen in owners:
            if chosen in seen:
                raise ValueError("BUILD_GRAPH_UNRESOLVED: cyclic membership")
            seen.add(chosen)
            chosen = owners[chosen]
        return [chosen]
    return [p for p in roots if p not in owners and not set(p.relative_to(ws).parts) & METADATA]


def prepare_plan(workspace, *, selected_root=None, quality_plans=None):
    # Lazy import avoids a cycle: quality.discover reuses select_build_roots.
    import quality
    ws = Path(workspace).resolve()
    try:
        roots = select_build_roots(ws, selected_root)
        _, all_roots, groups, _ = root_graph(ws, selected_root)
    except (ValueError, OSError, ET.ParseError) as exc:
        return {"version": 1, "status": "BLOCKED", "selected_root": selected_root, "projects": [],
                "excluded": [], "blockers": [str(exc)]}
    selection_required = selected_root is None and len(roots) > 1
    if quality_plans is None:
        # Preview each candidate, but execution must wait for an explicit choice.
        plans = ([p for root in roots for p in quality.discover(ws, selected_root=root.relative_to(ws).as_posix())]
                 if selection_required else quality.discover(ws, selected_root=selected_root))
    else:
        plans = quality_plans
    by_path = {p["path"]: p for p in plans}
    projects = []
    for root in roots:
        rel = root.relative_to(ws).as_posix()
        plan = by_path.get(rel)
        if plan is None:
            plan = {"blocked": "SELECTED_ROOT_NOT_PLANNED: run discovery against the selected upload root"}
        members, pending = set(), list(groups[root])
        while pending:
            member = pending.pop()
            if member not in members:
                members.add(member)
                pending.extend(groups[member])
        evidence = []
        for folder in [root, *sorted(members)]:
            for name in sorted(EVIDENCE):
                path = folder / name
                if path.is_file():
                    safe_path(ws, path.relative_to(ws))
                    evidence.append({"path": path.relative_to(ws).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        pm = {"name": None, "pin": None, "source": None}
        commands = {k: [] for k in ("install", "lint", "type", "unit", "build", "report")}
        for command in plan.get("commands", []):
            stage = quality.command_stage(command)
            commands["install" if stage == "prepare" else stage].append(command)
        if (root / "package.json").exists():
            pkg = json.loads((root / "package.json").read_text())
            try:
                name, pin, *_ = quality.javascript_manager(root, pkg)
                pm = {"name": name, "pin": pin, "source": rel + "/package.json#packageManager" if pkg.get("packageManager") else "quality profile / lockfile"}
            except ValueError:
                pass  # The existing planner's blocker remains authoritative.
            if "build" in pkg.get("scripts", {}) and pm["name"]:
                commands["build"] = [pm["name"] + " run build"]
        elif (root / "pom.xml").exists() or any((root / n).exists() for n in ("build.gradle", "build.gradle.kts")):
            pm = {"name": "maven" if (root / "pom.xml").exists() else "gradle", "pin": plan.get("toolchain", {}).get("version"),
                  "source": "trusted native image; repository wrapper declaration and checksum must agree"}
        else:
            pm = {"name": "uv", "pin": quality.UV, "source": "platform profile; existing uv lock or separate requirements resolution"}
        reason = plan.get("blocked")
        actions = []
        for name, pin in plan.get("generated_tools", {}).items():
            actions.append({"kind": "install_platform_tool", "name": name, "pin": pin,
                            "location": plan.get("generated_tools_location") or plan.get("provenance", {}).get("directory"),
                            "condition": "preserve existing installed checker versions", "implemented": True})
        if reason:
            kind = reason.split(":", 1)[0]
            action = {"MISSING_CHECKER": "prepare pinned external checker/config", "MISSING_LOCK": "resolve a separate candidate lock; preserve existing dependency declarations",
                      "NO_TESTS": "record missing unit coverage and propose separately labelled smoke"}.get(kind, "review the unsupported or conflicting build profile")
            actions.append({"kind": kind, "action": action, "implemented": False})
        projects.append({"root": rel, "build_root": rel, "members": [p.relative_to(ws).as_posix() for p in sorted(members)],
            "source_evidence": evidence, "runtime_image": plan.get("image"), "package_manager": pm,
            "commands": commands, "commands_verified": False, "build_in_unit": plan.get("stack") in {"maven", "gradle"},
            "preparation_provenance": plan.get("provenance"),
            "missing_tool_actions": actions, "blockers": [reason] if reason else []})
    included = {root for root in roots} | {ws / p for project in projects for p in project["members"]}
    blockers = [b for p in projects for b in p["blockers"]]
    if not projects:
        blockers.append("UNSUPPORTED: no selected application build roots")
    if selection_required:
        blockers.insert(0, "BUILD_ROOT_SELECTION_REQUIRED: choose an application build root")
    return {"version": 1, "status": "NEEDS_SELECTION" if selection_required else "NEEDS_PREPARATION" if projects and blockers else "READY" if projects else "BLOCKED",
        "selected_root": selected_root, "selection_required": selection_required,
        "projects": projects, "excluded": [{"path": p.relative_to(ws).as_posix(), "reason": "metadata/example or outside selected build graph"} for p in all_roots if p not in included],
        "blockers": blockers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--selected-root")
    args = parser.parse_args()
    result = prepare_plan(args.workspace, selected_root=args.selected_root)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
