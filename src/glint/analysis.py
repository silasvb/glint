"""Variable-use analysis: is every variable a job's scripts read actually set?"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .model import Finding
from .pipeline import YAML_SOURCES, JobResult, PipelineResult
from .predefined import RUNTIME
from .shell import DEFAULTING, GUARDING, gitlab_refs, scan

# Set by the shell / OS / runner image, not by GitLab.
SHELL_VARS = {
    "HOME", "PATH", "PWD", "OLDPWD", "USER", "LOGNAME", "SHELL", "HOSTNAME", "HOSTTYPE", "OSTYPE",
    "MACHTYPE", "UID", "EUID", "GID", "PPID", "RANDOM", "SECONDS", "LINENO", "IFS", "PS1", "PS2", "PS4",
    "TERM", "LANG", "LC_ALL", "TMPDIR", "BASH", "BASH_SOURCE", "BASH_VERSION", "BASH_REMATCH", "BASHPID",
    "FUNCNAME", "PIPESTATUS", "REPLY", "OPTARG", "OPTIND", "SHLVL", "EPOCHSECONDS", "EPOCHREALTIME",
}

# Sources that come from outside the YAML, so the YAML can't vouch for them.
EXTERNAL_SOURCES = {"cicd", "dotenv", "maybe-dotenv", "maybe-sourced", "undefined", "unavailable", "pipeline"}

SOURCE_TEXT = {
    "script": "set earlier in the script",
    "yaml": "defined in .gitlab-ci.yml",
    "predefined": "GitLab predefined variable",
    "cicd": "CI/CD settings variable (declared in .glint.yml)",
    "pipeline": "pipeline variable",
    "dotenv": "dotenv artifact from an upstream job",
    "maybe-dotenv": "possibly from an upstream dotenv artifact",
    "maybe-sourced": "possibly set by a sourced file / eval",
    "unavailable": "not set in this scenario",
    "undefined": "not defined anywhere glint can see",
    "shell": "shell/OS variable",
}


@dataclass
class VarUse:
    name: str
    section: str
    entry: int
    start: int
    end: int
    line: int
    source: str
    protection: Optional[str]  # defaulted | guard | guarded | tested | None
    severity: str  # ok | info | warning | error
    message: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


class _ShellState:
    def __init__(self):
        self.defined: dict[str, str] = {}
        self.guarded: dict[str, str] = {}
        self.opaque: Optional[str] = None  # first `source`/`eval` we couldn't see through


def analyse_job(jr: JobResult, pr: PipelineResult) -> None:
    s = pr.settings
    findings: dict[tuple, Finding] = {}
    uses: list[VarUse] = []

    def add(sev, code, msg, var=None, loc=None):
        key = (code, var)
        if key in findings:
            findings[key].count += 1
            return
        findings[key] = Finding(sev, code, msg, job=jr.name, var=var, location=loc)

    def source_of(name: str, st: _ShellState) -> tuple[str, str]:
        if name in st.defined:
            return "script", f"set at {st.defined[name]}"
        e = jr.variables.get(name)
        if e is not None:
            if e.source in YAML_SOURCES:
                return "yaml", f"{e.source} = {e.value!r}"
            if e.source == "predefined":
                return "predefined", "" if e.value == RUNTIME else f"= {e.value!r}"
            return e.source, e.detail
        if name in jr.unavailable:
            return "unavailable", jr.unavailable[name]
        if jr.dotenv_unknown:
            return "maybe-dotenv", f"upstream {', '.join(jr.dotenv_unknown)} writes a dotenv report glint can't read"
        if st.opaque:
            return "maybe-sourced", f"after {st.opaque}"
        return "undefined", ""

    def classify(name, section, idx, text, u, st: _ShellState, main_defs: dict[str, str], tested_here: set[str]):
        loc = f"{section}[{idx}] line {_line_of(text, u.start)}"
        if name in SHELL_VARS or name in s.ignore:
            uses.append(VarUse(name, section, idx, u.start, u.end, _line_of(text, u.start), "shell", None, "ok", SOURCE_TEXT["shell"]))
            return
        if u.modifier in DEFAULTING:
            protection = "defaulted"
        elif u.modifier in GUARDING:
            protection = "guard"
        elif u.guard_test:
            protection = "guard"
        elif u.tested or name in tested_here:
            # `if [ -n "$X" ]; then ... $X ... fi` - later uses in the same block are checked too
            protection = "tested"
            tested_here.add(name)
        elif name in st.guarded:
            protection = "guarded"
        else:
            protection = None
        source, detail = source_of(name, st)
        sev, msg = "ok", SOURCE_TEXT[source] + (f" ({detail})" if detail else "")
        safe = protection in ("defaulted", "tested")
        guarded = protection in ("guard", "guarded")

        if section == "after_script" and source in ("undefined", "maybe-sourced") and name in main_defs:
            sev, msg = "error", (
                f"${name} is set in {main_defs[name]}, but after_script runs in a separate shell - "
                "it is not available here. Write it to a file in script and read it back, or set it in variables:."
            )
            add("error", "after-script-scope", msg, name, loc)
        elif source == "yaml" and (jr.variables[name].expanded or "") == "" and not safe:
            if guarded:
                sev, msg = "warning", f"${name} is blank in the YAML; the guard will fail unless it's overridden in CI/CD settings"
                add("warning", "blank-guarded", msg, name, loc)
            else:
                sev, msg = "warning", f"${name} is defined but blank ({jr.variables[name].source} variables)"
                add("warning", "blank-var", msg, name, loc)
        elif source == "unavailable":
            if safe:
                sev, msg = "info", f"${name}: {detail} (handled: {protection})"
            elif guarded:
                sev, msg = "warning", f"${name}: {detail} - the guard will fail in this scenario"
                add("warning", "unavailable-guarded", msg, name, loc)
            else:
                sev, msg = "error", f"${name} is not set here: {detail}"
                add("error", "unavailable-var", msg, name, loc)
        elif source in ("maybe-dotenv", "maybe-sourced"):
            if not (safe or guarded):
                sev, msg = "warning", f"${name} isn't defined in the YAML; {detail}. Guard it to be sure."
                add("warning", "possibly-undefined", msg, name, loc)
        elif source == "undefined":
            hint = "Declare it under variables: in .glint.yml if it's set in project/group CI/CD settings."
            if safe:
                sev, msg = "info", f"${name} isn't defined anywhere glint can see (handled: {protection})"
            elif guarded:
                sev, msg = "warning", f"${name} isn't defined anywhere glint can see - the guard will fail unless it's set in CI/CD settings. {hint}"
                add("warning", "undefined-guarded", msg, name, loc)
            else:
                sev, msg = "error", f"${name} is used but never defined. {hint}"
                add("error", "undefined-var", msg, name, loc)

        if s.strict and sev == "ok" and source in EXTERNAL_SOURCES and protection is None:
            sev, msg = "error", (
                f"${name} comes from {SOURCE_TEXT[source]}, but isn't checked before use. "
                f"Add `: \"${{{name}:?{name} is required}}\"` or list it in ${s.required_vars_name}."
            )
            add("error", "unguarded", msg, name, loc)

        uses.append(VarUse(name, section, idx, u.start, u.end, _line_of(text, u.start), source, protection, sev, msg))
        if protection == "guard" and u.modifier in GUARDING or u.guard_test:
            st.guarded.setdefault(name, loc)

    def run_shell(sections: list[str], st: _ShellState, main_defs: dict[str, str]):
        for section in sections:
            for idx, text in enumerate(jr.job.script(section)):
                if not isinstance(text, str):
                    continue
                res = scan(text)
                tested_here: set[str] = set()
                events = [(u.start, 0, "use", u) for u in res.uses]
                events += [(d.pos, 1, "def", d) for d in res.defs]
                events += [(pos, 1, "source", p) for p, pos in res.sources]
                events += [(pos, 1, "eval", None) for pos in res.evals]
                for pos, _, kind, obj in sorted(events, key=lambda e: (e[0], e[1])):
                    loc = f"{section}[{idx}] line {_line_of(text, pos)}"
                    if kind == "use":
                        classify(obj.name, section, idx, text, obj, st, main_defs, tested_here)
                        if obj.name == s.required_vars_name:
                            e = jr.variables.get(obj.name)
                            for req in re.split(r"[\s,]+", (e.expanded or e.value or "") if e else ""):
                                if req:
                                    st.guarded.setdefault(req, f"{loc} (${s.required_vars_name})")
                    elif kind == "def":
                        st.defined.setdefault(obj.name, loc)
                    elif kind == "source":
                        names = _sourced_defs(obj, pr, jr)
                        if names is None:
                            st.opaque = st.opaque or f"`source {obj}` ({loc})"
                        else:
                            for n in names:
                                st.defined.setdefault(n, f"{obj} (sourced at {loc})")
                    elif kind == "eval":
                        st.opaque = st.opaque or f"`eval` ({loc})"

    main = _ShellState()
    run_shell(["before_script", "script"], main, {})
    after = _ShellState()
    after.guarded = dict(main.guarded)  # a guard proves the CI/CD value exists; it still does in after_script
    run_shell(["after_script"], after, main.defined)

    # Variable values in the YAML that reference undefined variables.
    for name, e in jr.variables.items():
        if e.source not in YAML_SOURCES or not e.expand or not e.value:
            continue
        for ref in gitlab_refs(e.value):
            if ref in jr.variables or ref in s.ignore or ref in jr.dotenv:
                continue
            if ref in jr.unavailable:
                add("warning", "var-ref-unavailable", f"{name}: {e.value!r} references ${ref}, which is not set here: {jr.unavailable[ref]}", ref)
            else:
                add("warning", "var-ref-undefined", f"{name}: {e.value!r} references ${ref}, which is never defined", ref)

    jr.uses = uses
    jr.findings = sorted(findings.values(), key=lambda f: ({"error": 0, "warning": 1, "info": 2}[f.severity], f.var or ""))


def _sourced_defs(path: str, pr: PipelineResult, jr: JobResult) -> Optional[set[str]]:
    """Variables defined at top level of a sourced file, if it's in the repo."""
    path = path.replace("$CI_PROJECT_DIR/", "").replace("${CI_PROJECT_DIR}/", "")
    if "$" in path:
        return None
    p = (pr.settings.repo_root / path.lstrip("./")) if not path.startswith("/") else Path(path)
    if not p.is_file():
        return None
    try:
        res = scan(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if res.sources or res.evals:
        return None
    return {d.name for d in res.defs}


def analyse(pr: PipelineResult) -> PipelineResult:
    for jr in pr.jobs:
        if jr.included:
            analyse_job(jr, pr)
    return pr
