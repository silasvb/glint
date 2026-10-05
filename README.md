# glint

The GitLab CI linter with super-powers.

See how a GitLab CI pipeline will behave on `main` (or any other branch, tag, MR or schedule) without pushing to it:

- **Which jobs run, and why.** `workflow:rules`, job `rules` and `only`/`except` are evaluated with GitLab's expression semantics. Each `if:` is shown with the actual values substituted, e.g. `$CI_COMMIT_BRANCH(="main") == $CI_DEFAULT_BRANCH(="main") → true`.
- **The full resolved job.** You get `before_script`, `script` and `after_script` after `include`, `extends`, `!reference`, `default:` and `parallel:matrix` have been applied.
- **Every variable in scope.** Each one shows its value, its source (predefined, global, workflow, job, rules, dotenv, CI/CD settings) and what it overrides.
- **Undefined variables.** Every `$VAR` a script reads is traced to where it's set, or reported when nothing sets it.

## Install

```sh
uv tool install .        # or: pipx install .   /   pip install .
```

## Usage

```sh
glint show                          # push to the default branch: which jobs run, and why
glint show --job deploy             # one job in full: rules trace, variables, scripts
glint show --all                    # every job in full
glint vars                          # every variable each job reads, and its source
glint check                         # findings; exits 1 on errors (use it in CI)
glint check --all-scenarios         # ...for main, feature branch, MR, tag and schedule
glint matrix                        # which jobs run in each of those scenarios
glint html -o pipeline.html         # interactive report with all of the above
```

Scenario options apply to every command:

| option | effect |
|---|---|
| `--branch X` / `--tag v1` / `--mr src[:target]` | what's being simulated (default: push to default branch) |
| `--source schedule\|web\|api\|trigger\|…` | `CI_PIPELINE_SOURCE` |
| `--var KEY=VALUE` | pipeline variables (schedule/trigger/manual-run variables) |
| `--changed a,b` / `--changed-since origin/main` | evaluates `rules:changes` for real (otherwise assumed true and flagged as "assumed") |
| `--gitlab-vars vars.json` | CI/CD variables exported from GitLab (see below) |
| `--project group/proj=../path` | resolves `include: project:` from a local checkout |
| `--strict` | enforces the guard pattern (see below) |

`rules:exists` is checked against the files in the repo.

## Telling glint about CI/CD settings variables

Variables set in *Settings → CI/CD → Variables* aren't in the YAML, so glint can't see them. Without that information it reports them as undefined. Declare them in `.glint.yml` at the repo root (see [`examples/.glint.yml`](examples/.glint.yml)):

```yaml
default_branch: main
protected_branches: [main, "release/*"]
protected_tags: ["v*"]
variables:
  NPM_REGISTRY: https://npm.example.com
  DEPLOY_TOKEN: {protected: true, masked: true}     # only available on protected refs
  AWS_REGION: {environment_scope: production}       # only in jobs with that environment
  DB_URL:                                           # same key in several scopes: most specific wins
    - {environment_scope: production}
    - {environment_scope: "review/*"}
ignore: [SOME_VAR_SET_BY_THE_RUNNER_IMAGE]
```

You can also export them. `glab variable export > vars.json` produces a file to reference with `gitlab_variables_json: [vars.json]` or `--gitlab-vars`. glint only reads names and flags from that file and never keeps the values, but the file itself still contains secrets, so don't commit it.

Protection and environment scope are applied per scenario. A protected `DEPLOY_TOKEN` is therefore reported as "not set here" on an unprotected feature branch.

## What it detects

| code | meaning |
|---|---|
| `undefined-var` | script reads `$X` and nothing defines it (YAML, predefined, CI/CD settings, dotenv, earlier in the script) |
| `unavailable-var` | `$X` exists, but not in this scenario. Examples: `$CI_COMMIT_TAG` on a branch, `$CI_MERGE_REQUEST_IID` outside MRs, a protected variable on an unprotected ref, an env-scoped variable in the wrong environment |
| `after-script-scope` | `$X` is set in `script` but read in `after_script`, which runs in a fresh shell |
| `blank-var` | `$X` is defined in the YAML as `""` and used without a default |
| `rules-unknown-var` | `rules:if` references a variable nothing defines. Usually a typo: the condition can never be true |
| `needs-excluded` | a job `needs:` a job whose rules exclude it in this scenario. GitLab refuses to create the pipeline |
| `possibly-undefined` | `$X` might come from an upstream dotenv report or a sourced file glint couldn't read |
| `var-ref-undefined` | a `variables:` value references an undefined variable |
| `script-not-string` | a script line was parsed by YAML as a mapping (an unquoted `: `) |
| `unguarded` (strict) | an externally supplied variable is used without a guard |

The script scanner handles quoting, `${X:-default}`, `${X:?msg}`, `$(…)`, heredocs, `export`/`read`/`for`, `source ./file.sh` (it reads the file if it's in the repo) and dotenv reports written with `echo "X=…" >> file.env`. Uses guarded by `if [ -n "$X" ]` count as checked.

## The guard pattern

Anything that comes from outside the YAML can be unset or blank at runtime. That covers CI/CD settings variables, schedule/trigger variables and dotenv artifacts. Shell happily expands those to empty strings, so the job fails later with a confusing error, or doesn't fail at all. Check them up front instead.

**Inline**, for one or two variables. `:?` fails on both unset *and* blank:

```yaml
deploy:
  script:
    - |
      : "${DEPLOY_TOKEN:?DEPLOY_TOKEN must be set (protected - is this a protected branch?)}"
      : "${AWS_REGION:?AWS_REGION must be set}"
    - ./deploy.sh
```

Use a `- |` block. A bare `- : "${X:?}"` line is invalid YAML.

**Declarative**, for jobs with several requirements. Include [`templates/require-vars.yml`](templates/require-vars.yml), then:

```yaml
deploy:
  variables:
    REQUIRED_VARS: "DEPLOY_TOKEN AWS_REGION KUBE_SERVER"
  before_script:
    - !reference [.require-vars, script]
  script:
    - ./deploy.sh
```

The job then fails immediately and lists every missing variable at once.

**Enforcing it.** `glint check --strict` (or `strict: true` in `.glint.yml`) fails when a variable from CI/CD settings, pipeline variables or dotenv is used before it's guarded. Variables defined in the YAML or predefined by GitLab don't need guards. glint recognises `${X:?}`, `REQUIRED_VARS` (the name is configurable with `required_vars_name`) and `[ -z "$X" ] … exit`. Run it in CI:

```yaml
lint-ci:
  stage: .pre
  image: python:3.12-slim
  script:
    - pip install ./tools/glint      # or from your package index
    - glint check --all-scenarios --strict
  rules:
    - changes: [".gitlab-ci.yml", "ci/**/*", ".glint.yml"]
```

As an extra safety net, put `set -u` at the top of a script. The shell then aborts on *unset* variables (not blank ones).

## Limitations

- `remote:`, `template:` and `component:` includes aren't fetched. glint warns, and the jobs they define are missing. Map `project:` includes to local checkouts.
- `rules:changes` needs `--changed`/`--changed-since`. Otherwise it's assumed true, and the result is marked "assumed".
- The shell scanner is heuristic, not a full bash parser. Variables set via `eval` of dynamic strings, or by sourced files outside the repo, are reported as "possibly undefined". Use `ignore:` for any that are set by the runner image.
- Runtime-only values (`CI_COMMIT_SHA`, `CI_JOB_ID`, …) show as `<runtime>`.

## Development

```sh
uv sync && uv run pytest
uv run glint html examples/.gitlab-ci.yml -o examples/glint-report.html
```

`examples/` contains a pipeline with deliberate mistakes covering each finding type.
