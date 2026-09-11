/* Behaviour tabs (Habits, Delivery, Friction, Skills) and the Lab tab that
   drives exports and model-written analysis. Loaded before app.js; it only
   calls app.js helpers at render time, never at load time. */
'use strict';

window.AIDASH_EXTRA = (() => {
  /* ------------------------------------------------------------ helpers */
  const sessionLink = (s) => `<span class="name" title="${esc(s.session_id)}">${esc(s.title || s.session_id)}</span>`;
  const chip = (v, cls = '') => `<span class="chip ${cls}">${esc(v)}</span>`;
  const statusChip = (s) => chip(s, s === 'delivered' ? 'ok' : s === 'worked' ? 'warn' : '');
  const note = (cls, html) => `<div class="note ${cls}">${html}</div>`;
  const sessionCols = (extra) => [
    { k: 'title', label: 'Session', cls: 'name', fmt: (v, r) => sessionLink(r) },
    { k: 'project', label: 'Project' },
    ...extra,
    { k: 'started', label: 'Started', fmt: when },
  ];

  /* ----------------------------------------------------------- Habits */
  function renderHabits() {
    const ins = lazy('insights', '/api/insights');
    if (!ins) return spinner('analysing sessions');
    if (ins.error) return note('warn', `Could not load the analysis: ${esc(ins.error)}`);
    const h = ins.habits;
    let html = note('info', `<b>How you drive it.</b> Every figure here is about the prompts <i>you</i> typed
      (${int(h.prompts)} of them), not the model's own turns. Sub-agent instructions, SDK runs and
      harness notices are excluded.`);

    html += `<div class="metrics">
      ${kpi('Actions per prompt', h.actions_per_prompt + '×', `${h.tools_per_prompt}× tool calls per prompt`)}
      ${kpi('Prompts per session', int(h.prompts_per_session_median), `median · p90 ${int(h.prompts_per_session_p90)}`)}
      ${kpi('One-prompt sessions', int(h.one_prompt_sessions), `of ${int(h.interactive_sessions)} interactive`)}
      ${kpi('Short prompts', pct(h.short_prompt_pct), `${int(h.short_prompts)} under 25 chars`)}
      ${kpi('Corrections', pct(h.correction_pct), `${int(h.corrections)} "no, that's not…"`)}
      ${kpi('Premature sends', int(h.premature_sends), h.premature_sends ? `re-sent with more text · median ${dur(h.premature_median_gap_s * 1000)}` : 'same prompt re-sent with more text')}
      ${kpi('Interrupts', int(h.interrupts), 'you stopped a reply mid-run')}
      ${kpi('Context compactions', int(h.compactions), 'conversation ran out of room')}
      ${kpi('Delegation', pct(h.delegated_tool_share), `of tool calls ran in sub-agents · ${int(h.delegating_sessions)} sessions`)}
      ${kpi('Off-hours', pct(h.night_pct), `00–05 · weekend ${pct(h.weekend_pct)}`)}
    </div>`;

    html += `<div class="grid cols-2">
      ${card('What you ask for', 'Intent buckets, non-exclusive, share of typed prompts', chartSlot('hb-intents'))}
      ${card('How sessions open', 'The first thing you type', chartSlot('hb-openers'))}
    </div>`;
    draw('hb-intents', el => hbars(el, h.intents.slice(0, 14).map(i => ({ label: i.intent, value: i.n, sub: pct(i.pct) })),
      { fmt: int, color: PALETTE[0], sub: true }));
    draw('hb-openers', el => hbars(el, h.openers.slice(0, 12).map(x => ({ label: x.kind, value: x.n })),
      { fmt: int, color: PALETTE[2] }));

    html += `<div class="grid cols-2">
      ${card('Interactive vs headless', 'CLI sessions you sat in versus <span class="mono">claude -p</span> / SDK runs',
        table(h.by_entrypoint, [
          { k: 'entrypoint', label: 'Mode' },
          { k: 'sessions', label: 'Sessions', num: true, fmt: int, bar: true },
          { k: 'prompts', label: 'Prompts', num: true, fmt: int },
          { k: 'api_calls', label: 'API calls', num: true, fmt: int },
          { k: 'tool_calls', label: 'Tools', num: true, fmt: int },
          { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd, bar: true }]))}
      ${card('Slash commands', 'Commands and skills you invoke by hand', table(h.slash, [
          { k: 'name', label: 'Command', cls: 'mono' },
          { k: 'n', label: 'Uses', num: true, fmt: int, bar: true },
          { k: 'sessions', label: 'Sessions', num: true, fmt: int }], { max: 20 }))}
    </div>`;

    html += `<div class="grid cols-2">
      ${card('Delegation economics',
        `Cost per tool call when work is handed to sub-agents versus done in the main loop.
         Model time above wall-clock time (ratio &gt; 1) only happens when agents run in parallel.`,
        `<div class="metrics" style="margin-bottom:12px">
          ${kpi('Per tool call, delegating', usd(h.cost_per_tool_delegating), 'median session')}
          ${kpi('Per tool call, direct', usd(h.cost_per_tool_direct), 'median session')}
          ${kpi('Cost in sub-agents', pct(h.delegated_cost_share), 'share of estimated spend')}
        </div>` + table(h.parallel_sessions, sessionCols([
          { k: 'parallelism', label: 'Model÷wall', num: true, fmt: (v) => v + '×' },
          { k: 'turn_ms', label: 'Model time', num: true, fmt: dur },
          { k: 'wall_ms', label: 'Wall', num: true, fmt: dur },
          { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd }]), { max: 12 }))}
      ${card('Headless reruns', 'The same SDK prompt fired more than once on the same day',
        (h.headless_reruns.length
          ? `<p class="hint">${int(h.redundant_reruns)} redundant run(s) in range.</p>` +
            table(h.headless_reruns, [
              { k: 'date', label: 'Date' }, { k: 'project', label: 'Project' },
              { k: 'head', label: 'Prompt', cls: 'mono name' },
              { k: 'n', label: 'Runs', num: true, fmt: int, bar: true }], { max: 15 })
          : '<div class="empty">No repeated headless prompts</div>'))}
    </div>`;

    if (h.overnight_sessions.length) {
      html += card('Overnight-shaped sessions', 'Longer than 4 h wall-clock, started after 17:00 or before 04:00',
        table(h.overnight_sessions, sessionCols([
          { k: 'wall_ms', label: 'Wall', num: true, fmt: dur },
          { k: 'prompts', label: 'Prompts', num: true, fmt: int },
          { k: 'tool_calls', label: 'Tools', num: true, fmt: int },
          { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd, bar: true }])), 'full');
    }

    const hist = ins.history;
    if (hist && hist.prompts) {
      html += card('The longer record',
        `<span class="mono">history.jsonl</span> keeps every prompt line typed at the REPL and outlives
         transcript retention. ${int(hist.prompts_before_transcripts)} of its ${int(hist.prompts)} prompts predate
         your oldest surviving transcript (${esc(hist.transcript_first_day || '?')});
         ${int(hist.lost_project_count)} project folder(s) it mentions no longer exist under
         <span class="mono">~/.claude/projects</span>.`,
        `<div class="grid cols-2">
          <div>${chartSlot('hb-hist')}</div>
          <div>${table(hist.lost_projects, [
            { k: 'project', label: 'Project (gone)', cls: 'name', title: true },
            { k: 'prompts', label: 'Prompts', num: true, fmt: int, bar: true },
            { k: 'first_day', label: 'First' }, { k: 'last_day', label: 'Last' }], { max: 12 })}</div>
        </div>`, 'full');
      draw('hb-hist', el => barChart(el, {
        labels: hist.monthly.map(m => m.month), values: hist.monthly.map(m => m.prompts),
        fmt: int, tipLabel: 'prompts typed', color: PALETTE[4], height: 180,
      }));
    }
    return html;
  }

  /* --------------------------------------------------------- Delivery */
  function renderDelivery() {
    const ins = lazy('insights', '/api/insights');
    if (!ins) return spinner('recovering commits and tickets');
    if (ins.error) return note('warn', `Could not load the analysis: ${esc(ins.error)}`);
    const d = ins.delivery, o = DATA.claude.overview;
    let html = note('info', `<b>What shipped.</b> Commits are recovered from recorded <span class="mono">git</span>
      output — <span class="mono">[branch sha] subject</span> is printed by <span class="mono">git commit</span> and
      nothing else. Ticket keys are matched textually: a key in a commit subject proves delivery, a key in a prompt
      only means it was discussed. Nothing here observes review, CI or deploy.`);

    html += `<div class="metrics">
      ${kpi('Commits', int(d.commits), `+${num(d.commit_insertions)} / −${num(d.commit_deletions)} per git`)}
      ${kpi('Pushes', int(d.pushes), `${int(d.prs)} PR link(s) seen`)}
      ${kpi('Source lines written', num(d.source_added), 'code + markup only; docs/config/data excluded')}
      ${kpi('All lines written', num(o.lines_added), `${num(o.lines_removed)} removed`)}
      ${kpi('Tickets touched', int(d.ticket_count), `${int(d.tickets_delivered)} delivered · ${int(d.tickets_worked)} worked`)}
      ${kpi('Model working time', d.active_hours + ' h', `measured on ${int(d.timed_sessions)} sessions = ${pct(d.time_coverage_pct)} of tokens`)}
      ${kpi('Est. spend', usd(d.cost_usd), d.source_added ? `${usd(d.cost_usd / d.source_added * 1000)} per 1k source lines` : '')}
    </div>`;

    html += `<div class="grid cols-2">
      ${card('Commits per day', 'From recorded shell output', chartSlot('dl-daily'))}
      ${card('Lines written by category', 'Edits and writes, bucketed by file extension', chartSlot('dl-cat'))}
    </div>`;
    draw('dl-daily', el => barChart(el, {
      labels: d.commits_daily.map(r => shortDate(r.date)), values: d.commits_daily.map(r => r.commits),
      fmt: int, tipLabel: 'commits', color: PALETTE[4],
    }));
    draw('dl-cat', el => hbars(el, d.line_categories.map(c => ({ label: c.category, value: c.added, sub: `−${int(c.removed)}` })),
      { fmt: int, color: PALETTE[0], sub: true }));

    html += card('Value framing',
      `Everything above is measured. This box is <b>modelled</b>: change the two assumptions and the
       comparison recomputes. Quote the list-price ratio, never a subscription ratio — a flat fee does
       not scale with usage.`,
      `<div class="roi">
        <label>Manual lines / hour <input type="number" id="roi-lph" value="60" min="1" step="5"></label>
        <label>Hourly rate $ <input type="number" id="roi-rate" value="75" min="1" step="5"></label>
        <div class="metrics" id="roi-out"></div>
      </div>`, 'full');

    html += `<div class="grid cols-2">
      ${card('Commits by project', '', table(d.commits_by_project, [
          { k: 'project', label: 'Project', cls: 'name' },
          { k: 'commits', label: 'Commits', num: true, fmt: int, bar: true },
          { k: 'insertions', label: '+', num: true, fmt: int },
          { k: 'deletions', label: '−', num: true, fmt: int },
          { k: 'sessions', label: 'Sessions', num: true, fmt: int }]))}
      ${card('Git subcommands', 'What kind of git work Claude did for you', chartSlot('dl-git'))}
    </div>`;
    draw('dl-git', el => hbars(el, d.git_commands.map(g => ({ label: g.cmd, value: g.n })), { fmt: int, color: PALETTE[3] }));

    html += card('Ticket ledger',
      `Status is the strongest evidence seen: <b>delivered</b> = key in the subject of a commit that git
       confirmed; <b>worked</b> = key in a branch, title, prompt or file path; <b>referenced</b> = only looked up.
       ${d.dropped_prefixes.length ? `Prefixes filtered as noise: <span class="mono">${d.dropped_prefixes.map(esc).join(', ')}</span>.` : ''}`,
      table(d.tickets, [
        { k: 'key', label: 'Key', cls: 'mono' },
        { k: 'status', label: 'Status', fmt: statusChip },
        { k: 'sessions', label: 'Sessions', num: true, fmt: int, bar: true },
        { k: 'projects', label: 'Projects', fmt: (v) => esc((v || []).join(', ')) },
        { k: 'evidence', label: 'Evidence', fmt: (v) => Object.entries(v || {}).sort((a, b) => b[1] - a[1])
            .map(([k, n]) => `<span class="chip">${esc(k)} ${int(n)}</span>`).join(' ') },
        { k: 'first_day', label: 'First' }, { k: 'last_day', label: 'Last' },
      ], { max: 80 }), 'full');

    html += card('Recent commits', '', table(d.recent_commits, [
      { k: 'date', label: 'Date' }, { k: 'project', label: 'Project' },
      { k: 'branch', label: 'Branch', cls: 'mono' },
      { k: 'subject', label: 'Subject', cls: 'name', title: true },
      { k: 'files', label: 'Files', num: true, fmt: int },
      { k: 'insertions', label: '+', num: true, fmt: int },
      { k: 'deletions', label: '−', num: true, fmt: int },
    ], { max: 40 }), 'full');

    if (d.prs_list.length) {
      html += card('Pull requests seen', '', table(d.prs_list, [
        { k: 'date', label: 'Date' }, { k: 'project', label: 'Project' },
        { k: 'url', label: 'URL', cls: 'mono name', fmt: (v) => `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` }]), 'full');
    }
    return html;
  }

  function wireRoi(host) {
    const d = DATA.insights && DATA.insights.delivery;
    if (!d) return;
    const out = host.querySelector('#roi-out');
    if (!out) return;
    const calc = () => {
      const lph = Math.max(1, +host.querySelector('#roi-lph').value || 60);
      const rate = Math.max(1, +host.querySelector('#roi-rate').value || 75);
      const hours = d.source_added / lph;
      const manual = hours * rate;
      out.innerHTML =
        kpi('Manual equivalent', Math.round(hours).toLocaleString() + ' h', `${num(d.source_added)} source lines at ${lph}/h`) +
        kpi('Manual cost', usd(manual), `at $${rate}/h`) +
        kpi('Est. model cost', usd(d.cost_usd), 'list price, this range') +
        kpi('Ratio', d.cost_usd ? (manual / d.cost_usd).toFixed(1) + '×' : '—', 'manual ÷ model, list price');
    };
    host.querySelectorAll('#roi-lph, #roi-rate').forEach(i => i.addEventListener('input', calc));
    calc();
  }

  /* --------------------------------------------------------- Friction */
  function renderFriction() {
    const ins = lazy('insights', '/api/insights');
    if (!ins) return spinner('scanning for errors and waste');
    if (ins.error) return note('warn', `Could not load the analysis: ${esc(ins.error)}`);
    const f = ins.friction, o = DATA.claude.overview, c = DATA.claude;
    let html = `<div class="metrics">
      ${kpi('Error replies', int(f.errors.reduce((a, e) => a + e.n, 0)), 'API errors, limits, fallbacks')}
      ${kpi('Tool errors', int(f.tool_errors), pct(o.tool_error_rate) + ' of calls')}
      ${kpi('Denials', int(o.denials), 'permission refused')}
      ${kpi('Interrupts', int(f.interrupts), 'replies you stopped')}
      ${kpi('Compactions', int(f.compactions), `${int(f.sessions_with_compaction)} sessions hit the context limit`)}
      ${kpi('Model switches', int(f.model_switch_sessions), 'sessions that changed model mid-way')}
      ${kpi('Zero-tool sessions', int(f.zero_tool_sessions), `${usd(f.zero_tool_cost)} spent talking only`)}
      ${kpi('Median session', usd(f.median_session_cost), `cost per tool call median ${usd(f.cost_per_tool_median)}`)}
    </div>`;

    html += `<div class="grid cols-2">
      ${card('Errors and limits over time', 'Replies whose text signals a problem', chartSlot('fr-err'))}
      ${card('Interrupts, compactions, API errors', 'Session events per day', chartSlot('fr-ev'))}
    </div>`;
    draw('fr-err', el => {
      const dates = [...new Set(f.error_daily.map(r => r.date))].sort();
      const kinds = [...new Set(f.error_daily.map(r => r.error_kind))];
      const idx = new Map(dates.map((d, i) => [d, i]));
      lineChart(el, {
        labels: dates.map(shortDate),
        series: kinds.map(k => {
          const vals = new Array(dates.length).fill(0);
          for (const r of f.error_daily) if (r.error_kind === k) vals[idx.get(r.date)] += r.n;
          return { name: k, values: vals };
        }), fmt: int, stacked: true, height: 220,
      });
    });
    draw('fr-ev', el => lineChart(el, {
      labels: f.events_daily.map(r => shortDate(r.date)),
      series: [
        { name: 'interrupts', values: f.events_daily.map(r => r.interrupts), color: PALETTE[3] },
        { name: 'compactions', values: f.events_daily.map(r => r.compactions), color: PALETTE[1] },
        { name: 'api errors', values: f.events_daily.map(r => r.api_errors), color: PALETTE[5] },
      ], fmt: int, height: 220,
    }));

    html += `<div class="grid cols-2">
      ${card('Error kinds', 'Matched on the reply text', table(f.errors, [
        { k: 'error_kind', label: 'Kind', cls: 'mono' },
        { k: 'n', label: 'Replies', num: true, fmt: int, bar: true },
        { k: 'sessions', label: 'Sessions', num: true, fmt: int },
        { k: 'first_day', label: 'First' }, { k: 'last_day', label: 'Last' }]))}
      ${card('Sessions that hit a limit', 'Usage/rate limits, billing notices, model fallbacks', table(f.limit_sessions, [
        { k: 'date', label: 'Date' }, { k: 'title', label: 'Session', cls: 'name' },
        { k: 'project', label: 'Project' }, { k: 'error_kind', label: 'Kind', cls: 'mono' },
        { k: 'n', label: 'Replies', num: true, fmt: int }], { max: 20 }))}
    </div>`;

    html += card('What the same tokens would cost on another model',
      `A routing counterfactual: every token in the scope re-priced at each family's list rate.
       It says nothing about whether a cheaper model would have done the job — only what the ceiling
       on savings is. <b>nested</b> = sub-agent transcripts; <b>headless</b> = SDK / <span class="mono">claude -p</span> runs.`,
      table(f.routing, [
        { k: 'scope', label: 'Scope' },
        { k: 'actual', label: 'Actual (est.)', num: true, fmt: usd, bar: true },
        { k: 'at_haiku', label: 'At Haiku', num: true, fmt: usd },
        { k: 'at_sonnet', label: 'At Sonnet', num: true, fmt: usd },
        { k: 'at_opus', label: 'At Opus', num: true, fmt: usd },
      ]), 'full');

    html += `<div class="grid cols-2">
      ${card('High burn, few tools', 'Sessions over $1 with fewer than 10 tool calls — long conversations, or big pastes',
        table(f.high_burn_low_tool, sessionCols([
          { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd, bar: true },
          { k: 'tool_calls', label: 'Tools', num: true, fmt: int },
          { k: 'api_calls', label: 'API calls', num: true, fmt: int },
          { k: 'prompts', label: 'Prompts', num: true, fmt: int }])))}
      ${card('Expensive per tool call', `Sessions ≥ $2 costing more than 3× the median (${usd(f.cost_per_tool_median)}) per tool call`,
        table(f.cost_per_tool_outliers, sessionCols([
          { k: 'cost_per_tool', label: '$/tool', num: true, fmt: usd, bar: true },
          { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd },
          { k: 'tool_calls', label: 'Tools', num: true, fmt: int }])))}
    </div>`;

    html += `<div class="grid cols-2">
      ${card('Search-heavy Opus sessions', '≥10 tool calls, ≥60% read-only (Read/Grep/Glob), on Opus — candidates for a cheaper explorer',
        table(f.search_heavy_opus, sessionCols([
          { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd, bar: true },
          { k: 'tool_calls', label: 'Tools', num: true, fmt: int },
          { k: 'readonly_share', label: 'Read-only', num: true, fmt: (v) => pct(v * 100) }])))}
      ${card('Cache by project', 'Hit rate and the 1-hour vs 5-minute write split', table(f.cache_by_project, [
        { k: 'project', label: 'Project', cls: 'name' },
        { k: 'hit_pct', label: 'Hit', num: true, fmt: pct, bar: true },
        { k: 'cache_1h', label: '1h writes', num: true, fmt: num },
        { k: 'cache_5m', label: '5m writes', num: true, fmt: num },
        { k: 'cost_usd', label: 'Est. cost', num: true, fmt: usd }]))}
    </div>`;

    html += card('Tools that fail most', 'From the Tools tab, error rate over 5% with at least 20 calls',
      table(c.tools.filter(t => t.calls >= 20 && t.errors / t.calls > 0.05)
        .sort((a, b) => b.errors / b.calls - a.errors / a.calls), [
        { k: 'name', label: 'Tool', cls: 'mono name' },
        { k: 'calls', label: 'Calls', num: true, fmt: int },
        { k: 'errors', label: 'Errors', num: true, fmt: int, bar: true },
        { k: 'denied', label: 'Denied', num: true, fmt: int },
        { k: 'median_latency_ms', label: 'Median', num: true, fmt: (v) => v == null ? '—' : dur(v) },
      ], { max: 15 }), 'full');
    return html;
  }

  /* ----------------------------------------------------------- Skills */
  function renderSkills() {
    const m = lazy('skills', '/api/skills');
    if (!m) return spinner('mining repeated prompts');
    if (m.error) return note('warn', `Could not mine prompts: ${esc(m.error)}`);
    let html = note('info', `<b>Repeated work is the raw material for skills.</b> ${int(m.prompt_count)} typed prompts
      were normalised (numbers, paths, URLs and ticket keys masked) and grouped three ways: exact repeats,
      near-duplicates (Jaccard ≥ 0.45 on content words) and recurring phrases. A pattern is only worth a skill
      when the trigger has to be automatic — when you would not know, at that moment, that you needed it.
      ${window.__AIDASH_REPORT__ ? '' : `The <b>Lab</b> tab can hand this evidence to Claude to draft SKILL.md files.`}`);

    html += `<div class="metrics">
      ${kpi('Prompts mined', int(m.prompt_count), 'typed by you, ≥ 12 chars')}
      ${kpi('Exact repeats', int(m.families.length), 'same prompt, digits masked, ≥ 2 sessions')}
      ${kpi('Near-duplicate clusters', int(m.clusters.length), '≥ 3 prompts each')}
      ${kpi('Recurring phrases', int(m.phrases.length), 'in ≥ 4 sessions')}
      ${kpi('Slash commands used', int(m.slash.length), 'distinct')}
    </div>`;

    html += card('Skill candidates', 'Ranked by sessions × occurrences; clusters confined to one session rank low on purpose',
      table(m.candidates, [
        { k: 'kind', label: 'Kind', fmt: (v) => chip(v, v === 'repeat' ? 'ok' : '') },
        { k: 'score', label: 'Score', num: true, fmt: int, bar: true },
        { k: 'size', label: 'Prompts', num: true, fmt: int },
        { k: 'sessions', label: 'Sessions', num: true, fmt: int },
        { k: 'projects', label: 'Projects', fmt: (v) => esc((v || []).join(', ')) },
        { k: 'example', label: 'Example', cls: 'name', fmt: (v, r) =>
            `<span class="mono" title="${esc(v || (r.examples && r.examples[0].text) || '')}">${esc((v || (r.examples && r.examples[0].text) || '').slice(0, 110))}</span>` },
        { k: 'first_day', label: 'First' }, { k: 'last_day', label: 'Last' },
      ], { max: 30 }), 'full');

    html += card('Near-duplicate clusters', 'Each cluster: keywords, then the longest examples', m.clusters.length
      ? m.clusters.slice(0, 20).map((c, i) => `
        <details class="cluster"${i < 3 ? ' open' : ''}>
          <summary><b>${int(c.size)} prompts · ${int(c.sessions)} sessions</b>
            <span class="mono">${esc(c.keywords.join(' '))}</span>
            <span class="hint">${esc((c.projects || []).join(', '))} · ${esc(c.first_day || '')} → ${esc(c.last_day || '')}</span></summary>
          ${c.examples.map(e => `<blockquote><span class="hint">${esc(e.date || '')} · ${esc(e.project || '')}</span><br>${esc(e.text)}</blockquote>`).join('')}
        </details>`).join('')
      : '<div class="empty">No clusters of 3+ similar prompts in range</div>', 'full');

    html += `<div class="grid cols-2">
      ${card('Exact repeats', 'Digits masked — "run batch 3" and "run batch 7" are one family', table(m.families, [
        { k: 'example', label: 'Prompt', cls: 'mono name', title: true, fmt: (v) => `<span class="mono">${esc((v || '').slice(0, 100))}</span>` },
        { k: 'size', label: 'Times', num: true, fmt: int, bar: true },
        { k: 'sessions', label: 'Sessions', num: true, fmt: int },
        { k: 'projects', label: 'Projects', fmt: (v) => esc((v || []).join(', ')) }], { max: 40 }))}
      ${card('Recurring phrases', '2–4 word phrases across sessions', table(m.phrases, [
        { k: 'phrase', label: 'Phrase', cls: 'mono' },
        { k: 'sessions', label: 'Sessions', num: true, fmt: int, bar: true },
        { k: 'n', label: 'Times', num: true, fmt: int }], { max: 60 }))}
    </div>`;

    html += `<div class="grid cols-2">
      ${card('Slash commands already in use', 'Existing automation — a candidate that overlaps one of these is a fix, not a new skill',
        table(m.slash, [
          { k: 'name', label: 'Command', cls: 'mono' },
          { k: 'n', label: 'Uses', num: true, fmt: int, bar: true },
          { k: 'sessions', label: 'Sessions', num: true, fmt: int },
          { k: 'projects', label: 'Projects', num: true, fmt: int },
          { k: 'last_day', label: 'Last' }], { max: 40 }))}
      ${card('Repeated sub-agent instructions', 'The same Agent prompt, sent more than once — a skill or a custom agent would carry it',
        table(m.agent_prompts, [
          { k: 'head', label: 'Instruction', cls: 'mono name', title: true, fmt: (v) => `<span class="mono">${esc((v || '').slice(0, 110))}</span>` },
          { k: 'n', label: 'Times', num: true, fmt: int, bar: true },
          { k: 'sessions', label: 'Sessions', num: true, fmt: int }], { max: 20 }))}
    </div>`;
    return html;
  }

  /* -------------------------------------------------------------- Lab */
  let artifactsFetch = null;
  function loadArtifacts(force) {
    if (artifactsFetch) return artifactsFetch;
    if (DATA.artifacts && !force) return Promise.resolve(DATA.artifacts);
    artifactsFetch = fetch('/api/artifacts').then(r => r.json()).then(a => {
      DATA.artifacts = a; artifactsFetch = null;
      DATA.jobs = a.jobs || []; // the server's list is the only source of truth
      return a;
    })
      .catch(e => { artifactsFetch = null; DATA.artifacts = { error: e.message, proposals: [], findings: [], exports: [], llm: {} }; return DATA.artifacts; });
    return artifactsFetch;
  }

  function renderLab() {
    if (window.__AIDASH_REPORT__) return '';
    const a = DATA.artifacts;
    if (!a) {
      loadArtifacts().then(() => { if (activeTab === 'lab') showTab('lab'); });
      return `<div id="loading"><span class="pulse"></span> listing generated files…</div>`;
    }
    const llm = a.llm || {};
    const f = Object.fromEntries(currentFilters().entries());
    const scope = (f.project ? `project <b>${esc(f.project)}</b>` : 'all projects') +
      (f.since || f.until ? ` · ${esc(f.since || 'start')} → ${esc(f.until || 'today')}` : ' · all time');
    const modelSel = (id) => `<select id="${id}" class="model">${(llm.models || ['sonnet']).map(m =>
      `<option value="${esc(m)}"${m === llm.default_model ? ' selected' : ''}>${esc(m)}</option>`).join('')}</select>`;

    let html = note('info', `<b>Everything on this tab writes files under <span class="mono">data/</span>.</b>
      Exports are computed locally. The two model-written outputs run <span class="mono">claude -p</span> under
      your own Claude Code login with tools disabled and session persistence off, so those runs never show up
      in the numbers. Current scope: ${scope}.`);

    if (!llm.available) {
      html += note('warn', `The <span class="mono">claude</span> CLI was not found on PATH, so skill proposals and
        findings are disabled. Exports still work.`);
    }

    html += `<div class="grid cols-2">
      ${card('Data', 'Pull new transcripts, or rebuild the database from scratch', `
        <div class="actions">
          <button class="primary" data-job="refresh">Refresh (incremental)</button>
          <button class="secondary" data-job="refresh-full">Rebuild everything</button>
        </div>
        <p class="hint">Incremental reads only bytes appended since last time. Rebuild takes about a minute per GB of transcripts.</p>`)}
      ${card('Exports', 'Self-contained files for the current scope', `
        <div class="actions">
          <button class="primary" data-job="report">Export HTML report</button>
          <button class="secondary" data-job="corpus">Export prompt corpus (.txt)</button>
        </div>
        <p class="hint">The report is this dashboard with every tab rendered on one page — open it anywhere, print to PDF.
        The corpus is every prompt you typed, grouped by project and session: what to read when the numbers say
        something repeats and you want to see <i>what</i>.</p>`)}
    </div>`;

    html += `<div class="grid cols-2">
      ${card('Skill proposals', 'Claude reads the evidence pack and drafts SKILL.md files', `
        <div class="actions">
          <button class="primary" data-job="propose-skills" data-model="m-skills"${llm.available ? '' : ' disabled'}>Propose skills</button>
          <label class="field"><span>Model</span>${modelSel('m-skills')}</label>
          <a class="secondary btn" href="/api/evidence?${esc(currentFilters().toString())}" target="_blank" rel="noopener">View evidence pack</a>
        </div>
        <p class="hint">Proposals land in <span class="mono">data/skills-proposed/&lt;name&gt;/SKILL.md</span>; install
        copies one to <span class="mono">${esc(a.skills_dir || '~/.claude/skills')}</span>. Read it first — it's a draft
        written from patterns, not a tested skill. Budget cap $3 per run.</p>`)}
      ${card('Findings', 'A coach-style read of the numbers and the evidence', `
        <div class="actions">
          <button class="primary" data-job="findings" data-model="m-findings"${llm.available ? '' : ' disabled'}>Write findings</button>
          <label class="field"><span>Model</span>${modelSel('m-findings')}</label>
        </div>
        <p class="hint">Markdown under <span class="mono">data/findings/</span>: headline, what the numbers say,
        habits to keep, friction ranked with fixes, delivery, and five actions for next week.</p>`)}
    </div>`;

    html += card('Proposed skills', a.proposals.length ? '' : 'None yet', table(a.proposals, [
      { k: 'name', label: 'Skill', cls: 'mono', fmt: (v) => `<a href="#" data-view="proposal" data-name="${esc(v)}">${esc(v)}</a>` },
      { k: 'confidence', label: 'Confidence', fmt: (v) => v ? chip(v, v === 'high' ? 'ok' : v === 'medium' ? 'warn' : '') : '' },
      { k: 'description', label: 'Description', cls: 'name', title: true, fmt: (v) => esc((v || '').slice(0, 140)) },
      { k: 'generated', label: 'Generated' }, { k: 'model', label: 'Model', cls: 'mono' },
      { k: 'installed', label: '', fmt: (v, r) => v ? chip('installed', 'ok')
          : `<button class="secondary small" data-install="${esc(r.name)}">Install</button>` },
    ]), 'full');

    html += `<div class="grid cols-2">
      ${card('Findings', '', table(a.findings, [
        { k: 'file', label: 'File', cls: 'mono', fmt: (v) => `<a href="#" data-view="finding" data-name="${esc(v)}">${esc(v)}</a>` },
        { k: 'mtime', label: 'Written', fmt: when },
        { k: 'size', label: 'Size', num: true, fmt: (v) => num(v) + 'B' }]))}
      ${card('Exports', '', table(a.exports, [
        { k: 'file', label: 'File', cls: 'mono', fmt: (v) => `<a href="/exports/${encodeURIComponent(v)}" target="_blank" rel="noopener">${esc(v)}</a>` },
        { k: 'kind', label: 'Kind' },
        { k: 'mtime', label: 'Written', fmt: when },
        { k: 'bytes', label: 'Size', num: true, fmt: (v) => num(v) + 'B' },
        { k: 'file', label: '', fmt: (v) => `<a class="secondary btn small" href="/exports/${encodeURIComponent(v)}?download=1">Download</a>` }]))}
    </div>`;

    html += `<div class="panel full" id="viewer" hidden><h3 id="viewer-title"></h3>
      <p class="hint"><a href="#" id="viewer-close">close</a></p><div class="body md" id="viewer-body"></div></div>`;
    html += card('Recent jobs', '', `<div id="jobs-list">${jobsHtml()}</div>`, 'full');
    return html;
  }

  function jobsHtml() {
    const list = (DATA.jobs || []);
    if (!list.length) return '<div class="empty">Nothing has run yet this session</div>';
    return list.map(j => `<details class="job ${esc(j.status)}"${j.status === 'running' ? ' open' : ''}>
      <summary>${chip(j.status, j.status === 'done' ? 'ok' : j.status === 'failed' ? 'bad' : j.status === 'running' ? 'warn' : '')}
        <b>${esc(j.label)}</b> <span class="hint">${when(j.started_at)}${j.error ? ' · ' + esc(j.error) : ''}</span></summary>
      <pre class="log">${esc((j.log || []).join('\n'))}</pre>
      ${j.result && j.result.url ? `<p class="hint">→ <a href="${esc(j.result.url)}" target="_blank" rel="noopener">${esc(j.result.file)}</a></p>` : ''}
    </details>`).join('');
  }

  function wireLab(host) {
    host.querySelectorAll('button[data-job]').forEach(b => b.addEventListener('click', () => {
      const body = Object.fromEntries(currentFilters().entries());
      if (b.dataset.model) body.model = host.querySelector('#' + b.dataset.model).value;
      runJob(b.dataset.job, body, () => loadArtifacts(true).then(() => { if (activeTab === 'lab') showTab('lab'); }));
    }));
    host.querySelectorAll('button[data-install]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      const res = await fetch('/api/skills/install', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: b.dataset.install }) });
      const j = await res.json();
      if (!res.ok) { alert(j.error || 'install failed'); b.disabled = false; return; }
      await loadArtifacts(true); showTab('lab');
    }));
    host.querySelectorAll('a[data-view]').forEach(a => a.addEventListener('click', async (e) => {
      e.preventDefault();
      const url = a.dataset.view === 'proposal' ? '/api/proposals/' : '/api/findings/';
      const text = await fetch(url + encodeURIComponent(a.dataset.name)).then(r => r.text());
      host.querySelector('#viewer-title').textContent = a.dataset.name;
      host.querySelector('#viewer-body').innerHTML = markdown(text);
      host.querySelector('#viewer').hidden = false;
      host.querySelector('#viewer').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }));
    const close = host.querySelector('#viewer-close');
    if (close) close.addEventListener('click', (e) => { e.preventDefault(); host.querySelector('#viewer').hidden = true; });
  }

  /* ------------------------------------------------ minimal markdown */
  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }
  function markdown(src) {
    const lines = (src || '').replace(/\r/g, '').split('\n');
    const out = [];
    let i = 0, para = [], list = null;
    const flushP = () => { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; } };
    const flushL = () => { if (list) { out.push(`</${list}>`); list = null; } };
    if (lines[0] === '---') {
      const end = lines.indexOf('---', 1);
      if (end > 0) {
        out.push('<dl class="fm">' + lines.slice(1, end).map(l => {
          const m = l.match(/^(\w[\w-]*):\s*(.*)$/);
          if (!m) return '';
          let v = m[2];
          if (/^".*"$/.test(v)) { try { v = JSON.parse(v); } catch (_) { v = v.slice(1, -1); } }
          return `<dt>${esc(m[1])}</dt><dd>${inline(v)}</dd>`;
        }).join('') + '</dl>');
        i = end + 1;
      }
    }
    for (; i < lines.length; i++) {
      const l = lines[i];
      if (/^```/.test(l)) {
        flushP(); flushL();
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>');
        continue;
      }
      if (/^<!--.*-->\s*$/.test(l)) continue;
      const h = l.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flushP(); flushL(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(l)) { flushP(); flushL(); out.push('<hr>'); continue; }
      if (/^\|.*\|\s*$/.test(l)) {
        flushP(); flushL();
        const rowsT = [];
        while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) rowsT.push(lines[i++]);
        i--;
        const cells = (r) => r.trim().slice(1, -1).split('|').map(c => c.trim());
        const body = rowsT.filter(r => !/^\|[\s:|-]+\|$/.test(r));
        out.push('<div class="tw"><table><thead><tr>' + cells(body[0]).map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>' +
          body.slice(1).map(r => '<tr>' + cells(r).map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>');
        continue;
      }
      const li = l.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
      if (li) {
        flushP();
        const kind = /^\s*\d/.test(l) ? 'ol' : 'ul';
        if (list !== kind) { flushL(); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline(li[1])}</li>`);
        continue;
      }
      if (/^>\s?/.test(l)) { flushP(); flushL(); out.push(`<blockquote>${inline(l.replace(/^>\s?/, ''))}</blockquote>`); continue; }
      if (!l.trim()) { flushP(); flushL(); continue; }
      if (list && /^\s{2,}/.test(l)) { out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, ' ' + inline(l.trim()) + '</li>'); continue; }
      flushL();
      para.push(l.trim());
    }
    flushP(); flushL();
    return out.join('\n');
  }

  return {
    tabs: [
      ['habits', 'Habits', 'How you drive it'],
      ['delivery', 'Delivery', 'What shipped, and what it cost'],
      ['friction', 'Friction', 'Errors, limits, waste and routing'],
      ['skills', 'Skills', 'Repeated work that could be automated'],
      ['lab', 'Lab', 'Refresh data, export reports, ask Claude for proposals'],
    ],
    renderers: { habits: renderHabits, delivery: renderDelivery, friction: renderFriction, skills: renderSkills, lab: renderLab },
    afterMount(name, host) {
      if (name === 'delivery') wireRoi(host);
      if (name === 'lab') wireLab(host);
    },
    jobsHtml, markdown,
  };
})();
