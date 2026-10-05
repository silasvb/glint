"""Evaluation of ``rules``, ``workflow:rules`` and legacy ``only``/``except``."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .expr import ExprError, evaluate
from .model import Scenario

Lookup = Callable[[str], Optional[str]]


@dataclass
class RuleTrace:
    index: int
    rule: object
    matched: bool
    details: list[str] = field(default_factory=list)
    uncertain: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "rule": self.rule,
            "matched": self.matched,
            "details": self.details,
            "uncertain": self.uncertain,
        }


@dataclass
class Outcome:
    included: bool
    when: str
    reason: str
    matched_index: Optional[int] = None
    variables: dict = field(default_factory=dict)
    allow_failure: object = None
    needs: object = None
    trace: list[RuleTrace] = field(default_factory=list)
    uncertain: bool = False
    errors: list[str] = field(default_factory=list)


# --- globbing ----------------------------------------------------------------


@lru_cache(maxsize=1024)
def glob_regex(pattern: str) -> re.Pattern:
    """GitLab-style glob (FNM_PATHNAME | FNM_DOTMATCH | FNM_EXTGLOB) as a regex."""
    p = pattern.lstrip("/")
    if p.startswith("./"):
        p = p[2:]
    out, i = "", 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        elif c == "{" and "}" in p[i:]:
            j = p.index("}", i)
            out += "(?:" + "|".join(glob_regex(a).pattern[1:-1] for a in p[i + 1 : j].split(",")) + ")"
            i = j + 1
        elif c == "[" and "]" in p[i + 1 :]:
            j = p.index("]", i + 1)
            body = p[i + 1 : j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out += "[" + body.replace("\\", "\\\\") + "]"
            i = j + 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile("^" + out + "$")


@lru_cache(maxsize=8)
def _repo_files(root: str) -> tuple[str, ...]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "__pycache__")]
        rel = os.path.relpath(dirpath, root)
        for f in filenames:
            files.append(f if rel == "." else f"{rel}/{f}".replace(os.sep, "/"))
        if len(files) > 50000:
            break
    return tuple(files)


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# --- rules -------------------------------------------------------------------


class RuleContext:
    def __init__(self, scenario: Scenario, repo_root: Path):
        self.sc = scenario
        self.repo_root = repo_root

    def eval_if(self, expr, lookup: Lookup, details: list[str]) -> bool:
        try:
            r = evaluate(str(expr), lookup)
        except ExprError as e:
            details.append(f"if: {expr}  →  INVALID: {e}")
            return False
        details.append(f"if: {expr}  →  {r.explanation}  →  {str(r.value).lower()}")
        return r.value

    def eval_changes(self, spec, lookup: Lookup, details: list[str]) -> tuple[bool, bool]:
        """Returns (result, uncertain)."""
        paths = spec.get("paths") if isinstance(spec, dict) else spec
        paths = [_expand(str(p), lookup) for p in _as_list(paths)]
        sc = self.sc
        if sc.kind == "tag":
            details.append(f"changes: {paths}  →  always true for tag pipelines")
            return True, False
        if sc.source not in ("push", "merge_request_event", "external_pull_request_event") or sc.new_branch:
            details.append(f"changes: {paths}  →  always true for {sc.source} pipelines")
            return True, False
        if sc.changed_files is not None:
            hits = [f for f in sc.changed_files if any(glob_regex(p).match(f) for p in paths)]
            details.append(f"changes: {paths}  →  {'matched ' + ', '.join(hits[:5]) if hits else 'no changed file matches'}")
            return bool(hits), False
        details.append(
            f"changes: {paths}  →  unknown which files changed; assuming {str(sc.assume_changes).lower()}"
            " (use --changed / --changed-since)"
        )
        return sc.assume_changes, True

    def eval_exists(self, spec, lookup: Lookup, details: list[str]) -> tuple[bool, bool]:
        if isinstance(spec, dict) and spec.get("project"):
            details.append(f"exists: {spec} in another project  →  can't check; assuming true")
            return True, True
        paths = spec.get("paths") if isinstance(spec, dict) else spec
        paths = [_expand(str(p), lookup) for p in _as_list(paths)]
        files = _repo_files(str(self.repo_root))
        hits = [f for f in files if any(glob_regex(p).match(f) for p in paths)]
        details.append(f"exists: {paths}  →  {'found ' + ', '.join(hits[:3]) if hits else 'no matching file'}")
        return bool(hits), False

    def eval_rules(self, rules, lookup: Lookup, default_when: str = "on_success") -> Outcome:
        trace = []
        errors = []
        for idx, rule in enumerate(_as_list(rules)):
            if not isinstance(rule, dict):
                errors.append(f"rule #{idx + 1} is not a mapping: {rule!r}")
                continue
            details: list[str] = []
            ok, uncertain = True, False
            if "if" in rule:
                ok = self.eval_if(rule["if"], lookup, details) and ok
            if ok and "changes" in rule:
                r, u = self.eval_changes(rule["changes"], lookup, details)
                ok, uncertain = ok and r, uncertain or u
            if ok and "exists" in rule:
                r, u = self.eval_exists(rule["exists"], lookup, details)
                ok, uncertain = ok and r, uncertain or u
            if not any(k in rule for k in ("if", "changes", "exists")):
                details.append("no conditions  →  always matches")
            trace.append(RuleTrace(idx + 1, rule, ok, details, uncertain))
            if ok:
                when = str(rule.get("when", default_when))
                rvars = rule.get("variables") or {}
                return Outcome(
                    included=when != "never",
                    when=when,
                    reason=f"rule #{idx + 1} matched" + (" with when: never" if when == "never" else ""),
                    matched_index=idx + 1,
                    variables=rvars if isinstance(rvars, dict) else {},
                    allow_failure=rule.get("allow_failure"),
                    needs=rule.get("needs"),
                    trace=trace,
                    uncertain=uncertain,
                    errors=errors,
                )
        return Outcome(
            included=False,
            when="never",
            reason="no rule matched" if trace else "rules list is empty",
            trace=trace,
            uncertain=any(t.uncertain for t in trace),
            errors=errors,
        )

    # --- only / except ---------------------------------------------------

    def _ref_matches(self, ref: str) -> tuple[bool, str]:
        sc = self.sc
        keyword = {
            "branches": sc.kind == "branch" and sc.source != "merge_request_event",
            "tags": sc.kind == "tag",
            "merge_requests": sc.source == "merge_request_event",
            "schedules": sc.source == "schedule",
            "pushes": sc.source == "push",
            "web": sc.source == "web",
            "api": sc.source == "api",
            "triggers": sc.source == "trigger",
            "pipelines": sc.source in ("pipeline", "parent_pipeline"),
            "external": sc.source == "external",
            "chat": sc.source == "chat",
            "external_pull_requests": sc.source == "external_pull_request_event",
        }
        if ref in keyword:
            return keyword[ref], ref
        if sc.kind == "mr":
            return False, ref
        name = ref.split("@", 1)[0]
        if len(name) >= 2 and name.startswith("/") and name.rfind("/") > 0:
            try:
                body, _, flags = name[1:].rpartition("/")
                return re.search(body, sc.ref, re.I if "i" in flags else 0) is not None, ref
            except re.error:
                return False, ref
        return name == sc.ref, ref

    def _only_clause(self, spec, lookup: Lookup, details: list[str], label: str) -> tuple[bool, bool]:
        """Does an only/except spec match? Returns (matched, uncertain)."""
        if isinstance(spec, (list, str)):
            spec = {"refs": _as_list(spec)}
        if not isinstance(spec, dict):
            return False, False
        matched, uncertain = True, False
        if "refs" in spec:
            refs = [str(r) for r in _as_list(spec["refs"])]
            hits = [r for r in refs if self._ref_matches(r)[0]]
            details.append(f"{label}:refs {refs}  →  {'matches ' + ', '.join(hits) if hits else 'no match'}")
            matched = bool(hits)
        if matched and "variables" in spec:
            sub = [self.eval_if(e, lookup, details) for e in _as_list(spec["variables"])]
            matched = any(sub)
        if matched and "changes" in spec:
            r, u = self.eval_changes(spec["changes"], lookup, details)
            matched, uncertain = r, u
        if matched and "kubernetes" in spec:
            details.append(f"{label}:kubernetes  →  can't check; assuming active")
            uncertain = True
        return matched, uncertain

    def eval_only_except(self, job: dict, lookup: Lookup) -> Outcome:
        only, exc = job.get("only"), job.get("except")
        details: list[str] = []
        # GitLab defaults `only` to [branches, tags] whenever it's absent, even if `except` is set.
        defaulted = only is None
        if defaulted:
            only = ["branches", "tags"]
            details.append("no only: defaults to only: [branches, tags]")
        when = str(job.get("when", "on_success"))
        inc, unc = self._only_clause(only, lookup, details, "only")
        excluded = False
        if inc and exc is not None:
            excluded, u2 = self._only_clause(exc, lookup, details, "except")
            unc = unc or u2
            if excluded:
                inc = False
                details.append("excluded by except")
        trace = [RuleTrace(1, {"only": only, "except": exc}, inc, details, unc)]
        if inc and when == "never":
            return Outcome(False, "never", "when: never", trace=trace)
        return Outcome(
            included=inc,
            when=when if inc else "never",
            reason=("default only: [branches, tags]" if defaulted else "only/except matched")
            if inc
            else "excluded by except"
            if excluded
            else "not a branch/tag pipeline (default only: [branches, tags])"
            if defaulted
            else "excluded by only",
            matched_index=1 if inc else None,
            trace=trace,
            uncertain=unc,
        )


_EXPAND_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand(s: str, lookup: Lookup) -> str:
    return _EXPAND_RE.sub(lambda m: lookup(m.group(1) or m.group(2)) or "", s)
