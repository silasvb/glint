"""Terminal output."""

from __future__ import annotations

import json
from collections.abc import Iterable

from .model import Finding
from .pipeline import SOURCE_LABELS, JobResult, PipelineResult

_C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}


class Out:
    def __init__(self, color: bool):
        self.color = color
        self.lines: list[str] = []

    def c(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(_C[s] for s in styles) + text + _C["reset"]

    def __call__(self, line: str = "") -> None:
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines)


SEV_STYLE = {
    "error": ("red", "bold"),
    "warning": ("yellow",),
    "info": ("grey",),
    "ok": ("green",),
}
USE_STYLE = {
    "error": ("red", "bold"),
    "warning": ("yellow", "bold"),
    "info": ("cyan",),
    "ok": ("green",),
}


def status_of(jr: JobResult) -> tuple[str, str, tuple]:
    if not jr.included:
        return "·", "skipped", ("grey",)
    when = jr.outcome.when
    if when == "manual":
        return "▶", "manual", ("yellow",)
    if when == "delayed":
        return "◷", "delayed", ("cyan",)
    if when in ("on_failure",):
        return "!", "on_failure", ("magenta",)
    if when == "always":
        return "✔", "always", ("green",)
    return "✔", "runs", ("green",)


def _job_findings(pr: PipelineResult, jr: JobResult) -> list[Finding]:
    return [
        f for f in pr.findings if f.job in (jr.name, jr.job.base_name)
    ] + jr.findings


def all_findings(pr: PipelineResult) -> list[Finding]:
    out = [f for f in pr.findings]
    for jr in pr.jobs:
        out += jr.findings
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: (order[f.severity], f.job or "", f.code))


def overview(pr: PipelineResult, o: Out, show_skipped: bool = True) -> None:
    sc = pr.scenario
    o(
        o.c(f"Pipeline simulation: {sc.describe()}", "bold")
        + o.c(
            f"   protected ref: {'yes' if sc.protected(pr.settings) else 'no'}", "dim"
        )
    )
    if pr.settings.config_file:
        o(
            o.c(
                f"settings: {pr.settings.config_file.name}  ·  {len(pr.settings.external_vars)} CI/CD variables declared",
                "dim",
            )
        )
    else:
        o(
            o.c(
                "settings: none (no .glint.yml) - CI/CD settings variables are unknown, so they'll show as undefined",
                "dim",
            )
        )
    o(o.c(f"files: {', '.join(pr.config.files)}", "dim"))
    wf = pr.workflow
    if pr.config.workflow.get("rules"):
        o(
            "workflow: "
            + (
                o.c("pipeline created", "green")
                if wf.included
                else o.c("NO PIPELINE", "red", "bold")
            )
            + f" - {wf.reason}"
        )
        for t in wf.trace:
            for d in t.details:
                o(
                    o.c(
                        f"   {'✔' if t.matched else '✘'} #{t.index} {d}",
                        "green" if t.matched else "grey",
                    )
                )
        if pr.workflow_variables:
            o(
                "   sets: "
                + ", ".join(f"{k}={v.value}" for k, v in pr.workflow_variables.items())
            )
    o()
    by_stage: dict[str, list[JobResult]] = {}
    for jr in pr.jobs:
        by_stage.setdefault(jr.job.stage, []).append(jr)
    width = max((len(j.name) for j in pr.jobs), default=10) + 2
    for stage in pr.config.stages + sorted(set(by_stage) - set(pr.config.stages)):
        jobs = by_stage.get(stage, [])
        if not jobs:
            continue
        visible = [j for j in jobs if show_skipped or j.included]
        if not visible:
            continue
        o(o.c(f"stage: {stage}", "bold", "blue"))
        for jr in visible:
            icon, label, style = status_of(jr)
            errs = [f for f in _job_findings(pr, jr) if f.severity == "error"]
            warns = [f for f in _job_findings(pr, jr) if f.severity == "warning"]
            badge = ""
            if errs:
                badge += o.c(
                    f" {len(errs)} error{'s' * (len(errs) != 1)}", "red", "bold"
                )
            if warns:
                badge += o.c(
                    f" {len(warns)} warning{'s' * (len(warns) != 1)}", "yellow"
                )
            reason = jr.outcome.reason
            if jr.included and jr.outcome.matched_index and "rules" in jr.job.config:
                d = jr.outcome.trace[-1].details
                reason = f"rule #{jr.outcome.matched_index}: " + (
                    d[0].split("  →  ")[0] if d else ""
                )
            unc = o.c(" (assumed)", "magenta") if jr.outcome.uncertain else ""
            o(
                f"  {o.c(icon, *style)} {jr.name.ljust(width)}{o.c(label.ljust(10), *style)}{o.c(reason, 'dim')}{unc}{badge}"
            )
        o()
    summarise(pr, o)


def summarise(pr: PipelineResult, o: Out) -> None:
    fs = all_findings(pr)
    n = {s: sum(1 for f in fs if f.severity == s) for s in ("error", "warning", "info")}
    inc = [j for j in pr.jobs if j.included]
    o(
        o.c(f"{len(inc)}/{len(pr.jobs)} jobs in pipeline", "bold")
        + "  ·  "
        + o.c(
            f"{n['error']} error{'s' * (n['error'] != 1)}",
            *(SEV_STYLE["error"] if n["error"] else ("dim",)),
        )
        + "  ·  "
        + o.c(
            f"{n['warning']} warning{'s' * (n['warning'] != 1)}",
            *(SEV_STYLE["warning"] if n["warning"] else ("dim",)),
        )
    )


def findings(fs: Iterable[Finding], o: Out, min_severity: str = "warning") -> None:
    order = {"error": 0, "warning": 1, "info": 2}
    fs = [f for f in fs if order[f.severity] <= order[min_severity]]
    if not fs:
        o(o.c("No problems found.", "green"))
        return
    for f in fs:
        where = f.job or "(pipeline)"
        loc = f"  @ {f.location}" if f.location else ""
        more = f" (+{f.count - 1} more)" if f.count > 1 else ""
        o(
            f"{o.c(f.severity.upper().ljust(8), *SEV_STYLE[f.severity])}{o.c(where, 'bold')}{o.c(loc, 'dim')}{more}"
        )
        o(f"        {f.message}  {o.c('[' + f.code + ']', 'dim')}")


def _highlight(text: str, uses, o: Out) -> str:
    if not o.color or not uses:
        return text
    out, pos = [], 0
    for u in sorted(uses, key=lambda u: u.start):
        if u.start < pos:
            continue
        out.append(text[pos : u.start])
        out.append(o.c(text[u.start : u.end], *USE_STYLE.get(u.severity, ())))
        pos = u.end
    out.append(text[pos:])
    return "".join(out)


def job_detail(
    pr: PipelineResult, jr: JobResult, o: Out, show_predefined: bool = False
) -> None:
    _icon, label, style = status_of(jr)
    j = jr.job
    o(
        o.c(f"━━ {jr.name} ", "bold")
        + o.c(f"[{label}]", *style)
        + o.c(f"  stage: {j.stage}", "dim")
    )
    if j.origin:
        o(o.c(f"   defined in: {', '.join(j.origin)}", "dim"))
    if j.extends:
        o(o.c(f"   extends: {' → '.join(reversed(j.extends))}", "dim"))
    if jr.included:
        af = jr.allow_failure()
        o(
            o.c(
                f"   when: {jr.outcome.when}   allow_failure: {json.dumps(af) if not isinstance(af, str) else af}",
                "dim",
            )
        )
    o()
    o(
        o.c(
            "   Why "
            + ("it runs" if jr.included else "it doesn't run")
            + f": {jr.outcome.reason}",
            "bold",
        )
    )
    for t in jr.outcome.trace:
        mark = o.c("✔", "green") if t.matched else o.c("✘", "grey")
        extra = ""
        if isinstance(t.rule, dict):
            extra = "  " + ", ".join(
                f"{k}: {t.rule[k]}" for k in ("when", "allow_failure") if k in t.rule
            )
        o(f"   {mark} #{t.index}{o.c(extra, 'dim')}")
        for d in t.details:
            o(o.c(f"       {d}", "dim" if not t.matched else "reset"))
        if t.uncertain:
            o(o.c("       (depends on an assumption - see above)", "magenta"))
    o()
    o(
        o.c("   Variables", "bold")
        + o.c("  (highest-precedence value shown; ← = overrides)", "dim")
    )
    rows = [
        e for e in jr.variables.values() if show_predefined or e.source != "predefined"
    ]
    if not rows:
        o(o.c("     (none besides predefined)", "dim"))
    w = max((len(e.name) for e in rows), default=4) + 2
    for e in sorted(rows, key=lambda e: e.name):
        val = "(set, value unknown)" if e.value is None else repr(e.value)
        if e.expanded is not None and e.expanded != e.value:
            val += o.c(f" → {e.expanded!r}", "cyan")
        if e.expand is False:
            val += o.c(" (expand: false)", "dim")
        src = SOURCE_LABELS.get(e.source, e.source) + (
            f" {e.detail}" if e.detail else ""
        )
        over = "".join(
            f" ← {SOURCE_LABELS.get(s, s)} {v!r}"
            for s, v in reversed(e.overrides)
            if s != "predefined" or show_predefined
        )
        o(f"     {e.name.ljust(w)}{val}  {o.c(src, 'dim')}{o.c(over, 'magenta')}")
    if jr.dotenv:
        o(
            o.c(
                "     from dotenv artifacts: "
                + ", ".join(f"{k} ({v})" for k, v in jr.dotenv.items()),
                "dim",
            )
        )
    o()
    for key in ("before_script", "script", "after_script"):
        entries = j.script(key)
        if not entries:
            continue
        o(
            o.c(f"   {key}:", "bold")
            + (o.c("  (separate shell)", "dim") if key == "after_script" else "")
        )
        for i, entry in enumerate(entries):
            text = entry if isinstance(entry, str) else repr(entry)
            uses = [u for u in jr.uses if u.section == key and u.entry == i]
            lines = _highlight(text.rstrip("\n"), uses, o).split("\n")
            for li, line in enumerate(lines):
                prefix = f"{i:>3} │ " if li == 0 else "    │ "
                o(o.c(f"   {prefix}", "dim") + line)
        o()
    needs = jr.outcome.needs if jr.outcome.needs is not None else j.config.get("needs")
    if needs is not None:
        o(
            o.c("   needs: ", "bold")
            + ", ".join(
                str(n.get("job", n) if isinstance(n, dict) else n) for n in needs
            )
            if needs
            else o.c("   needs: []  (starts immediately)", "bold")
        )
    for key in ("image", "environment", "trigger"):
        if key in j.config:
            o(o.c(f"   {key}: ", "bold") + json.dumps(j.config[key], default=str))
    fs = _job_findings(pr, jr)
    if fs:
        o()
        o(o.c("   Findings", "bold"))
        for f in fs:
            loc = f" @ {f.location}" if f.location else ""
            o(
                f"   {o.c(f.severity.upper().ljust(8), *SEV_STYLE[f.severity])}{f.message}{o.c(loc, 'dim')}"
            )
    o()


def variable_table(pr: PipelineResult, o: Out, job_filter=None) -> None:
    """Every variable read by every included job, and where it comes from."""
    for jr in pr.jobs:
        if not jr.included or (job_filter and jr.name not in job_filter):
            continue
        uses = [u for u in jr.uses if u.source != "shell"]
        if not uses:
            continue
        o(o.c(f"━━ {jr.name}", "bold"))
        seen = {}
        for u in uses:
            key = (u.name, u.section if u.section == "after_script" else "main")
            if key in seen:
                if {"error": 0, "warning": 1, "info": 2, "ok": 3}[u.severity] < {
                    "error": 0,
                    "warning": 1,
                    "info": 2,
                    "ok": 3,
                }[seen[key].severity]:
                    seen[key] = u
                continue
            seen[key] = u
        w = max(len(u.name) for u in seen.values()) + 3
        for u in seen.values():
            prot = f" [{u.protection}]" if u.protection else ""
            sec = " (after_script)" if u.section == "after_script" else ""
            o(
                f"   {o.c(('$' + u.name).ljust(w), *USE_STYLE[u.severity])}{u.source.ljust(14)}{o.c(prot + sec, 'cyan')} {o.c(u.message, 'dim')}"
            )
        o()


def matrix(results: list[PipelineResult], o: Out) -> None:
    names: list[str] = []
    for pr in results:
        for jr in pr.jobs:
            if jr.name not in names:
                names.append(jr.name)
    w = max((len(n) for n in names), default=8) + 2
    cols = [pr.scenario.label for pr in results]
    cw = [max(len(c), 9) + 2 for c in cols]
    o(o.c("job".ljust(w) + "".join(c.ljust(cw[i]) for i, c in enumerate(cols)), "bold"))
    wf_row = "(workflow)".ljust(w)
    for i, pr in enumerate(results):
        wf_row += o.c(
            ("created" if pr.runs else "NO PIPELINE").ljust(cw[i]),
            "green" if pr.runs else "red",
        )
    o(o.c(wf_row, "dim") if not o.color else wf_row)
    for n in names:
        row = n.ljust(w)
        for i, pr in enumerate(results):
            jr = pr.job(n)
            if jr is None:
                row += "-".ljust(cw[i])
                continue
            icon, label, style = status_of(jr)
            errs = sum(1 for f in _job_findings(pr, jr) if f.severity == "error")
            cell = f"{icon} {label}" + (f" ✘{errs}" if errs else "")
            row += o.c(cell.ljust(cw[i]), *(("red", "bold") if errs else style))
        o(row)
