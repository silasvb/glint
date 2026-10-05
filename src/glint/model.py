"""Shared data structures."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class ExternalVar:
    """A CI/CD variable set in project/group/instance settings (not in YAML)."""

    name: str
    value: Optional[str] = None  # None = value unknown (but non-blank)
    protected: bool = False
    masked: bool = False
    environment_scope: str = "*"
    origin: str = ".glint.yml"


@dataclass
class Settings:
    repo_root: Path
    default_branch: str = "main"
    project_path: str = "group/project"
    protected_branches: list[str] = field(default_factory=list)  # empty = [default_branch]
    protected_tags: list[str] = field(default_factory=list)
    # name -> one entry per environment scope (GitLab allows the same key in several scopes)
    external_vars: dict[str, list[ExternalVar]] = field(default_factory=dict)
    ignore: set[str] = field(default_factory=set)
    required_vars_name: str = "REQUIRED_VARS"
    strict: bool = False
    project_includes: dict[str, Path] = field(default_factory=dict)
    config_file: Optional[Path] = None

    def __post_init__(self):
        self.external_vars = {
            k: list(v) if isinstance(v, (list, tuple)) else [v] for k, v in self.external_vars.items()
        }

    def add_external(self, ev: ExternalVar) -> None:
        """Add a CI/CD variable; the first definition for a given name + scope wins."""
        scopes = self.external_vars.setdefault(ev.name, [])
        if not any(x.environment_scope == ev.environment_scope for x in scopes):
            scopes.append(ev)


@dataclass
class Scenario:
    """The pipeline being simulated: which ref, which trigger."""

    label: str
    kind: str = "branch"  # branch | tag | mr
    ref: str = "main"  # branch name, tag name, or MR source branch
    source: str = "push"
    target_branch: Optional[str] = None  # MR only
    pipeline_vars: dict[str, str] = field(default_factory=dict)  # --var / schedule / trigger vars
    changed_files: Optional[list[str]] = None  # None = unknown
    assume_changes: bool = True
    new_branch: bool = False

    def protected(self, s: Settings) -> bool:
        if self.kind == "mr":
            return False  # MR pipelines run on refs/merge-requests/*, which aren't protected
        patterns = s.protected_tags if self.kind == "tag" else (s.protected_branches or [s.default_branch])
        return any(fnmatch.fnmatchcase(self.ref, p) for p in patterns)

    def describe(self) -> str:
        if self.kind == "mr":
            return f"merge request {self.ref} → {self.target_branch} ({self.source})"
        return f"{self.kind} {self.ref} ({self.source})"


@dataclass
class Finding:
    severity: str  # error | warning | info
    code: str
    message: str
    job: Optional[str] = None
    var: Optional[str] = None
    location: Optional[str] = None
    count: int = 1

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def slugify(ref: str) -> str:
    s = re.sub(r"[^a-z0-9]", "-", ref.lower())[:63]
    return s.strip("-")
