"""Load .glint.yml and GitLab CI/CD variable exports; build scenarios."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from .model import ExternalVar, Scenario, Settings


def _ext_from_mapping(name: str, spec, origin: str) -> ExternalVar:
    if isinstance(spec, dict):
        val = spec.get("value")
        return ExternalVar(
            name,
            None if val is None else str(val),
            bool(spec.get("protected", False)),
            bool(spec.get("masked", False)),
            str(spec.get("environment_scope", "*")),
            origin,
        )
    return ExternalVar(name, None if spec is None else str(spec), origin=origin)


def load_gitlab_variables_json(path: Path, scope_label: Optional[str] = None) -> list[ExternalVar]:
    """Read the JSON array returned by GET /projects/:id/variables (or `glab variable export`)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("variables", [])
    out = []
    for item in data:
        if not isinstance(item, dict) or "key" not in item:
            continue
        out.append(
            ExternalVar(
                str(item["key"]),
                None,  # never keep secret values around; presence is what matters
                bool(item.get("protected", False)),
                bool(item.get("masked", False)),
                str(item.get("environment_scope", "*")),
                scope_label or path.name,
            )
        )
    return out


def load_settings(repo_root: Path, config_path: Optional[Path] = None) -> Settings:
    s = Settings(repo_root=repo_root)
    path = config_path or repo_root / ".glint.yml"
    if not path.is_file():
        return s
    s.config_file = path
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s.default_branch = str(data.get("default_branch", s.default_branch))
    s.project_path = str(data.get("project_path", s.project_path))
    s.protected_branches = [str(x) for x in data.get("protected_branches") or []]
    s.protected_tags = [str(x) for x in data.get("protected_tags") or []]
    s.ignore = {str(x) for x in data.get("ignore") or []}
    s.required_vars_name = str(data.get("required_vars_name", s.required_vars_name))
    s.strict = bool(data.get("strict", False))
    for name, spec in (data.get("variables") or {}).items():
        for item in spec if isinstance(spec, list) else [spec]:  # a list = one entry per environment scope
            s.add_external(_ext_from_mapping(str(name), item, path.name))
    for f in data.get("gitlab_variables_json") or []:
        for ev in load_gitlab_variables_json((path.parent / f).resolve()):
            s.add_external(ev)
    for proj, local in (data.get("project_includes") or {}).items():
        s.project_includes[str(proj)] = (path.parent / str(local)).resolve()
    return s


def git_changed_files(repo_root: Path, since: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{since}...HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [l for l in out.stdout.splitlines() if l]


def standard_scenarios(s: Settings, base: Scenario) -> list[Scenario]:
    """The scenarios people usually care about, for side-by-side comparison."""
    common = dict(
        pipeline_vars=base.pipeline_vars,
        changed_files=base.changed_files,
        assume_changes=base.assume_changes,
    )
    d = s.default_branch
    return [
        Scenario(f"{d} (push)", "branch", d, "push", **common),
        Scenario("feature branch (push)", "branch", "feature/example", "push", **common),
        Scenario(f"merge request → {d}", "mr", "feature/example", "merge_request_event", target_branch=d, **common),
        Scenario("tag v1.0.0", "tag", "v1.0.0", "push", **common),
        Scenario(f"schedule on {d}", "branch", d, "schedule", **common),
    ]
