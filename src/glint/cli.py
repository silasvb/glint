"""glint - simulate and lint GitLab CI pipelines.

    glint show                     # what runs on the default branch, and why
    glint show --job deploy        # full resolved definition of one job
    glint vars                     # every variable each job reads, and where it comes from
    glint check [--strict]         # findings; non-zero exit on errors (use in CI)
    glint matrix                   # which jobs run on main / feature / MR / tag / schedule
    glint html -o pipeline.html    # interactive report covering all of the above
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import report_html, report_text
from .analysis import analyse
from .model import Scenario
from .pipeline import PipelineResult, Simulator
from .settings import git_changed_files, load_gitlab_variables_json, load_settings, standard_scenarios


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", nargs="?", default=".gitlab-ci.yml", help="CI file (default: .gitlab-ci.yml)")
    p.add_argument("--repo-root", type=Path, help="repository root (default: directory of the CI file)")
    p.add_argument("--config", type=Path, help="glint settings file (default: <repo-root>/.glint.yml)")
    g = p.add_argument_group("scenario (default: push to the default branch)")
    g.add_argument("--branch", help="simulate a push to this branch")
    g.add_argument("--tag", help="simulate a tag pipeline")
    g.add_argument("--mr", metavar="SOURCE[:TARGET]", help="simulate a merge request pipeline")
    g.add_argument("--source", help="CI_PIPELINE_SOURCE (push, web, schedule, api, trigger, ...)")
    g.add_argument("--default-branch", help="override the default branch (else .glint.yml, else main)")
    g.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="pipeline variable (repeatable)")
    g.add_argument("--gitlab-vars", action="append", default=[], type=Path, metavar="JSON",
                   help="CI/CD variables exported from the GitLab API / `glab variable export` (repeatable)")
    g.add_argument("--project", action="append", default=[], metavar="GROUP/PROJ=PATH",
                   help="resolve `include: project:` from a local checkout (repeatable)")
    g.add_argument("--changed", help="comma-separated changed files, for rules:changes")
    g.add_argument("--changed-since", metavar="REF", help="use `git diff REF...HEAD` for rules:changes")
    g.add_argument("--assume-changes", choices=["true", "false"], default="true",
                   help="rules:changes result when changed files are unknown (default: true)")
    p.add_argument("--strict", action="store_true",
                   help="require a guard before using any variable that doesn't come from the YAML or GitLab")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _settings_and_scenario(args):
    ci = Path(args.file).resolve()
    if not ci.is_file():
        sys.exit(f"glint: {args.file}: no such file")
    root = (args.repo_root or ci.parent).resolve()
    s = load_settings(root, args.config)
    if args.default_branch:
        s.default_branch = args.default_branch
    if args.strict:
        s.strict = True
    for f in args.gitlab_vars:
        for ev in load_gitlab_variables_json(f):
            s.external_vars.setdefault(ev.name, ev)
    for spec in args.project:
        proj, _, path = spec.partition("=")
        s.project_includes[proj] = Path(path).resolve()

    pvars = {}
    for kv in args.var:
        k, _, v = kv.partition("=")
        pvars[k] = v
    changed = None
    if args.changed:
        changed = [c.strip() for c in args.changed.split(",") if c.strip()]
    elif args.changed_since:
        changed = git_changed_files(root, args.changed_since)

    common = dict(pipeline_vars=pvars, changed_files=changed, assume_changes=args.assume_changes == "true")
    if args.mr:
        src, _, tgt = args.mr.partition(":")
        sc = Scenario(f"MR {src} → {tgt or s.default_branch}", "mr", src, args.source or "merge_request_event",
                      target_branch=tgt or s.default_branch, **common)
    elif args.tag:
        sc = Scenario(f"tag {args.tag}", "tag", args.tag, args.source or "push", **common)
    else:
        b = args.branch or s.default_branch
        sc = Scenario(f"{b} ({args.source or 'push'})", "branch", b, args.source or "push", **common)
    return ci, s, sc


def simulate(ci: Path, s, sc) -> PipelineResult:
    return analyse(Simulator(ci, s, sc).run())


def _out(args) -> report_text.Out:
    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    return report_text.Out(color)


def _job_names(pr: PipelineResult, wanted: list[str]) -> list[str]:
    names = []
    for w in wanted:
        hits = [j.name for j in pr.jobs if j.name == w or j.job.base_name == w]
        if not hits:
            hits = [j.name for j in pr.jobs if w.lower() in j.name.lower()]
        if not hits:
            sys.exit(f"glint: no job matching '{w}'. Jobs: {', '.join(j.name for j in pr.jobs)}")
        names += hits
    return names


def cmd_show(args) -> int:
    ci, s, sc = _settings_and_scenario(args)
    pr = simulate(ci, s, sc)
    if args.json:
        print(json.dumps(report_html.to_data(pr), indent=2, default=str))
        return 0
    o = _out(args)
    if args.job:
        for n in _job_names(pr, args.job):
            report_text.job_detail(pr, pr.job(n), o, args.predefined)
    else:
        report_text.overview(pr, o, show_skipped=not args.hide_skipped)
        if args.all:
            o()
            for jr in pr.jobs:
                if jr.included:
                    report_text.job_detail(pr, jr, o, args.predefined)
        else:
            o(o.c("Run `glint show --job NAME` for a job's full definition, or `glint show --all`.", "dim"))
    print(o.text())
    return 0


def cmd_vars(args) -> int:
    ci, s, sc = _settings_and_scenario(args)
    pr = simulate(ci, s, sc)
    if args.json:
        print(json.dumps({j.name: [u.to_dict() for u in j.uses] for j in pr.jobs if j.included}, indent=2))
        return 0
    o = _out(args)
    o(o.c(f"Variables read by each job - {sc.describe()}", "bold"))
    o(o.c("green = defined · cyan = defaulted/tested · yellow = suspicious · red = undefined", "dim"))
    o()
    report_text.variable_table(pr, o, set(_job_names(pr, args.job)) if args.job else None)
    print(o.text())
    return 0


def cmd_check(args) -> int:
    ci, s, base = _settings_and_scenario(args)
    scenarios = standard_scenarios(s, base) if args.all_scenarios else [base]
    o = _out(args)
    worst = 0
    payload = []
    for sc in scenarios:
        pr = simulate(ci, s, sc)
        fs = report_text.all_findings(pr)
        if args.json:
            payload.append({"scenario": sc.label, "findings": [f.to_dict() for f in fs]})
        else:
            o(o.c(f"── {sc.describe()}", "bold"))
            report_text.findings(fs, o, "info" if args.verbose else "warning")
            o()
        if any(f.severity == "error" for f in fs):
            worst = max(worst, 1)
        if args.fail_on_warning and any(f.severity == "warning" for f in fs):
            worst = max(worst, 1)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(o.text())
    return worst


def cmd_matrix(args) -> int:
    ci, s, base = _settings_and_scenario(args)
    results = [simulate(ci, s, sc) for sc in standard_scenarios(s, base)]
    o = _out(args)
    report_text.matrix(results, o)
    print(o.text())
    return 0


def cmd_html(args) -> int:
    ci, s, base = _settings_and_scenario(args)
    explicit = any([args.branch, args.tag, args.mr, args.source])
    scenarios = standard_scenarios(s, base)
    if explicit:
        scenarios = [base] + [x for x in scenarios if x.label != base.label]
    results = [simulate(ci, s, sc) for sc in scenarios]
    out = Path(args.output)
    out.write_text(report_html.render(results, ci.name), encoding="utf-8")
    print(f"wrote {out} ({len(results)} scenarios)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="glint", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("show", help="which jobs run, why, and their full resolved definitions")
    _add_common(sp)
    sp.add_argument("--job", "-j", action="append", default=[], help="show one job in full (repeatable; substring ok)")
    sp.add_argument("--all", action="store_true", help="show every included job in full")
    sp.add_argument("--hide-skipped", action="store_true")
    sp.add_argument("--predefined", action="store_true", help="include predefined variables in the variable table")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("vars", help="every variable each job reads, and where it comes from")
    _add_common(sp)
    sp.add_argument("--job", "-j", action="append", default=[])
    sp.set_defaults(func=cmd_vars)

    sp = sub.add_parser("check", help="report problems; exits 1 on errors")
    _add_common(sp)
    sp.add_argument("--all-scenarios", action="store_true", help="check main, feature, MR, tag and schedule pipelines")
    sp.add_argument("--fail-on-warning", action="store_true")
    sp.add_argument("--verbose", "-v", action="store_true", help="include info-level findings")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("matrix", help="compare which jobs run across standard scenarios")
    _add_common(sp)
    sp.set_defaults(func=cmd_matrix)

    sp = sub.add_parser("html", help="write an interactive HTML report")
    _add_common(sp)
    sp.add_argument("-o", "--output", default="glint-report.html")
    sp.set_defaults(func=cmd_html)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
