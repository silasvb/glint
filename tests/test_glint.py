from pathlib import Path
from textwrap import dedent

import pytest

from glint.analysis import analyse
from glint.expr import evaluate
from glint.model import ExternalVar, Scenario, Settings
from glint.pipeline import Simulator
from glint.shell import scan

EXAMPLES = Path(__file__).parent.parent / "examples"


# --- shell scanner -----------------------------------------------------------


def names(script):
    return [(u.name, u.modifier) for u in scan(script).uses]


def test_scan_plain_braced_and_modifiers():
    assert names('echo $A "${B}" ${C:-x} ${D:?msg} ${#E}') == [
        ("A", None), ("B", None), ("C", ":-"), ("D", ":?"), ("E", "#")
    ]


def test_scan_ignores_single_quotes_escapes_and_comments():
    assert names("echo '$A' \\$B $C # $D") == [("C", None)]


def test_scan_nested_default_and_command_substitution():
    assert names('x=$(cat "$F") ; echo ${A:-$B} `echo $C`') == [("F", None), ("A", ":-"), ("B", None), ("C", None)]


def test_scan_heredocs():
    quoted = "cat <<'EOF'\n$NOPE\nEOF\necho $YES"
    unquoted = "cat <<EOF\n$YES1\nEOF\necho $YES2"
    assert names(quoted) == [("YES", None)]
    assert names(unquoted) == [("YES1", None), ("YES2", None)]


def test_scan_definitions():
    s = scan("A=1\nexport B=2\nread -r C\nfor D in x y; do :; done\nFOO=1 make\nlocal E\neval \"F=1\"")
    assert {d.name for d in s.defs} == {"A", "B", "C", "D", "E", "F"}  # FOO=1 make is a prefix, not a def


def test_scan_tests_and_guard_tests():
    s = scan('[ -n "$A" ] && echo hi\nif [ -z "$B" ]; then echo missing; exit 1; fi')
    a, b = s.uses
    assert a.tested and not a.guard_test
    assert b.tested and b.guard_test


def test_scan_subshell_defs_do_not_leak():
    assert scan("x=$(Y=1; echo $Y)").defs[0].name == "x"
    assert [d.name for d in scan("x=$(Y=1; echo $Y)").defs] == ["x"]


def test_scan_set_u():
    assert scan("set -euo pipefail").set_u


# --- expressions -------------------------------------------------------------


def ev(expr, **vars):
    return evaluate(expr, vars.get).value


@pytest.mark.parametrize(
    "expr,vars,expected",
    [
        ('$CI_COMMIT_BRANCH == "main"', {"CI_COMMIT_BRANCH": "main"}, True),
        ('$CI_COMMIT_BRANCH == "main"', {}, False),
        ("$CI_COMMIT_BRANCH == null", {}, True),
        ('$X == ""', {}, False),
        ("$X", {"X": ""}, False),
        ("$X", {"X": "0"}, True),
        ("$A == $B", {"A": "x", "B": "x"}, True),
        ("$A != $B", {"A": "x", "B": "x"}, False),
        ("$TAG =~ /^v\\d+/", {"TAG": "v12"}, True),
        ("$TAG !~ /^v\\d+/", {"TAG": "v12"}, False),
        ("$TAG =~ /^V/i", {"TAG": "v1"}, True),
        ("$TAG =~ $RX", {"TAG": "release-1", "RX": "/^release-/"}, True),
        ("$U =~ /x/", {}, False),
        ('$A == "1" || $B == "1" && $C == "1"', {"A": "1"}, True),
        ('($A == "1" || $B == "1") && $C == "1"', {"A": "1"}, False),
        ("$A && $B", {"A": "1"}, False),
    ],
)
def test_expressions(expr, vars, expected):
    assert ev(expr, **vars) is expected


# --- end to end ---------------------------------------------------------------


def run(tmp_path, yml, scenario=None, files=None, **settings):
    (tmp_path / ".gitlab-ci.yml").write_text(dedent(yml))
    for name, content in (files or {}).items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(content))
    s = Settings(repo_root=tmp_path, **settings)
    sc = scenario or Scenario("main", "branch", "main", "push")
    return analyse(Simulator(tmp_path / ".gitlab-ci.yml", s, sc).run())


def codes(pr, job=None):
    out = [f.code for f in pr.findings if job is None or f.job == job]
    for j in pr.jobs:
        if job is None or j.name == job:
            out += [f.code for f in j.findings]
    return out


def test_extends_reference_and_default(tmp_path):
    pr = run(
        tmp_path,
        """
        default:
          before_script: [echo default]
        .base:
          variables: {A: "1", B: "1"}
          script: [echo base]
        .more:
          extends: .base
          variables: {B: "2"}
        job:
          extends: .more
          script:
            - !reference [.base, script]
            - echo job
        """,
    )
    j = pr.job("job")
    assert j.job.script("before_script") == ["echo default"]
    assert j.job.script("script") == ["echo base", "echo job"]
    assert j.variables["A"].value == "1" and j.variables["B"].value == "2"


def test_rules_on_main_vs_feature(tmp_path):
    yml = """
        deploy:
          script: [echo deploy]
          rules:
            - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
              when: manual
    """
    assert run(tmp_path, yml).job("deploy").outcome.when == "manual"
    feature = run(tmp_path, yml, Scenario("f", "branch", "feature/x", "push"))
    assert not feature.job("deploy").included


def test_workflow_blocks_pipeline(tmp_path):
    pr = run(
        tmp_path,
        """
        workflow:
          rules:
            - if: $CI_PIPELINE_SOURCE == "merge_request_event"
        job:
          script: [echo]
        """,
    )
    assert not pr.runs and not pr.job("job").included


def test_variable_precedence(tmp_path):
    pr = run(
        tmp_path,
        """
        variables: {X: global, Y: global}
        job:
          variables: {X: job}
          script: [echo $X $Y $Z]
          rules:
            - if: $CI_COMMIT_BRANCH
              variables: {Y: rule}
        """,
        external_vars={"Z": ExternalVar("Z", "settings")},
    )
    v = pr.job("job").variables
    assert (v["X"].value, v["X"].source) == ("job", "job")
    assert (v["Y"].value, v["Y"].source) == ("rule", "rules")
    assert v["Z"].source == "cicd"
    assert codes(pr, "job") == []


def test_undefined_after_script_and_guard(tmp_path):
    pr = run(
        tmp_path,
        """
        job:
          script:
            - URL=https://x
            - |
              : "${TOKEN:?TOKEN is required}"
            - curl -H "$TOKEN" "$URL" "$MISSING"
          after_script:
            - echo $URL
        """,
    )
    c = codes(pr, "job")
    assert "undefined-var" in c  # $MISSING
    assert "after-script-scope" in c  # $URL
    assert "undefined-guarded" in c  # $TOKEN is guarded, but nothing defines it
    uses = {u.name: u for u in pr.job("job").uses if u.section == "script"}
    assert uses["TOKEN"].protection in ("guard", "guarded")


def test_unavailable_predefined_and_protected(tmp_path):
    yml = """
        job:
          script: [echo $CI_COMMIT_TAG $SECRET]
    """
    ext = {"SECRET": ExternalVar("SECRET", protected=True)}
    pr = run(tmp_path, yml, Scenario("f", "branch", "feature", "push"), external_vars=ext)
    msgs = [f.message for f in pr.job("job").findings]
    assert any("CI_COMMIT_TAG" in m and "tag pipelines" in m for m in msgs)
    assert any("SECRET" in m and "protected" in m for m in msgs)
    pr = run(tmp_path, yml, Scenario("t", "tag", "v1", "push"), external_vars=ext, protected_tags=["v*"])
    assert codes(pr, "job") == []


def test_required_vars_pattern_and_strict(tmp_path):
    template = (Path(__file__).parent.parent / "templates" / "require-vars.yml").read_text()
    yml = """
        include: /require-vars.yml
        guarded:
          variables: {REQUIRED_VARS: "TOKEN"}
          before_script: [!reference [.require-vars, script]]
          script: [deploy --token "$TOKEN"]
        unguarded:
          script: [deploy --token "$TOKEN"]
    """
    ext = {"TOKEN": ExternalVar("TOKEN")}
    pr = run(tmp_path, yml, files={"require-vars.yml": template}, external_vars=ext, strict=True)
    assert codes(pr, "guarded") == []
    assert codes(pr, "unguarded") == ["unguarded"]


def test_needs_on_excluded_job(tmp_path):
    pr = run(
        tmp_path,
        """
        lint:
          script: [lint]
          rules: [{if: $CI_PIPELINE_SOURCE == "merge_request_event"}]
        deploy:
          script: [deploy]
          needs: [lint]
        """,
    )
    assert "needs-excluded" in codes(pr, "deploy")


def test_dotenv_variables(tmp_path):
    pr = run(
        tmp_path,
        """
        stages: [build, deploy]
        build:
          stage: build
          script: ['echo "VERSION=1.2" >> build.env']
          artifacts: {reports: {dotenv: build.env}}
        deploy:
          stage: deploy
          script: [echo $VERSION]
        """,
    )
    assert pr.job("deploy").variables["VERSION"].source == "dotenv"
    assert codes(pr, "deploy") == []


def test_script_entry_parsed_as_mapping(tmp_path):
    pr = run(
        tmp_path,
        """
        job:
          script:
            - echo Status: ok
        """,
    )
    assert "script-not-string" in codes(pr, "job")
    pr = run(tmp_path, 'job:\n  script:\n    - : "${TOKEN:?}"\n')
    assert "yaml-error" in codes(pr)


def test_rules_changes_and_exists(tmp_path):
    yml = """
        job:
          script: [x]
          rules:
            - exists: ["**/Dockerfile"]
              changes: ["src/**/*"]
    """
    files = {"app/Dockerfile": "FROM x"}
    sc = Scenario("m", "branch", "main", "push", changed_files=["src/a/b.py"])
    assert run(tmp_path, yml, sc, files=files).job("job").included
    sc = Scenario("m", "branch", "main", "push", changed_files=["docs/readme.md"])
    assert not run(tmp_path, yml, sc, files=files).job("job").included


def test_parallel_matrix(tmp_path):
    pr = run(
        tmp_path,
        """
        test:
          script: [echo $PY]
          parallel:
            matrix:
              - PY: ["3.11", "3.12"]
        """,
    )
    assert [j.name for j in pr.jobs] == ["test: [3.11]", "test: [3.12]"]
    assert codes(pr) == []


def test_example_pipeline_findings():
    from glint.settings import load_settings

    s = load_settings(EXAMPLES)
    pr = analyse(Simulator(EXAMPLES / ".gitlab-ci.yml", s, Scenario("main", "branch", "main", "push")).run())
    c = codes(pr)
    for expected in ("after-script-scope", "unavailable-var", "undefined-var", "rules-unknown-var", "blank-var"):
        assert expected in c


# --- regressions from the PR #1 review ----------------------------------------


def test_self_matching_include_glob_terminates(tmp_path):
    pr = run(
        tmp_path,
        """
        include: "ci/*.yml"
        """,
        files={
            "ci/a.yml": 'include: "ci/*.yml"\na:\n  script: [a]\n',
            "ci/b.yml": 'include: "ci/*.yml"\nb:\n  script: [b]\n',
        },
    )
    assert {j.name for j in pr.jobs} == {"a", "b"}
    assert "include-cycle" in codes(pr)


def test_except_without_only_still_defaults_to_branches_and_tags(tmp_path):
    yml = """
        job:
          script: [x]
          except: [main]
    """
    mr = Scenario("mr", "mr", "feature", "merge_request_event", target_branch="main")
    assert not run(tmp_path, yml, mr).job("job").included
    assert run(tmp_path, yml, Scenario("f", "branch", "feature", "push")).job("job").included
    assert not run(tmp_path, yml).job("job").outcome.included  # main is excluded


def test_changes_always_true_for_tags(tmp_path):
    yml = """
        job:
          script: [x]
          rules:
            - changes: ["src/**/*"]
    """
    sc = Scenario("t", "tag", "v1", "push", changed_files=["README.md"])
    assert run(tmp_path, yml, sc).job("job").included


def test_environment_from_rules_variables_selects_scoped_cicd_vars(tmp_path):
    pr = run(
        tmp_path,
        """
        deploy:
          environment: $TARGET_ENV
          script: [echo $DB_URL]
          rules:
            - if: $CI_COMMIT_BRANCH == "main"
              variables: {TARGET_ENV: production}
        """,
        external_vars={"DB_URL": ExternalVar("DB_URL", "prod-db", environment_scope="production")},
    )
    j = pr.job("deploy")
    assert j.variables["CI_ENVIRONMENT_NAME"].value == "production"
    assert j.variables["DB_URL"].value == "prod-db"
    assert codes(pr, "deploy") == []


def test_same_cicd_var_in_several_environment_scopes(tmp_path):
    yml = """
        staging:
          environment: staging
          script: [echo $URL]
        production:
          environment: production
          script: [echo $URL]
        review:
          environment: review/feature
          script: [echo $URL]
    """
    ext = {
        "URL": [
            ExternalVar("URL", "https://prod", environment_scope="production"),
            ExternalVar("URL", "https://staging", environment_scope="staging"),
            ExternalVar("URL", "https://default", environment_scope="*"),
        ]
    }
    pr = run(tmp_path, yml, external_vars=ext)
    assert pr.job("staging").variables["URL"].value == "https://staging"
    assert pr.job("production").variables["URL"].value == "https://prod"
    assert pr.job("review").variables["URL"].value == "https://default"  # most specific match wins
    assert codes(pr) == []


def test_glint_yml_and_export_merge_scopes(tmp_path):
    import json

    from glint.settings import load_settings

    (tmp_path / ".glint.yml").write_text(
        "variables:\n  URL:\n    - {environment_scope: production}\n"
        "gitlab_variables_json: [vars.json]\n"
    )
    (tmp_path / "vars.json").write_text(
        json.dumps([{"key": "URL", "value": "x", "environment_scope": "staging"},
                    {"key": "URL", "value": "y", "environment_scope": "production"}])
    )
    s = load_settings(tmp_path)
    assert sorted(v.environment_scope for v in s.external_vars["URL"]) == ["production", "staging"]


def test_sourced_dot_directory_file(tmp_path):
    pr = run(
        tmp_path,
        """
        job:
          script:
            - source .ci/env.sh
            - echo $FROM_ENV
        """,
        files={".ci/env.sh": "FROM_ENV=1\n"},
    )
    assert codes(pr, "job") == []


def test_sourced_file_outside_repo_is_not_read(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.sh").write_text("SECRET_DEF=1\n")
    pr = run(repo, "job:\n  script:\n    - source ../outside.sh\n    - echo $SECRET_DEF\n")
    assert "possibly-undefined" in codes(pr, "job")


def test_dotenv_through_needs_on_matrix_job(tmp_path):
    pr = run(
        tmp_path,
        """
        stages: [build, deploy]
        build:
          stage: build
          parallel:
            matrix:
              - ARCH: [amd64, arm64]
          script: ['echo "IMAGE_TAG=x" >> build.env']
          artifacts: {reports: {dotenv: build.env}}
        deploy:
          stage: deploy
          needs: [build]
          script: [echo $IMAGE_TAG]
        """,
    )
    assert pr.job("deploy").variables["IMAGE_TAG"].source == "dotenv"
    assert codes(pr, "deploy") == []


@pytest.mark.parametrize(
    "script,is_guard",
    [
        ('[ -z "$X" ] && exit 1', True),
        ('[ -n "$X" ] || exit 1', True),
        ('[ -z "$X" ] && { echo missing; exit 1; }', True),
        ('if [ -z "$X" ]; then echo missing; exit 1; fi', True),
        ('[ -n "$X" ] && exit 0', False),
        ('[ -z "$X" ] || exit 1', False),
        ('if [ -n "$X" ]; then exit 0; fi', False),
        ('if [ -z "$X" ]; then echo hi; else exit 1; fi', False),
    ],
)
def test_guard_test_direction(script, is_guard):
    use = scan(script).uses[0]
    assert use.tested and use.guard_test is is_guard


def test_mr_target_branch_protected_uses_wildcards(tmp_path):
    from glint.predefined import pipeline_variables

    s = Settings(repo_root=tmp_path, protected_branches=["main", "release/*"])
    sc = Scenario("mr", "mr", "fix", "merge_request_event", target_branch="release/1.0")
    assert pipeline_variables(sc, s)["CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED"] == "true"


def test_html_offsets_are_utf16(tmp_path):
    from glint.report_html import to_data

    pr = run(tmp_path, 'job:\n  script:\n    - echo "🚀 deploying $FOO"\n', external_vars={"FOO": ExternalVar("FOO")})
    text = 'echo "🚀 deploying $FOO"'
    use = next(u for u in to_data(pr)["jobs"][0]["uses"] if u["name"] == "FOO")
    as_js = text.encode("utf-16-le")
    assert as_js[use["start"] * 2 : use["end"] * 2].decode("utf-16-le") == "$FOO"
