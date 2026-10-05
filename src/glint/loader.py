"""Load a .gitlab-ci.yml the way GitLab does.

Order of operations mirrors GitLab's ``Gitlab::Ci::Config``:
includes → extends → !reference → default/inheritance → job list.
"""

from __future__ import annotations

import copy
import glob as globlib
import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

from .model import Finding

RESERVED = {
    "image", "services", "stages", "types", "before_script", "after_script", "variables",
    "cache", "include", "workflow", "default", "spec",
}
DEFAULT_KEYS = [
    "after_script", "artifacts", "before_script", "cache", "hooks", "id_tokens", "image",
    "interruptible", "retry", "services", "tags", "timeout",
]
LEGACY_GLOBAL_KEYS = ["image", "services", "cache", "before_script", "after_script"]
SCRIPT_KEYS = ("before_script", "script", "after_script")
DEFAULT_STAGES = ["build", "test", "deploy"]
MAX_INCLUDES = 150  # GitLab's limit on included files per pipeline


class Reference(list):
    """An unresolved ``!reference [job, key, ...]`` tag."""

    def __repr__(self) -> str:
        return f"!reference {list(self)}"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reference", lambda loader, node: Reference(loader.construct_sequence(node, deep=True)))


def deep_merge(a, b):
    """GitLab merge: hashes merge recursively, everything else is replaced."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(a[k], v) if k in a else v
        return out
    return b


@dataclass
class VarDef:
    value: str
    expand: bool = True
    description: Optional[str] = None


def normalise_variables(raw) -> dict[str, VarDef]:
    out: dict[str, VarDef] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if isinstance(v, dict):
            val = v.get("value", "")
            out[str(k)] = VarDef(_scalar(val), v.get("expand", True) is not False, v.get("description"))
        else:
            out[str(k)] = VarDef(_scalar(v))
    return out


def _scalar(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


@dataclass
class Job:
    name: str
    config: dict  # fully resolved (extends, references, defaults applied)
    stage: str
    origin: list[str] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    matrix: dict[str, str] = field(default_factory=dict)  # parallel:matrix values for this instance
    base_name: Optional[str] = None  # job name before matrix expansion

    def script(self, key: str) -> list:
        return self.config.get(key) or []


@dataclass
class Config:
    raw: dict
    jobs: dict[str, Job]
    stages: list[str]
    global_variables: dict[str, VarDef]
    workflow: dict
    files: list[str]
    findings: list[Finding]
    hidden: dict[str, dict]


IncludeRuleCheck = Callable[[list], tuple[bool, str]]


class ConfigLoader:
    def __init__(
        self,
        repo_root: Path,
        project_includes: dict[str, Path] | None = None,
        include_rule_check: IncludeRuleCheck | None = None,
    ):
        self.repo_root = repo_root
        self.project_includes = project_includes or {}
        self.include_rule_check = include_rule_check
        self.findings: list[Finding] = []
        self.files: list[str] = []
        self.origins: dict[str, list[str]] = {}
        self._stack: list[Path] = []  # files currently being loaded, for cycle detection
        self._loaded: set[tuple[Path, str]] = set()  # (file, inputs) already merged

    # --- files & includes -------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)

    def _parse(self, path: Path, inputs: dict | None) -> dict:
        text = path.read_text(encoding="utf-8")
        try:
            docs = [d for d in yaml.load_all(text, Loader=_Loader)]
        except yaml.YAMLError as e:
            self.findings.append(Finding("error", "yaml-error", f"{self._rel(path)}: {e}"))
            return {}
        if len(docs) >= 2 and isinstance(docs[0], dict) and "spec" in docs[0]:
            spec = docs[0].get("spec") or {}
            values = {}
            for name, opts in (spec.get("inputs") or {}).items():
                if isinstance(opts, dict) and "default" in opts:
                    values[name] = opts["default"]
            values.update(inputs or {})
            body = re.split(r"^---[ \t]*$", text, maxsplit=1, flags=re.M)
            body_text = body[1] if len(body) > 1 else text
            body_text = self._interpolate(body_text, values, path)
            try:
                return yaml.load(body_text, Loader=_Loader) or {}
            except yaml.YAMLError as e:
                self.findings.append(Finding("error", "yaml-error", f"{self._rel(path)} (after inputs): {e}"))
                return {}
        doc = docs[-1] if docs else {}
        return doc if isinstance(doc, dict) else {}

    def _interpolate(self, text: str, values: dict, path: Path) -> str:
        def sub(m):
            name, funcs = m.group(1), m.group(2) or ""
            if name not in values:
                self.findings.append(
                    Finding("error", "include-input", f"{self._rel(path)}: input '{name}' has no value or default")
                )
                return m.group(0)
            val = values[name]
            val = "true" if val is True else "false" if val is False else str(val)
            t = re.search(r"truncate\((\d+),\s*(\d+)\)", funcs)
            if t:
                val = val[int(t.group(1)) : int(t.group(1)) + int(t.group(2))]
            return val

        return re.sub(r"\$\[\[\s*inputs\.([A-Za-z0-9_-]+)\s*((?:\|[^\]]*)?)\]\]", sub, text)

    def load_file(self, path: Path, root: Path, inputs: dict | None = None, depth: int = 0) -> dict:
        rel = self._rel(path)
        key = path.resolve()
        if key in self._stack:
            chain = " → ".join(self._rel(p) for p in self._stack[self._stack.index(key) :] + [key])
            self.findings.append(
                Finding("warning", "include-cycle", f"circular include skipped (each file is loaded once): {chain}")
            )
            return {}
        loaded_key = (key, json.dumps(inputs or {}, sort_keys=True, default=str))
        if loaded_key in self._loaded:
            return {}  # GitLab merges a file that's included more than once only once
        if len(self._loaded) >= MAX_INCLUDES:
            self.findings.append(
                Finding("error", "include-limit", f"more than {MAX_INCLUDES} included files; {rel} not loaded")
            )
            return {}
        self._loaded.add(loaded_key)
        self.files.append(rel)
        cfg = self._parse(path, inputs)
        includes = cfg.pop("include", None)
        merged: dict = {}
        self._stack.append(key)
        try:
            for spec in includes if isinstance(includes, list) else ([includes] if includes else []):
                for sub in self._resolve_include(spec, path, root, depth):
                    merged = deep_merge(merged, sub)
        finally:
            self._stack.pop()
        for key, val in cfg.items():
            if key not in RESERVED and isinstance(val, dict):
                self.origins.setdefault(key, [])
                if rel not in self.origins[key]:
                    self.origins[key].append(rel)
        return deep_merge(merged, cfg)

    def _resolve_include(self, spec, current: Path, root: Path, depth: int) -> list[dict]:
        if isinstance(spec, str):
            spec = {"remote": spec} if re.match(r"https?://", spec) else {"local": spec}
        if not isinstance(spec, dict):
            self.findings.append(Finding("error", "include-invalid", f"invalid include entry: {spec!r}"))
            return []
        label = next((f"{k}: {spec[k]}" for k in ("local", "project", "remote", "template", "component") if k in spec), str(spec))
        if "rules" in spec and self.include_rule_check:
            ok, why = self.include_rule_check(spec["rules"])
            if not ok:
                self.findings.append(Finding("info", "include-skipped", f"include {label} skipped: {why}"))
                return []
        inputs = spec.get("inputs") or spec.get("with")
        if "local" in spec:
            return self._load_glob(root, str(spec["local"]), inputs, depth, label)
        if "project" in spec:
            proj = str(spec["project"])
            proj_root = self.project_includes.get(proj)
            if proj_root is None:
                self.findings.append(
                    Finding(
                        "warning",
                        "include-unresolved",
                        f"include {label} not loaded: map it to a local checkout with "
                        f"project_includes in .glint.yml (or --project {proj}=PATH)",
                    )
                )
                return []
            files = spec.get("file") or []
            out = []
            for f in files if isinstance(files, list) else [files]:
                out += self._load_glob(proj_root, str(f), inputs, depth, f"project {proj}: {f}")
            return out
        kind = next((k for k in ("remote", "template", "component") if k in spec), "unknown")
        self.findings.append(
            Finding("warning", "include-unresolved", f"include {label} not loaded ({kind} includes aren't fetched); jobs it defines are missing")
        )
        return []

    def _load_glob(self, root: Path, pattern: str, inputs, depth: int, label: str) -> list[dict]:
        rel = pattern.lstrip("/")
        if any(c in rel for c in "*?["):
            paths = sorted(Path(p) for p in globlib.glob(str(root / rel), recursive=True))
        else:
            paths = [root / rel]
        out = []
        for p in paths:
            if not p.is_file():
                self.findings.append(Finding("error", "include-missing", f"include {label}: file not found ({p})"))
                continue
            out.append(self.load_file(p, root, inputs, depth + 1))
        if not paths:
            self.findings.append(Finding("warning", "include-missing", f"include {label}: glob matched no files"))
        return out

    # --- extends / references / defaults ---------------------------------

    def _resolve_extends(self, cfg: dict) -> tuple[dict, dict[str, list[str]]]:
        resolved: dict[str, dict] = {}
        chains: dict[str, list[str]] = {}

        def resolve(name: str, stack: tuple) -> dict:
            if name in resolved:
                return resolved[name]
            entry = cfg.get(name)
            if not isinstance(entry, dict):
                return {}
            parents = entry.get("extends")
            parents = [parents] if isinstance(parents, str) else list(parents or [])
            base: dict = {}
            chain: list[str] = []
            for p in parents:
                if p in stack:
                    self.findings.append(Finding("error", "extends-cycle", f"circular extends: {' → '.join(stack + (p,))}", job=name))
                    continue
                if p not in cfg:
                    self.findings.append(Finding("error", "extends-missing", f"'{name}' extends '{p}', which isn't defined", job=name))
                    continue
                if len(stack) > 11:
                    self.findings.append(Finding("error", "extends-depth", f"extends nesting deeper than 11 levels at '{name}'", job=name))
                    continue
                base = deep_merge(base, resolve(p, stack + (p,)))
                chain += chains.get(p, []) + [p]
            own = {k: v for k, v in entry.items() if k != "extends"}
            resolved[name] = deep_merge(base, own)
            chains[name] = chain
            return resolved[name]

        out = {}
        for k, v in cfg.items():
            out[k] = resolve(k, (k,)) if (k not in RESERVED and isinstance(v, dict)) else v
        return out, chains

    def _resolve_references(self, cfg: dict) -> dict:
        def lookup(path: list, depth: int):
            node = cfg
            for part in path:
                if not isinstance(node, dict) or part not in node:
                    self.findings.append(Finding("error", "reference-missing", f"!reference {list(path)} points at nothing"))
                    return None
                node = node[part]
            return resolve(node, depth + 1)

        def resolve(node, depth: int = 0):
            if depth > 10:
                self.findings.append(Finding("error", "reference-depth", "!reference nesting deeper than 10 levels"))
                return None
            if isinstance(node, Reference):
                return lookup(list(node), depth)
            if isinstance(node, dict):
                return {k: resolve(v, depth) for k, v in node.items()}
            if isinstance(node, list):
                return [resolve(v, depth) for v in node]
            return node

        return {k: resolve(v) for k, v in cfg.items()}

    # --- top level ----------------------------------------------------------

    def load(self, path: Path) -> Config:
        raw = self.load_file(path, self.repo_root)
        extended, chains = self._resolve_extends(raw)
        cfg = self._resolve_references(extended)

        stages = cfg.get("stages") or cfg.get("types") or DEFAULT_STAGES
        stages = [".pre"] + [s for s in stages if s not in (".pre", ".post")] + [".post"]

        default = dict(cfg.get("default") or {})
        for key in LEGACY_GLOBAL_KEYS:
            if key in cfg and key not in default:
                default[key] = cfg[key]

        jobs: dict[str, Job] = {}
        hidden: dict[str, dict] = {}
        for name, entry in cfg.items():
            if name in RESERVED or not isinstance(entry, dict):
                continue
            if name.startswith("."):
                hidden[name] = entry
                continue
            job = dict(entry)
            inherit = job.get("inherit") or {}
            inh_default = inherit.get("default", True)
            for key in DEFAULT_KEYS:
                if key in default and key not in job:
                    if inh_default is True or (isinstance(inh_default, list) and key in inh_default):
                        job[key] = copy.deepcopy(default[key])
            for key in SCRIPT_KEYS:
                if key in job:
                    job[key] = _flatten(job[key])
            for key in ("rules", "needs"):
                if isinstance(job.get(key), list):
                    job[key] = _flatten(job[key])
            stage = str(job.get("stage", "test"))
            origin = self.origins.get(name, [])
            for inst_name, inst_cfg, matrix in _expand_matrix(name, job):
                jobs[inst_name] = Job(inst_name, inst_cfg, stage, origin, chains.get(name, []), matrix, name)

        wf = cfg.get("workflow") or {}
        if isinstance(wf.get("rules"), list):
            wf = dict(wf, rules=_flatten(wf["rules"]))
        return Config(
            raw=cfg,
            jobs=jobs,
            stages=stages,
            global_variables=normalise_variables(cfg.get("variables")),
            workflow=wf,
            files=self.files,
            findings=self.findings,
            hidden=hidden,
        )


def _flatten(items) -> list:
    if not isinstance(items, list):
        return [items] if items is not None else []
    out = []
    for it in items:
        if isinstance(it, list):
            out.extend(_flatten(it))
        elif it is not None:
            out.append(it)
    return out


def _expand_matrix(name: str, job: dict):
    par = job.get("parallel")
    matrix = par.get("matrix") if isinstance(par, dict) else None
    if not isinstance(matrix, list):
        yield name, job, {}
        return
    for entry in matrix:
        if not isinstance(entry, dict):
            continue
        keys = list(entry)
        values = [[_scalar(x) for x in (v if isinstance(v, list) else [v])] for v in entry.values()]
        for combo in itertools.product(*values):
            vals = dict(zip(keys, combo))
            yield f"{name}: [{', '.join(combo)}]", job, vals
