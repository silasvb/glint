"""Simulate pipeline creation for a scenario: workflow, job inclusion, variables."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import predefined
from .expr import referenced_variables
from .loader import Config, ConfigLoader, Job, VarDef, normalise_variables
from .model import ExternalVar, Finding, Scenario, Settings
from .rules import Outcome, RuleContext
from .shell import GITLAB_REF_RE

# Variable sources, lowest precedence first.
SOURCES = [
    "predefined",
    "global",
    "workflow",
    "job",
    "matrix",
    "rules",
    "dotenv",
    "cicd",
    "pipeline",
]
SOURCE_LABELS = {
    "predefined": "predefined (GitLab)",
    "global": "global variables:",
    "workflow": "workflow:rules:variables",
    "job": "job variables:",
    "matrix": "parallel:matrix",
    "rules": "rules:variables",
    "dotenv": "dotenv artifact",
    "cicd": "CI/CD settings",
    "pipeline": "pipeline variable (--var)",
}
YAML_SOURCES = {"global", "workflow", "job", "matrix", "rules"}


@dataclass
class VarEntry:
    name: str
    value: str | None  # None = present but value unknown (e.g. masked CI/CD variable)
    source: str
    expand: bool = True
    detail: str = ""
    overrides: list[tuple[str, str | None]] = field(
        default_factory=list
    )  # (source, value) shadowed
    expanded: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "expanded": self.expanded,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "detail": self.detail,
            "expand": self.expand,
            "overrides": [
                {"source": s, "source_label": SOURCE_LABELS.get(s, s), "value": v}
                for s, v in self.overrides
            ],
        }


@dataclass
class JobResult:
    job: Job
    outcome: Outcome
    variables: dict[str, VarEntry] = field(default_factory=dict)
    unavailable: dict[str, str] = field(
        default_factory=dict
    )  # name -> why it's not set
    dotenv: dict[str, str] = field(default_factory=dict)  # name -> producer job
    dotenv_unknown: list[str] = field(
        default_factory=list
    )  # producers whose variable names we couldn't see
    findings: list[Finding] = field(default_factory=list)
    uses: list = field(default_factory=list)  # analysis.VarUse

    @property
    def name(self) -> str:
        return self.job.name

    @property
    def included(self) -> bool:
        return self.outcome.included

    def allow_failure(self):
        af = self.outcome.allow_failure
        if af is None:
            af = self.job.config.get("allow_failure")
        if af is None:
            af = self.outcome.when == "manual"
        return af


@dataclass
class PipelineResult:
    scenario: Scenario
    settings: Settings
    config: Config
    workflow: Outcome
    workflow_variables: dict[str, VarDef]
    jobs: list[JobResult]
    findings: list[Finding]
    pipeline_vars: dict[str, str]

    @property
    def runs(self) -> bool:
        return self.workflow.included

    def job(self, name: str) -> JobResult | None:
        return next((j for j in self.jobs if j.name == name), None)


def _external_available(
    ev: ExternalVar, sc: Scenario, s: Settings, env_name: str | None
) -> tuple[bool, str]:
    if ev.protected and not sc.protected(s):
        return (
            False,
            f"protected CI/CD variable, but {sc.ref} is not a protected ref in this scenario",
        )
    scope = ev.environment_scope or "*"
    if scope != "*":
        if not env_name:
            return (
                False,
                f"CI/CD variable scoped to environment '{scope}', but the job has no environment",
            )
        if not fnmatch.fnmatchcase(env_name, scope):
            return (
                False,
                f"CI/CD variable scoped to environment '{scope}', job environment is '{env_name}'",
            )
    return True, ""


def _select_external(
    candidates: list[ExternalVar], sc: Scenario, s: Settings, env_name: str | None
) -> tuple[ExternalVar | None, str]:
    """Pick the CI/CD variable GitLab would use: the most specific matching environment scope."""
    available, reasons = [], []
    for ev in candidates:
        ok, why = _external_available(ev, sc, s, env_name)
        if ok:
            available.append(ev)
        else:
            reasons.append(why)
    if not available:
        return None, "; ".join(dict.fromkeys(reasons))

    def specificity(ev: ExternalVar) -> int:
        scope = ev.environment_scope or "*"
        return 0 if scope == "*" else 2 if scope == env_name else 1

    return max(available, key=specificity), ""


def _expand_value(value: str, lookup, depth: int = 0) -> str:
    if depth > 10:
        return value

    def sub(m):
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(2) or m.group(3)
        v = lookup(name)
        if v is None:
            return m.group(0)
        return _expand_value(v, lookup, depth + 1)

    return GITLAB_REF_RE.sub(sub, value)


class Simulator:
    def __init__(self, ci_file: Path, settings: Settings, scenario: Scenario):
        self.ci_file = ci_file
        self.s = settings
        self.sc = scenario
        self.ctx = RuleContext(scenario, settings.repo_root)
        self.base_vars = predefined.pipeline_variables(scenario, settings)

    def _ext_lookup_vars(self) -> dict[str, str]:
        out = {}
        for name, candidates in self.s.external_vars.items():
            ev, _ = _select_external(candidates, self.sc, self.s, None)
            if ev is not None:
                out[name] = ev.value if ev.value is not None else "<set>"
        return out

    def run(self) -> PipelineResult:
        findings: list[Finding] = []
        sc = self.sc

        include_vars = {**self.base_vars, **self._ext_lookup_vars(), **sc.pipeline_vars}

        def include_check(rules):
            o = self.ctx.eval_rules(rules, include_vars.get, default_when="always")
            return o.included, o.reason

        loader = ConfigLoader(self.s.repo_root, self.s.project_includes, include_check)
        cfg = loader.load(self.ci_file)
        findings += cfg.findings

        # --- workflow ---
        gvars = cfg.global_variables
        wf_lookup = self._lookup(
            [
                ("predefined", self.base_vars),
                ("global", gvars),
                ("cicd", self._ext_lookup_vars()),
                ("pipeline", sc.pipeline_vars),
            ]
        )
        if cfg.workflow.get("rules"):
            wf = self.ctx.eval_rules(
                cfg.workflow["rules"], wf_lookup, default_when="always"
            )
            for e in wf.errors:
                findings.append(Finding("error", "rules-invalid", f"workflow: {e}"))
        else:
            wf = Outcome(True, "always", "no workflow:rules - pipeline always created")
        wf_vars = normalise_variables(wf.variables) if wf.included else {}

        # --- jobs ---
        results: list[JobResult] = []
        for job in cfg.jobs.values():
            results.append(self._job(job, cfg, gvars, wf_vars, findings))

        if not wf.included:
            for r in results:
                r.outcome = Outcome(
                    False,
                    "never",
                    "pipeline not created (workflow:rules)",
                    trace=r.outcome.trace,
                )

        self._dotenv(results, cfg)
        self._validate(results, cfg, findings)
        if wf.included and not any(r.included for r in results):
            findings.append(
                Finding(
                    "warning",
                    "empty-pipeline",
                    "no jobs run in this scenario, so GitLab won't create a pipeline",
                )
            )

        return PipelineResult(
            sc, self.s, cfg, wf, wf_vars, results, findings, sc.pipeline_vars
        )

    # ------------------------------------------------------------------

    def _lookup(self, layers):
        merged: dict[str, tuple[str, bool]] = {}
        for _, layer in layers:
            for k, v in layer.items():
                if isinstance(v, VarDef):
                    merged[k] = (v.value, v.expand)
                else:
                    merged[k] = (v, True)

        def lookup(name: str) -> str | None:
            if name not in merged:
                return None
            value, expand = merged[name]
            if value is None:
                return None
            return _expand_value(value, lookup) if expand else value

        return lookup

    def _inherited_globals(
        self, job: Job, gvars: dict[str, VarDef]
    ) -> dict[str, VarDef]:
        inherit = (job.config.get("inherit") or {}).get("variables", True)
        if inherit is True:
            return gvars
        if isinstance(inherit, list):
            return {k: v for k, v in gvars.items() if k in inherit}
        return {}

    def _job(self, job: Job, cfg: Config, gvars, wf_vars, findings) -> JobResult:
        sc, s = self.sc, self.s
        jvars = normalise_variables(job.config.get("variables"))
        globals_ = self._inherited_globals(job, gvars)
        matrix = {k: VarDef(v) for k, v in job.matrix.items()}

        unscoped = self._ext_lookup_vars()

        def resolve_environment(rule_vars: dict[str, VarDef]):
            """Job predefined vars (incl. CI_ENVIRONMENT_NAME) and the CI/CD variables that apply to it."""
            env_lookup = self._lookup(
                [
                    ("p", self.base_vars),
                    ("g", globals_),
                    ("w", wf_vars),
                    ("j", jvars),
                    ("m", matrix),
                    ("r", rule_vars),
                    ("c", unscoped),
                    ("pv", sc.pipeline_vars),
                ]
            )
            job_pre = predefined.job_variables(
                job.name, job.stage, job.config, lambda v: _expand_value(v, env_lookup)
            )
            env_name = job_pre.get("CI_ENVIRONMENT_NAME")
            externals, unavailable = {}, {}
            for name, candidates in s.external_vars.items():
                ev, why = _select_external(candidates, sc, s, env_name)
                if ev is not None:
                    externals[name] = ev
                else:
                    unavailable[name] = why
            return job_pre, externals, unavailable

        job_pre, externals, unavailable = resolve_environment({})
        predef = {**self.base_vars, **job_pre}
        ext_values = {
            k: (v.value if v.value is not None else "<set>")
            for k, v in externals.items()
        }
        lookup = self._lookup(
            [
                ("predefined", predef),
                ("global", globals_),
                ("workflow", wf_vars),
                ("job", jvars),
                ("matrix", matrix),
                ("cicd", ext_values),
                ("pipeline", sc.pipeline_vars),
            ]
        )

        if "rules" in job.config:
            outcome = self.ctx.eval_rules(
                job.config["rules"],
                lookup,
                default_when=str(job.config.get("when", "on_success")),
            )
            for e in outcome.errors:
                findings.append(Finding("error", "rules-invalid", e, job=job.name))
        else:
            outcome = self.ctx.eval_only_except(job.config, lookup)
        rvars = normalise_variables(outcome.variables)
        if rvars:
            # e.g. `environment: $TARGET_ENV` with TARGET_ENV set by rules:variables
            job_pre, externals, unavailable = resolve_environment(rvars)
            predef = {**self.base_vars, **job_pre}

        for name, why in predefined.CONDITIONAL.items():
            if name not in predef:
                unavailable.setdefault(name, why)

        layers = [
            ("predefined", {k: VarDef(v) for k, v in predef.items()}),
            ("global", globals_),
            ("workflow", wf_vars),
            ("job", jvars),
            ("matrix", matrix),
            ("rules", rvars),
            (
                "cicd",
                {k: VarDef(v.value, True, v.origin) for k, v in externals.items()},
            ),
            ("pipeline", {k: VarDef(v) for k, v in sc.pipeline_vars.items()}),
        ]
        entries: dict[str, VarEntry] = {}
        for source, layer in layers:
            for k, vd in layer.items():
                ve = VarEntry(k, vd.value, source, vd.expand)
                if source == "cicd":
                    ev = externals[k]
                    flags = [
                        f
                        for f, on in (
                            ("protected", ev.protected),
                            ("masked", ev.masked),
                        )
                        if on
                    ]
                    if ev.environment_scope not in ("*", None, ""):
                        flags.append(f"env: {ev.environment_scope}")
                    ve.detail = f"{ev.origin}" + (
                        f" ({', '.join(flags)})" if flags else ""
                    )
                    if ev.masked and ev.value is not None:
                        ve.value = "[masked]"
                if k in entries:
                    prev = entries[k]
                    ve.overrides = prev.overrides + [(prev.source, prev.value)]
                entries[k] = ve
        for k in list(unavailable):
            if k in entries:
                del unavailable[k]

        def final_lookup(name):
            e = entries.get(name)
            if e is None or e.value is None:
                return None
            return _expand_value(e.value, final_lookup) if e.expand else e.value

        for e in entries.values():
            if e.value is not None and e.source != "predefined":
                e.expanded = final_lookup(e.name)

        return JobResult(job, outcome, entries, unavailable)

    # ------------------------------------------------------------------

    def _dotenv(self, results: list[JobResult], cfg: Config) -> None:
        by_name = {r.name: r for r in results}
        producers: dict[str, tuple[set[str], bool]] = {}
        for r in results:
            if not r.included:
                continue
            reports = ((r.job.config.get("artifacts") or {}).get("reports") or {}).get(
                "dotenv"
            )
            if not reports:
                continue
            paths = reports if isinstance(reports, list) else [reports]
            producers[r.name] = _dotenv_names(r.job, [str(p) for p in paths])

        if not producers:
            return
        stage_idx = {s: i for i, s in enumerate(cfg.stages)}
        for r in results:
            if not r.included:
                continue
            needs = r.job.config.get("needs")
            if isinstance(needs, list):
                upstream = []
                for n in needs:
                    if isinstance(n, dict):
                        if (
                            n.get("artifacts") is False
                            or "pipeline" in n
                            or "project" in n
                        ):
                            continue
                        upstream.append(str(n.get("job")))
                    else:
                        upstream.append(str(n))
            elif isinstance(r.job.config.get("dependencies"), list):
                upstream = [str(d) for d in r.job.config["dependencies"]]
            else:
                my = stage_idx.get(r.job.stage, 0)
                upstream = [
                    o.name
                    for o in results
                    if o.included and stage_idx.get(o.job.stage, 0) < my
                ]
            # `needs: [build]` on a parallel:matrix job means every `build: [...]` instance
            upstream = [
                o.name
                for up in upstream
                for o in results
                if up in (o.name, o.job.base_name)
            ]
            for up in upstream:
                if (
                    up in producers
                    and up != r.name
                    and by_name.get(up)
                    and by_name[up].included
                ):
                    names, complete = producers[up]
                    for n in names:
                        r.dotenv.setdefault(n, up)
                        if n not in r.variables or SOURCES.index(
                            r.variables[n].source
                        ) < SOURCES.index("dotenv"):
                            prev = r.variables.get(n)
                            ve = VarEntry(
                                n, "<from artifact>", "dotenv", detail=f"from {up}"
                            )
                            if prev:
                                ve.overrides = prev.overrides + [
                                    (prev.source, prev.value)
                                ]
                            r.variables[n] = ve
                            r.unavailable.pop(n, None)
                    if not complete:
                        r.dotenv_unknown.append(up)

    def _validate(
        self, results: list[JobResult], cfg: Config, findings: list[Finding]
    ) -> None:
        included = {r.name for r in results if r.included}
        defined = {r.name for r in results} | {r.job.base_name for r in results}
        stages = set(cfg.stages)
        for r in results:
            c = r.job.config
            if r.job.stage not in stages:
                findings.append(
                    Finding(
                        "error",
                        "unknown-stage",
                        f"job uses stage '{r.job.stage}', which isn't in stages:",
                        job=r.name,
                    )
                )
            if "script" not in c and "trigger" not in c and "run" not in c:
                findings.append(
                    Finding(
                        "error",
                        "no-script",
                        "job has no script: or trigger: (GitLab rejects this config)",
                        job=r.name,
                    )
                )
            for key in ("before_script", "script", "after_script"):
                for i, entry in enumerate(r.job.script(key)):
                    if not isinstance(entry, str):
                        findings.append(
                            Finding(
                                "error",
                                "script-not-string",
                                f"{key}[{i}] is a {type(entry).__name__}, not a string - YAML parsed it as a "
                                f"mapping/value (an unquoted ': ' or leading ':'?). Use a '- |' block or quote it: {entry!r}",
                                job=r.name,
                                location=f"{key}[{i}]",
                            )
                        )
            if not r.included:
                continue
            needs = r.outcome.needs if r.outcome.needs is not None else c.get("needs")
            for n in needs if isinstance(needs, list) else []:
                optional = False
                if isinstance(n, dict):
                    if "pipeline" in n or "project" in n:
                        continue
                    optional = bool(n.get("optional"))
                    n = n.get("job")
                n = str(n)
                matches = {
                    x.name for x in results if x.name == n or x.job.base_name == n
                }
                if not matches & defined and n not in defined:
                    findings.append(
                        Finding(
                            "error",
                            "needs-undefined",
                            f"needs '{n}', which isn't defined anywhere",
                            job=r.name,
                        )
                    )
                elif not matches & included:
                    if optional:
                        findings.append(
                            Finding(
                                "info",
                                "needs-optional-absent",
                                f"optional need '{n}' isn't in this pipeline",
                                job=r.name,
                            )
                        )
                    else:
                        findings.append(
                            Finding(
                                "error",
                                "needs-excluded",
                                f"needs '{n}', which isn't in the pipeline in this scenario - GitLab will refuse to "
                                "create the pipeline (add `optional: true` or align the rules)",
                                job=r.name,
                            )
                        )
            for d in c.get("dependencies") or []:
                if str(d) not in included:
                    findings.append(
                        Finding(
                            "error",
                            "dependency-excluded",
                            f"dependencies lists '{d}', which isn't in this pipeline",
                            job=r.name,
                        )
                    )

        # Variables used in rules that nothing defines - usually a typo.
        known = (
            set(predefined.KNOWN)
            | set(self.s.external_vars)
            | set(self.sc.pipeline_vars)
            | set(self.s.ignore)
        )
        known |= set(cfg.global_variables)
        for r in results:
            known |= set(normalise_variables(r.job.config.get("variables")))
            known |= set(r.job.matrix)
        exprs: list[tuple[str | None, str]] = []
        for rule in cfg.workflow.get("rules") or []:
            if isinstance(rule, dict) and "if" in rule:
                exprs.append((None, str(rule["if"])))
            if isinstance(rule, dict):
                known |= set(normalise_variables(rule.get("variables")))
        seen_jobs: set[str] = set()
        for r in results:
            base = r.job.base_name or r.name
            if base in seen_jobs:
                continue
            seen_jobs.add(base)
            for rule in r.job.config.get("rules") or []:
                if isinstance(rule, dict) and "if" in rule:
                    exprs.append((base, str(rule["if"])))
        reported = set()
        for job, e in exprs:
            for v in referenced_variables(e):
                if v not in known and (job, v) not in reported:
                    reported.add((job, v))
                    findings.append(
                        Finding(
                            "warning",
                            "rules-unknown-var",
                            f"rules reference ${v}, which nothing defines (not predefined, not in YAML, not declared in "
                            f".glint.yml) - it is always null here. Typo? Or a schedule/trigger variable: declare it.",
                            job=job,
                            var=v,
                        )
                    )


_DOTENV_LINE = re.compile(
    r"""(?:echo|printf)\s+(?:-[a-z]+\s+)*["']?([A-Za-z_][A-Za-z0-9_]*)="""
)


def _dotenv_names(job: Job, paths: list[str]) -> tuple[set[str], bool]:
    """Best-effort: which variables does this job write to its dotenv report?"""
    names: set[str] = set()
    complete = True
    bases = {p.split("/")[-1] for p in paths}
    text = "\n".join(str(x) for k in ("before_script", "script") for x in job.script(k))
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        targets = re.findall(r">>?\s*[\"']?([^\s\"';|&]+)", line)
        if any(t.split("/")[-1] in bases for t in targets):
            heredoc = re.search(r"<<-?\s*[\"']?(\w+)", line)
            if heredoc:
                delim = heredoc.group(1)
                i += 1
                while i < len(lines) and lines[i].strip() != delim:
                    m = re.match(
                        r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", lines[i]
                    )
                    if m:
                        names.add(m.group(1))
                    i += 1
            else:
                found = _DOTENV_LINE.findall(line)
                if found:
                    names.update(found)
                else:
                    complete = False
        i += 1
    if not names:
        complete = False
    return names, complete
