"""Self-contained interactive HTML report."""

from __future__ import annotations

import json
from datetime import datetime

from .pipeline import PipelineResult
from .predefined import RUNTIME
from .report_text import all_findings, status_of


def to_data(pr: PipelineResult) -> dict:
    jobs = []
    for jr in pr.jobs:
        j = jr.job
        _, label, _ = status_of(jr)
        job_findings = [f.to_dict() for f in pr.findings if f.job in (jr.name, j.base_name)] + [f.to_dict() for f in jr.findings]
        needs = jr.outcome.needs if jr.outcome.needs is not None else j.config.get("needs")
        jobs.append(
            {
                "name": jr.name,
                "stage": j.stage,
                "included": jr.included,
                "status": label,
                "when": jr.outcome.when,
                "allow_failure": jr.allow_failure() if jr.included else None,
                "reason": jr.outcome.reason,
                "uncertain": jr.outcome.uncertain,
                "origin": j.origin,
                "extends": j.extends,
                "trace": [t.to_dict() for t in jr.outcome.trace],
                "uses_rules": "rules" in j.config,
                "variables": [
                    e.to_dict()
                    for e in sorted(jr.variables.values(), key=lambda e: e.name)
                    if not (e.source == "predefined" and e.value == RUNTIME)
                ],
                "scripts": {
                    k: [x if isinstance(x, str) else repr(x) for x in j.script(k)]
                    for k in ("before_script", "script", "after_script")
                },
                "uses": [u.to_dict() for u in jr.uses],
                "needs": [
                    (n.get("job") or n.get("pipeline") or str(n)) if isinstance(n, dict) else str(n)
                    for n in (needs or [])
                ]
                if needs is not None
                else None,
                "extra": {k: j.config[k] for k in ("image", "environment", "trigger", "artifacts", "tags") if k in j.config},
                "findings": job_findings,
            }
        )
    fs = all_findings(pr)
    return {
        "scenario": {
            "label": pr.scenario.label,
            "describe": pr.scenario.describe(),
            "protected": pr.scenario.protected(pr.settings),
            "pipeline_vars": pr.scenario.pipeline_vars,
        },
        "settings": {
            "file": pr.settings.config_file.name if pr.settings.config_file else None,
            "external_vars": len(pr.settings.external_vars),
            "strict": pr.settings.strict,
        },
        "files": pr.config.files,
        "stages": pr.config.stages,
        "workflow": {
            "has_rules": bool(pr.config.workflow.get("rules")),
            "runs": pr.workflow.included,
            "reason": pr.workflow.reason,
            "trace": [t.to_dict() for t in pr.workflow.trace],
            "variables": {k: v.value for k, v in pr.workflow_variables.items()},
        },
        "jobs": jobs,
        "findings": [f.to_dict() for f in fs],
    }


def render(results: list[PipelineResult], filename: str) -> str:
    data = {
        "file": filename,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scenarios": [to_data(pr) for pr in results],
    }
    blob = json.dumps(data, default=str).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", blob).replace("__TITLE__", f"glint · {filename}")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --panel-2: #f1f1ee; --text: #1d1d1b; --muted: #6b6b66; --border: #e2e1dc;
  --accent: #5b5bd6; --ok: #1f8a4c; --ok-bg: #e5f4ea; --warn: #a15c00; --warn-bg: #fdf0d9; --err: #c62f2f; --err-bg: #fbe5e3;
  --info: #2a6f9b; --info-bg: #e3eff7; --manual: #8a5a00; --skip: #9a9a94; --code-bg: #f4f3ef;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #161615; --panel: #1f1f1d; --panel-2: #262624; --text: #ecebe6; --muted: #9c9b94; --border: #34342f;
  --accent: #8f8ff0; --ok: #5cc98a; --ok-bg: #173323; --warn: #e6a54a; --warn-bg: #3a2c12; --err: #f07a72; --err-bg: #3c1d1b;
  --info: #79b8e0; --info-bg: #17303f; --manual: #e6b85c; --skip: #6f6e68; --code-bg: #1a1a18;
} }
:root[data-theme="dark"] {
  --bg: #161615; --panel: #1f1f1d; --panel-2: #262624; --text: #ecebe6; --muted: #9c9b94; --border: #34342f;
  --accent: #8f8ff0; --ok: #5cc98a; --ok-bg: #173323; --warn: #e6a54a; --warn-bg: #3a2c12; --err: #f07a72; --err-bg: #3c1d1b;
  --info: #79b8e0; --info-bg: #17303f; --manual: #e6b85c; --skip: #6f6e68; --code-bg: #1a1a18;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding: 16px 20px 0; border-bottom: 1px solid var(--border); background: var(--panel); }
h1 { font-size: 18px; margin: 0; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
h1 small { color: var(--muted); font-weight: 400; font-size: 13px; }
.tabs { display: flex; gap: 2px; overflow-x: auto; margin-top: 12px; }
.tab { border: 0; background: none; color: var(--muted); padding: 8px 12px; font: inherit; cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--text); border-bottom-color: var(--accent); font-weight: 600; }
.tab .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-left: 6px; vertical-align: middle; }
main { padding: 16px 20px 40px; max-width: 1600px; margin: 0 auto; }
.bar { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin-bottom: 14px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; background: var(--panel-2); font-size: 12.5px; }
.chip b { font-variant-numeric: tabular-nums; }
.chip.err { background: var(--err-bg); color: var(--err); } .chip.warn { background: var(--warn-bg); color: var(--warn); }
.chip.ok { background: var(--ok-bg); color: var(--ok); }
.views { display: flex; gap: 6px; margin-left: auto; }
.views button, .toggle { border: 1px solid var(--border); background: var(--panel); color: var(--text); border-radius: 6px; padding: 4px 10px; font: inherit; font-size: 13px; cursor: pointer; }
.views button.active { background: var(--text); color: var(--bg); border-color: var(--text); }
.banner { padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; background: var(--panel); border: 1px solid var(--border); }
.banner.bad { border-color: var(--err); background: var(--err-bg); }
.banner details summary { cursor: pointer; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr); gap: 16px; align-items: start; }
@media (max-width: 1000px) { .layout { grid-template-columns: 1fr; } }
.stages { display: flex; flex-wrap: wrap; gap: 12px; }
.stage { min-width: 0; flex: 1 1 170px; }
.stage h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 8px; }
.card { display: block; width: 100%; text-align: left; background: var(--panel); border: 1px solid var(--border); border-left: 4px solid var(--ok); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; color: inherit; font: inherit; }
.card:hover { border-color: var(--accent); }
.card.sel { outline: 2px solid var(--accent); outline-offset: 1px; }
.card.manual { border-left-color: var(--manual); }
.card.skipped { border-left-color: var(--skip); opacity: .6; border-style: dashed; border-left-style: solid; }
.card .nm { font-weight: 600; word-break: break-word; }
.card .st { font-size: 12px; color: var(--muted); display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 2px; }
.pill { font-size: 11px; padding: 0 7px; border-radius: 999px; background: var(--panel-2); color: var(--muted); }
.pill.runs, .pill.always { background: var(--ok-bg); color: var(--ok); }
.pill.manual { background: var(--warn-bg); color: var(--manual); }
.pill.e { background: var(--err-bg); color: var(--err); } .pill.w { background: var(--warn-bg); color: var(--warn); }
.pill.assumed { background: var(--info-bg); color: var(--info); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow: auto; }
@media (max-width: 1000px) { .panel { position: static; max-height: none; } }
.panel h2 { margin: 0 0 4px; font-size: 17px; word-break: break-word; }
.panel h4 { margin: 18px 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.meta { color: var(--muted); font-size: 12.5px; }
.empty { color: var(--muted); padding: 30px 10px; text-align: center; }
.rule { border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; margin-bottom: 6px; font-size: 13px; }
.rule.m { border-color: var(--ok); background: var(--ok-bg); }
.rule .d { font-family: var(--mono); font-size: 12px; color: var(--muted); white-space: pre-wrap; word-break: break-word; }
.rule.m .d { color: var(--text); }
pre.code { margin: 0 0 6px; background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; font: 12.5px/1.5 var(--mono); white-space: pre-wrap; word-break: break-word; position: relative; }
pre.code .n { position: absolute; right: 8px; top: 4px; color: var(--muted); font-size: 11px; }
.v { border-radius: 3px; padding: 0 1px; cursor: help; }
.v.ok { color: var(--ok); } .v.info { color: var(--info); background: var(--info-bg); }
.v.warning { color: var(--warn); background: var(--warn-bg); font-weight: 600; }
.v.error { color: var(--err); background: var(--err-bg); font-weight: 700; text-decoration: underline wavy; }
.sec-note { font-size: 12px; color: var(--muted); font-weight: 400; text-transform: none; letter-spacing: 0; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { text-align: left; padding: 5px 6px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
td.mono, .mono { font-family: var(--mono); word-break: break-all; }
.ovr { color: var(--muted); font-size: 11.5px; }
.ovr s { opacity: .8; }
.finding { display: grid; grid-template-columns: 74px 1fr; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.sev { font-size: 11px; font-weight: 700; text-transform: uppercase; padding: 2px 6px; border-radius: 4px; text-align: center; height: fit-content; }
.sev.error { background: var(--err-bg); color: var(--err); } .sev.warning { background: var(--warn-bg); color: var(--warn); } .sev.info { background: var(--info-bg); color: var(--info); }
.finding .code { color: var(--muted); font-family: var(--mono); font-size: 11.5px; }
.finding a { color: var(--accent); cursor: pointer; text-decoration: none; font-weight: 600; }
.legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.matrix td, .matrix th { white-space: nowrap; }
.matrix td.c-runs, .matrix td.c-always { color: var(--ok); } .matrix td.c-manual { color: var(--manual); } .matrix td.c-skipped { color: var(--skip); }
.matrix td.c-err { color: var(--err); font-weight: 600; }
.matrix tr:hover td { background: var(--panel-2); }
.box { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; overflow-x: auto; }
input[type=search] { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-2); color: var(--text); font: inherit; margin-bottom: 6px; }
.uses { display: flex; flex-wrap: wrap; gap: 4px; }
</style>
</head>
<body>
<header>
  <h1>glint <small id="sub"></small></h1>
  <div class="tabs" id="tabs" role="tablist"></div>
</header>
<main id="main"></main>
<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const st = { sc: 0, job: null, view: 'pipeline', skipped: true, varFilter: '', showPredef: false };
try { const s = JSON.parse(localStorage.getItem('glint-ui') || '{}'); if (typeof s.skipped === 'boolean') st.skipped = s.skipped; } catch (e) {}
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cnt = (fs, s) => fs.filter(f => f.severity === s).length;
const jobFindings = (j) => j.findings || [];

function renderTabs() {
  $('#sub').textContent = `${DATA.file} · generated ${DATA.generated}`;
  $('#tabs').innerHTML = DATA.scenarios.map((s, i) => {
    const e = cnt(s.findings, 'error'), w = cnt(s.findings, 'warning');
    const col = e ? 'var(--err)' : w ? 'var(--warn)' : 'var(--ok)';
    return `<button class="tab ${i === st.sc ? 'active' : ''}" role="tab" data-i="${i}">${esc(s.scenario.label)}<span class="dot" style="background:${col}"></span></button>`;
  }).join('');
  document.querySelectorAll('.tab').forEach(b => b.onclick = () => { st.sc = +b.dataset.i; render(); });
}

function summary(S) {
  const inc = S.jobs.filter(j => j.included);
  const e = cnt(S.findings, 'error'), w = cnt(S.findings, 'warning');
  return `<div class="bar">
    <span class="chip">${esc(S.scenario.describe)}</span>
    <span class="chip">protected ref:&nbsp;<b>${S.scenario.protected ? 'yes' : 'no'}</b></span>
    <span class="chip ok"><span><b>${inc.length}</b> of ${S.jobs.length} jobs run</span></span>
    <span class="chip ${e ? 'err' : ''}"><span><b>${e}</b> errors</span></span>
    <span class="chip ${w ? 'warn' : ''}"><span><b>${w}</b> warnings</span></span>
    <span class="chip" title="CI/CD settings variables glint knows about">${S.settings.file ? esc(S.settings.file) + ' · ' + S.settings.external_vars + ' CI/CD vars' : 'no .glint.yml - CI/CD vars unknown'}</span>
    <div class="views">${['pipeline','findings','compare'].map(v => `<button data-v="${v}" class="${st.view === v ? 'active' : ''}">${v[0].toUpperCase() + v.slice(1)}</button>`).join('')}</div>
  </div>`;
}

function workflowBanner(S) {
  const wf = S.workflow;
  if (!wf.has_rules) return '';
  const rules = wf.trace.map(t => `<div class="rule ${t.matched ? 'm' : ''}"><b>${t.matched ? '✔' : '✘'} #${t.index}</b><div class="d">${t.details.map(esc).join('\n')}</div></div>`).join('');
  const vars = Object.keys(wf.variables).length ? `<div class="meta">sets: ${Object.entries(wf.variables).map(([k, v]) => `<span class="mono">${esc(k)}=${esc(v)}</span>`).join(', ')}</div>` : '';
  return `<div class="banner ${wf.runs ? '' : 'bad'}"><details ${wf.runs ? '' : 'open'}><summary><b>workflow:rules</b> - ${wf.runs ? 'pipeline is created' : '<b>no pipeline is created</b>'} (${esc(wf.reason)})</summary><div style="margin-top:8px">${rules}${vars}</div></details></div>`;
}

function card(j) {
  const e = cnt(jobFindings(j), 'error'), w = cnt(jobFindings(j), 'warning');
  return `<button class="card ${j.status} ${st.job === j.name ? 'sel' : ''}" data-job="${esc(j.name)}">
    <div class="nm">${esc(j.name)}</div>
    <div class="st"><span class="pill ${j.status}">${j.status}</span>${j.uncertain ? '<span class="pill assumed" title="depends on rules:changes/exists assumptions">assumed</span>' : ''}${e ? `<span class="pill e">${e} err</span>` : ''}${w ? `<span class="pill w">${w} warn</span>` : ''}</div>
    ${j.included ? '' : `<div class="st">${esc(j.reason)}</div>`}
  </button>`;
}

function pipelineView(S) {
  const stages = [...S.stages, ...[...new Set(S.jobs.map(j => j.stage))].filter(s => !S.stages.includes(s))];
  const cols = stages.map(s => {
    const jobs = S.jobs.filter(j => j.stage === s && (st.skipped || j.included));
    if (!jobs.length) return '';
    return `<div class="stage"><h3>${esc(s)}</h3>${jobs.map(card).join('')}</div>`;
  }).join('');
  const job = S.jobs.find(j => j.name === st.job);
  return `<div class="layout">
    <div>
      <div class="bar"><label class="meta"><input type="checkbox" id="skp" ${st.skipped ? 'checked' : ''}> show skipped jobs</label></div>
      <div class="stages">${cols || '<div class="empty">No jobs</div>'}</div>
    </div>
    <div class="panel" id="panel">${job ? jobDetail(S, job) : '<div class="empty">Select a job to see why it runs, its full scripts and its variables.</div>'}</div>
  </div>`;
}

function highlight(text, uses) {
  let out = '', pos = 0;
  for (const u of [...uses].sort((a, b) => a.start - b.start)) {
    if (u.start < pos) continue;
    out += esc(text.slice(pos, u.start));
    const tip = `$${u.name}: ${u.message}${u.protection ? ` [${u.protection}]` : ''}`;
    out += `<span class="v ${u.severity}" title="${esc(tip)}">${esc(text.slice(u.start, u.end))}</span>`;
    pos = u.end;
  }
  return out + esc(text.slice(pos));
}

function jobDetail(S, j) {
  const af = j.allow_failure;
  let h = `<h2>${esc(j.name)}</h2>
    <div class="meta">stage <b>${esc(j.stage)}</b> · <span class="pill ${j.status}">${j.status}</span>${j.included ? ` · when: ${esc(j.when)} · allow_failure: ${esc(JSON.stringify(af))}` : ''}</div>
    ${j.origin.length ? `<div class="meta">defined in ${j.origin.map(o => `<span class="mono">${esc(o)}</span>`).join(', ')}</div>` : ''}
    ${j.extends.length ? `<div class="meta">extends ${[...j.extends].reverse().map(esc).join(' → ')}</div>` : ''}`;

  const fs = jobFindings(j);
  if (fs.length) h += `<h4>Findings</h4>` + fs.map(findingRow).join('');

  h += `<h4>Why ${j.included ? 'it runs' : "it doesn't run"} <span class="sec-note">- ${esc(j.reason)}</span></h4>`;
  h += j.trace.map(t => {
    const r = (t.rule && typeof t.rule === 'object' && j.uses_rules) ? Object.entries(t.rule).filter(([k]) => ['when','allow_failure','variables','needs'].includes(k)).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(' · ') : '';
    return `<div class="rule ${t.matched ? 'm' : ''}"><b>${t.matched ? '✔' : '✘'} ${j.uses_rules ? '#' + t.index : 'only/except'}</b> <span class="meta">${esc(r)}</span>${t.uncertain ? ' <span class="pill assumed">assumption</span>' : ''}<div class="d">${t.details.map(esc).join('\n')}</div></div>`;
  }).join('') || '<div class="meta">(no rules)</div>';

  for (const key of ['before_script', 'script', 'after_script']) {
    const entries = j.scripts[key];
    if (!entries.length) continue;
    const note = key === 'after_script' ? 'separate shell - variables set in script are gone' : key === 'before_script' ? 'same shell as script' : '';
    h += `<h4>${key} ${note ? `<span class="sec-note">- ${note}</span>` : ''}</h4>`;
    h += entries.map((e, i) => `<pre class="code"><span class="n">${i}</span>${highlight(e, j.uses.filter(u => u.section === key && u.entry === i))}</pre>`).join('');
  }
  if (j.included) {
    h += `<div class="legend"><span><span class="v ok">$X</span> defined</span><span><span class="v info">$X</span> defaulted/tested</span><span><span class="v warning">$X</span> suspicious</span><span><span class="v error">$X</span> undefined</span><span>hover for details</span></div>`;
  }

  if (j.uses.length) {
    const seen = new Map();
    const rank = { error: 0, warning: 1, info: 2, ok: 3 };
    for (const u of j.uses) { if (u.source === 'shell') continue; const p = seen.get(u.name); if (!p || rank[u.severity] < rank[p.severity]) seen.set(u.name, u); }
    h += `<h4>Variables read by the scripts</h4><table><tr><th>variable</th><th>source</th><th>notes</th></tr>` +
      [...seen.values()].sort((a, b) => rank[a.severity] - rank[b.severity] || a.name.localeCompare(b.name)).map(u =>
        `<tr><td class="mono"><span class="v ${u.severity}">$${esc(u.name)}</span></td><td>${esc(u.source)}${u.protection ? `<div class="ovr">${esc(u.protection)}</div>` : ''}</td><td>${esc(u.message)}</td></tr>`).join('') + `</table>`;
  }

  const vars = j.variables.filter(v => (st.showPredef || v.source !== 'predefined') && (!st.varFilter || v.name.toLowerCase().includes(st.varFilter.toLowerCase())));
  h += `<h4>Variables in scope <span class="sec-note">- highest precedence wins</span></h4>
    <input type="search" id="vf" placeholder="filter variables…" value="${esc(st.varFilter)}">
    <label class="meta"><input type="checkbox" id="pd" ${st.showPredef ? 'checked' : ''}> include predefined</label>
    <table><tr><th>name</th><th>value</th><th>source</th></tr>${vars.map(v => {
      const val = v.value === null ? '<i>set (value unknown)</i>' : esc(JSON.stringify(v.value));
      const exp = v.expanded !== null && v.expanded !== v.value ? `<div class="ovr">→ ${esc(JSON.stringify(v.expanded))}</div>` : '';
      const ovr = v.overrides.filter(o => o.source !== 'predefined' || st.showPredef).map(o => `<div class="ovr">overrides <s>${esc(JSON.stringify(o.value))}</s> from ${esc(o.source_label)}</div>`).join('');
      return `<tr><td class="mono">${esc(v.name)}</td><td class="mono">${val}${exp}${v.expand ? '' : '<div class="ovr">expand: false</div>'}</td><td>${esc(v.source_label)}${v.detail ? `<div class="ovr">${esc(v.detail)}</div>` : ''}${ovr}</td></tr>`;
    }).join('')}</table>`;

  if (j.needs !== null) h += `<h4>needs</h4><div class="meta">${j.needs.length ? j.needs.map(n => `<span class="mono">${esc(n)}</span>`).join(', ') : '[] - starts immediately'}</div>`;
  for (const [k, v] of Object.entries(j.extra)) h += `<h4>${esc(k)}</h4><pre class="code">${esc(JSON.stringify(v, null, 2))}</pre>`;
  return h;
}

function findingRow(f) {
  const job = f.job ? `<a data-goto="${esc(f.job)}">${esc(f.job)}</a>` : '<b>(pipeline)</b>';
  return `<div class="finding"><span class="sev ${f.severity}">${f.severity}</span><div>${job}${f.location ? ` <span class="meta">@ ${esc(f.location)}</span>` : ''}${f.count > 1 ? ` <span class="meta">(×${f.count})</span>` : ''}<div>${esc(f.message)}</div><div class="code">${esc(f.code)}</div></div></div>`;
}

function findingsView(S) {
  return `<div class="box">${S.findings.length ? S.findings.map(findingRow).join('') : '<div class="empty">No problems found in this scenario.</div>'}</div>`;
}

function compareView() {
  const names = [];
  DATA.scenarios.forEach(s => s.jobs.forEach(j => { if (!names.includes(j.name)) names.push(j.name); }));
  const head = `<tr><th>job</th>${DATA.scenarios.map(s => `<th>${esc(s.scenario.label)}</th>`).join('')}</tr>`;
  const wf = `<tr><td><i>workflow</i></td>${DATA.scenarios.map(s => `<td class="${s.workflow.runs ? 'c-runs' : 'c-err'}">${s.workflow.runs ? 'pipeline created' : 'no pipeline'}</td>`).join('')}</tr>`;
  const rows = names.map(n => `<tr><td class="mono">${esc(n)}</td>${DATA.scenarios.map((s, i) => {
    const j = s.jobs.find(x => x.name === n);
    if (!j) return '<td>-</td>';
    const e = cnt(jobFindings(j), 'error');
    const icon = { runs: '✔', always: '✔', manual: '▶', skipped: '·', delayed: '◷', on_failure: '!' }[j.status] || '';
    return `<td class="${e ? 'c-err' : 'c-' + j.status}" title="${esc(j.reason)}"><a data-sc="${i}" data-job="${esc(n)}" style="cursor:pointer">${icon} ${j.status}${e ? ` · ${e} err` : ''}</a></td>`;
  }).join('')}</tr>`).join('');
  return `<div class="box"><table class="matrix">${head}${wf}${rows}</table><p class="meta">Click a cell to open that job in that scenario. Hover for the reason.</p></div>`;
}

function render() {
  renderTabs();
  const S = DATA.scenarios[st.sc];
  if (st.job && !S.jobs.some(j => j.name === st.job)) st.job = null;
  let body = summary(S) + workflowBanner(S);
  body += st.view === 'pipeline' ? pipelineView(S) : st.view === 'findings' ? findingsView(S) : compareView();
  $('#main').innerHTML = body;
  document.querySelectorAll('.views button').forEach(b => b.onclick = () => { st.view = b.dataset.v; render(); });
  document.querySelectorAll('.card').forEach(c => c.onclick = () => { st.job = c.dataset.job; render(); });
  document.querySelectorAll('[data-goto]').forEach(a => a.onclick = () => { st.job = a.dataset.goto; st.view = 'pipeline'; render(); });
  document.querySelectorAll('.matrix a').forEach(a => a.onclick = () => { st.sc = +a.dataset.sc; st.job = a.dataset.job; st.view = 'pipeline'; render(); });
  const skp = $('#skp'); if (skp) skp.onchange = () => { st.skipped = skp.checked; try { localStorage.setItem('glint-ui', JSON.stringify({ skipped: st.skipped })); } catch (e) {} render(); };
  rebindPanel();
  syncHash();
}
function rebindPanel() {
  const pd = $('#pd'); if (pd) pd.onchange = () => { st.showPredef = pd.checked; render(); };
  const vf = $('#vf');
  if (vf) vf.oninput = () => {
    st.varFilter = vf.value;
    const S = DATA.scenarios[st.sc];
    $('#panel').innerHTML = jobDetail(S, S.jobs.find(j => j.name === st.job));
    rebindPanel();
    const n = $('#vf'); n.focus(); n.setSelectionRange(n.value.length, n.value.length);
  };
  document.querySelectorAll('#panel [data-goto]').forEach(a => a.onclick = () => { st.job = a.dataset.goto; render(); });
}
function syncHash() {
  const p = new URLSearchParams({ sc: st.sc, view: st.view });
  if (st.job) p.set('job', st.job);
  history.replaceState(null, '', '#' + p.toString());
}
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.has('sc')) st.sc = Math.min(Math.max(+p.get('sc') || 0, 0), DATA.scenarios.length - 1);
  if (['pipeline', 'findings', 'compare'].includes(p.get('view'))) st.view = p.get('view');
  if (p.has('job')) st.job = p.get('job');
  return p.has('job');
}
if (!readHash()) {
  const S0 = DATA.scenarios[st.sc];
  const first = S0.jobs.find(j => j.findings.some(f => f.severity === 'error')) || S0.jobs.find(j => j.included);
  if (first) st.job = first.name;
}
window.addEventListener('hashchange', () => { readHash(); render(); });
render();
</script>
</body>
</html>
"""
