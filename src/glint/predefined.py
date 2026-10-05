"""GitLab predefined CI/CD variables and when each one is actually set."""

from __future__ import annotations

import fnmatch

from .model import Scenario, Settings, slugify

RUNTIME = "<runtime>"

# Set in every job of every pipeline. Values here are placeholders unless the
# scenario determines them (see pipeline_variables()).
ALWAYS = {
    "CI", "GITLAB_CI", "CI_API_V4_URL", "CI_API_GRAPHQL_URL", "CI_BUILDS_DIR", "CI_COMMIT_AUTHOR",
    "CI_COMMIT_BEFORE_SHA", "CI_COMMIT_DESCRIPTION", "CI_COMMIT_MESSAGE", "CI_COMMIT_MESSAGE_IS_TRUNCATED",
    "CI_COMMIT_REF_NAME", "CI_COMMIT_REF_PROTECTED", "CI_COMMIT_REF_SLUG", "CI_COMMIT_SHA",
    "CI_COMMIT_SHORT_SHA", "CI_COMMIT_TIMESTAMP", "CI_COMMIT_TITLE", "CI_CONCURRENT_ID",
    "CI_CONCURRENT_PROJECT_ID", "CI_CONFIG_PATH", "CI_DEFAULT_BRANCH", "CI_DEFAULT_BRANCH_SLUG",
    "CI_DEPENDENCY_PROXY_DIRECT_GROUP_IMAGE_PREFIX", "CI_DEPENDENCY_PROXY_GROUP_IMAGE_PREFIX",
    "CI_DEPENDENCY_PROXY_PASSWORD", "CI_DEPENDENCY_PROXY_SERVER", "CI_DEPENDENCY_PROXY_USER",
    "CI_JOB_ID", "CI_JOB_IMAGE", "CI_JOB_NAME", "CI_JOB_NAME_SLUG", "CI_JOB_STAGE", "CI_JOB_STATUS",
    "CI_JOB_TIMEOUT", "CI_JOB_TOKEN", "CI_JOB_URL", "CI_JOB_STARTED_AT", "CI_PAGES_DOMAIN",
    "CI_PAGES_HOSTNAME", "CI_PAGES_URL", "CI_PIPELINE_ID", "CI_PIPELINE_IID", "CI_PIPELINE_SOURCE",
    "CI_PIPELINE_URL", "CI_PIPELINE_CREATED_AT", "CI_PIPELINE_NAME", "CI_PROJECT_DIR", "CI_PROJECT_ID",
    "CI_PROJECT_NAME", "CI_PROJECT_NAMESPACE", "CI_PROJECT_NAMESPACE_ID", "CI_PROJECT_NAMESPACE_SLUG",
    "CI_PROJECT_PATH_SLUG", "CI_PROJECT_PATH", "CI_PROJECT_REPOSITORY_LANGUAGES", "CI_PROJECT_ROOT_NAMESPACE",
    "CI_PROJECT_TITLE", "CI_PROJECT_DESCRIPTION", "CI_PROJECT_URL", "CI_PROJECT_VISIBILITY",
    "CI_PROJECT_CLASSIFICATION_LABEL", "CI_REGISTRY", "CI_REGISTRY_IMAGE", "CI_REGISTRY_PASSWORD",
    "CI_REGISTRY_USER", "CI_REPOSITORY_URL", "CI_RUNNER_DESCRIPTION", "CI_RUNNER_EXECUTABLE_ARCH",
    "CI_RUNNER_ID", "CI_RUNNER_REVISION", "CI_RUNNER_SHORT_TOKEN", "CI_RUNNER_TAGS", "CI_RUNNER_VERSION",
    "CI_SERVER_FQDN", "CI_SERVER_HOST", "CI_SERVER_NAME", "CI_SERVER_PORT", "CI_SERVER_PROTOCOL",
    "CI_SERVER_SHELL_SSH_HOST", "CI_SERVER_SHELL_SSH_PORT", "CI_SERVER_REVISION", "CI_SERVER_URL",
    "CI_SERVER_VERSION_MAJOR", "CI_SERVER_VERSION_MINOR", "CI_SERVER_VERSION_PATCH", "CI_SERVER_VERSION",
    "CI_SERVER", "CI_SHARED_ENVIRONMENT", "CI_TEMPLATE_REGISTRY_HOST", "GITLAB_FEATURES",
    "GITLAB_USER_EMAIL", "GITLAB_USER_ID", "GITLAB_USER_LOGIN", "GITLAB_USER_NAME",
}

_MR_VARS = [
    "APPROVED", "ASSIGNEES", "DIFF_BASE_SHA", "DIFF_ID", "EVENT_TYPE", "DESCRIPTION",
    "DESCRIPTION_IS_TRUNCATED", "DRAFT", "ID", "IID", "LABELS", "MILESTONE", "PROJECT_ID", "PROJECT_PATH",
    "PROJECT_URL", "REF_PATH", "SOURCE_BRANCH_NAME", "SOURCE_BRANCH_PROTECTED", "SOURCE_BRANCH_SHA",
    "SOURCE_PROJECT_ID", "SOURCE_PROJECT_PATH", "SOURCE_PROJECT_URL", "SQUASH_ON_MERGE",
    "TARGET_BRANCH_NAME", "TARGET_BRANCH_PROTECTED", "TARGET_BRANCH_SHA", "TITLE",
]
_EXT_PR_VARS = [
    "IID", "SOURCE_REPOSITORY", "TARGET_REPOSITORY", "SOURCE_BRANCH_NAME", "SOURCE_BRANCH_SHA",
    "TARGET_BRANCH_NAME", "TARGET_BRANCH_SHA",
]

# Only set in some pipelines/jobs: name -> when it's set.
CONDITIONAL = {
    "CI_COMMIT_BRANCH": "only set in branch pipelines (not tag or merge request pipelines)",
    "CI_COMMIT_TAG": "only set in tag pipelines",
    "CI_COMMIT_TAG_MESSAGE": "only set in tag pipelines",
    "CI_RELEASE_DESCRIPTION": "only set in tag pipelines for a release",
    "CI_OPEN_MERGE_REQUESTS": "only set in branch/MR pipelines when the branch has an open merge request",
    "CI_PIPELINE_SCHEDULE_DESCRIPTION": "only set in scheduled pipelines",
    "CI_PIPELINE_TRIGGERED": "only set in triggered pipelines",
    "TRIGGER_PAYLOAD": "only set in pipelines triggered via webhook",
    "CI_TRIGGER_SHORT_TOKEN": "only set in pipelines triggered with a trigger token",
    "CHAT_CHANNEL": "only set in ChatOps pipelines",
    "CHAT_INPUT": "only set in ChatOps pipelines",
    "CHAT_USER_ID": "only set in ChatOps pipelines",
    "CI_ENVIRONMENT_NAME": "only set when the job defines an environment",
    "CI_ENVIRONMENT_SLUG": "only set when the job defines an environment",
    "CI_ENVIRONMENT_URL": "only set when the job defines environment:url",
    "CI_ENVIRONMENT_ACTION": "only set when the job defines an environment",
    "CI_ENVIRONMENT_TIER": "only set when the job defines an environment",
    "CI_NODE_INDEX": "only set when the job uses parallel",
    "CI_NODE_TOTAL": "only set when the job uses parallel",
    "CI_JOB_GROUP_NAME": "only set when the job uses parallel or a grouped name",
    "CI_JOB_MANUAL": "only set when the job was started manually",
    "CI_DEPLOY_USER": "only set if the project has a deploy token named gitlab-deploy-token",
    "CI_DEPLOY_PASSWORD": "only set if the project has a deploy token named gitlab-deploy-token",
    "CI_DEPLOY_FREEZE": "only set during a deploy freeze window",
    "CI_KUBERNETES_ACTIVE": "only set when a Kubernetes agent/cluster is configured",
    "KUBECONFIG": "only set when a Kubernetes agent/cluster is configured",
    "CI_DISPOSABLE_ENVIRONMENT": "only set on disposable executors (docker, kubernetes, ...)",
    "CI_HAS_OPEN_REQUIREMENTS": "only set when the project has open requirements",
    "CI_GITLAB_FIPS_MODE": "only set on FIPS-enabled instances",
    "CI_SERVER_TLS_CA_FILE": "only set when the runner has a custom CA",
    "CI_SERVER_TLS_CERT_FILE": "only set when the runner has a client certificate",
    "CI_SERVER_TLS_KEY_FILE": "only set when the runner has a client certificate",
    "CI_DEBUG_TRACE": "only set if you set it",
    "CI_DEBUG_SERVICES": "only set if you set it",
}
for _v in _MR_VARS:
    CONDITIONAL[f"CI_MERGE_REQUEST_{_v}"] = "only set in merge request pipelines"
for _v in _EXT_PR_VARS:
    CONDITIONAL[f"CI_EXTERNAL_PULL_REQUEST_{_v}"] = "only set in external pull request pipelines"

KNOWN = ALWAYS | set(CONDITIONAL)


def pipeline_variables(sc: Scenario, s: Settings) -> dict[str, str]:
    """Predefined variables (with values) for the whole pipeline in this scenario."""
    ns, _, name = s.project_path.rpartition("/")
    host = "gitlab.example.com"
    v = {k: RUNTIME for k in ALWAYS}
    v.update(
        {
            "CI": "true",
            "GITLAB_CI": "true",
            "CI_SERVER": "yes",
            "CI_SERVER_HOST": host,
            "CI_SERVER_FQDN": host,
            "CI_SERVER_URL": f"https://{host}",
            "CI_API_V4_URL": f"https://{host}/api/v4",
            "CI_PIPELINE_SOURCE": sc.source,
            "CI_DEFAULT_BRANCH": s.default_branch,
            "CI_DEFAULT_BRANCH_SLUG": slugify(s.default_branch),
            "CI_CONFIG_PATH": ".gitlab-ci.yml",
            "CI_PROJECT_PATH": s.project_path,
            "CI_PROJECT_PATH_SLUG": slugify(s.project_path),
            "CI_PROJECT_NAME": name,
            "CI_PROJECT_TITLE": name,
            "CI_PROJECT_NAMESPACE": ns,
            "CI_PROJECT_ROOT_NAMESPACE": ns.split("/")[0],
            "CI_PROJECT_URL": f"https://{host}/{s.project_path}",
            "CI_PROJECT_DIR": f"/builds/{s.project_path}",
            "CI_BUILDS_DIR": "/builds",
            "CI_REGISTRY": f"registry.{host}",
            "CI_REGISTRY_IMAGE": f"registry.{host}/{s.project_path}",
            "CI_REGISTRY_USER": "gitlab-ci-token",
            "CI_COMMIT_REF_PROTECTED": "true" if sc.protected(s) else "false",
        }
    )
    if sc.kind == "mr":
        ref = "refs/merge-requests/1/head"
        v["CI_COMMIT_REF_NAME"] = sc.ref
        v["CI_COMMIT_REF_SLUG"] = slugify(sc.ref)
        for k in _MR_VARS:
            v[f"CI_MERGE_REQUEST_{k}"] = RUNTIME
        v.update(
            {
                "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": sc.ref,
                "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": sc.target_branch or s.default_branch,
                "CI_MERGE_REQUEST_EVENT_TYPE": "detached",
                "CI_MERGE_REQUEST_IID": "1",
                "CI_MERGE_REQUEST_REF_PATH": ref,
                "CI_MERGE_REQUEST_DRAFT": "false",
                "CI_MERGE_REQUEST_LABELS": "",
                "CI_MERGE_REQUEST_SOURCE_BRANCH_PROTECTED": "false",
                "CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED": "true"
                if any(
                    fnmatch.fnmatchcase(sc.target_branch or s.default_branch, p)
                    for p in (s.protected_branches or [s.default_branch])
                )
                else "false",
                "CI_OPEN_MERGE_REQUESTS": f"{s.project_path}!1",
            }
        )
    else:
        v["CI_COMMIT_REF_NAME"] = sc.ref
        v["CI_COMMIT_REF_SLUG"] = slugify(sc.ref)
        if sc.kind == "tag":
            v["CI_COMMIT_TAG"] = sc.ref
            v["CI_COMMIT_TAG_MESSAGE"] = RUNTIME
        else:
            v["CI_COMMIT_BRANCH"] = sc.ref
    if sc.source == "schedule":
        v["CI_PIPELINE_SCHEDULE_DESCRIPTION"] = RUNTIME
    if sc.source in ("trigger", "pipeline", "parent_pipeline"):
        v["CI_PIPELINE_TRIGGERED"] = "true"
    if sc.source == "trigger":
        v["CI_TRIGGER_SHORT_TOKEN"] = RUNTIME
    if sc.source == "chat":
        for k in ("CHAT_CHANNEL", "CHAT_INPUT", "CHAT_USER_ID"):
            v[k] = RUNTIME
    return v


def job_variables(job_name: str, stage: str, job: dict, expand) -> dict[str, str]:
    """Predefined variables that depend on the job itself."""
    v = {
        "CI_JOB_NAME": job_name,
        "CI_JOB_NAME_SLUG": slugify(job_name),
        "CI_JOB_STAGE": stage,
    }
    env = job.get("environment")
    if isinstance(env, str):
        env = {"name": env}
    if isinstance(env, dict) and env.get("name"):
        name = expand(str(env["name"]))
        v["CI_ENVIRONMENT_NAME"] = name
        v["CI_ENVIRONMENT_SLUG"] = slugify(name)[:24]
        v["CI_ENVIRONMENT_ACTION"] = str(env.get("action", "start"))
        v["CI_ENVIRONMENT_TIER"] = str(env.get("deployment_tier", RUNTIME))
        if env.get("url"):
            v["CI_ENVIRONMENT_URL"] = expand(str(env["url"]))
    if job.get("parallel"):
        v["CI_NODE_INDEX"] = RUNTIME
        v["CI_NODE_TOTAL"] = RUNTIME
        v["CI_JOB_GROUP_NAME"] = RUNTIME
    if job.get("image"):
        img = job["image"]
        v["CI_JOB_IMAGE"] = expand(str(img.get("name") if isinstance(img, dict) else img))
    return v
