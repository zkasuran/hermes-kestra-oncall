// Kestra on-call: read-only Hermes Desktop pane.
// Reads /api/plugins/kestra-oncall/overview through ctx.rest (the same backend as the dashboard tab,
// dashboard/plugin_api.py). It never writes to Kestra: approvals happen in the Hermes chat.
// Not exercised in a real Desktop build by the author. Validated with `hermes plugins validate` only.
import { useEffect, useState, createElement as h } from 'react'

const REFRESH_MS = 10000

function Row({ label, value }) {
  return h('div', { className: 'flex gap-2 text-xs' },
    h('span', { className: 'text-(--ui-text-tertiary)' }, label),
    h('span', { className: 'font-mono break-all' }, value))
}

function Incident({ inc }) {
  const p = inc.proposal
  return h('div', { className: 'flex flex-col gap-1 rounded border border-(--ui-border) p-2' },
    h('div', { className: 'text-sm font-medium break-all' }, `${inc.namespace}.${inc.flow_id}`),
    h('div', { className: 'text-xs' }, inc.status + (inc.protected ? ' (protected, diagnose only)' : '')),
    h(Row, { label: 'incident', value: inc.incident_id }),
    h(Row, { label: 'failed task', value: inc.failed_task || 'unknown' }),
    p ? h('pre', { className: 'overflow-x-auto text-[0.7rem]' }, p.diff) : null,
    inc.apply_command ? h(Row, { label: 'approve in chat', value: inc.apply_command }) : null)
}

function Pause({ pause }) {
  return h('div', { className: 'flex flex-col gap-1 rounded border border-(--ui-border) p-2' },
    h('div', { className: 'text-sm font-medium break-all' }, `${pause.namespace}.${pause.flow_id}`),
    pause.description ? h('div', { className: 'text-xs' }, pause.description) : null,
    h(Row, { label: 'resume', value: pause.resume_command }),
    h(Row, { label: 'or', value: pause.kill_command }))
}

function Pane({ rest }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    const load = () =>
      rest('/overview')
        .then(d => { if (alive) { setData(d); setError('') } })
        .catch(e => { if (alive) setError(String((e && e.message) || e)) })
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => { alive = false; clearInterval(timer) }
  }, [rest])

  const errors = data ? Object.values(data.errors || {}) : []
  return h('div', { className: 'flex h-full flex-col gap-2 overflow-y-auto p-3 text-sm' },
    h('div', { className: 'font-medium' }, 'Kestra on-call'),
    data ? h('div', { className: 'text-xs text-(--ui-text-tertiary)' },
      data.connected ? `Kestra ${data.version} connected, read-only` : 'Kestra unreachable') : null,
    error ? h('div', { className: 'text-xs' }, 'Could not load: ' + error) : null,
    errors.map((e, i) => h('div', { key: i, className: 'text-xs' }, e)),
    h('div', { className: 'font-medium' }, `Incidents (${data ? data.incidents.length : 0})`),
    data ? data.incidents.map(inc => h(Incident, { key: inc.incident_id, inc })) : null,
    h('div', { className: 'font-medium' }, `Pending pauses (${data ? data.pauses.length : 0})`),
    data ? data.pauses.map(p => h(Pause, { key: p.execution_id + p.paused_task_run_id, pause: p })) : null)
}

export default {
  id: 'kestra-oncall', // must match the folder name
  name: 'Kestra on-call',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'kestra on-call',
      data: { placement: 'right', width: '340px' },
      render: () => h(Pane, { rest: ctx.rest })
    })
  }
}
