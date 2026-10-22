import { useEffect, useMemo, useState } from 'react'
import type { RunSummary, TaskSummary } from '../types'
import { displayStatus } from '../presentation'

type Props = {
  tasks: TaskSummary[]
  runs: RunSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
}

type Filter = 'all' | 'real' | 'development'

export default function TaskBrowser({ tasks, runs, selectedId, onSelect }: Props) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    return tasks.filter(task => {
      const kind = task.origin === 'BugsInPy' ? 'real' : 'development'
      return (filter === 'all' || kind === filter) &&
        (!needle || `${task.id} ${task.category} ${task.origin}`.toLocaleLowerCase().includes(needle))
    })
  }, [tasks, query, filter])

  useEffect(() => {
    if (filtered.length > 0 && !filtered.some(task => task.id === selectedId)) {
      onSelect(filtered[0].id)
    }
  }, [filtered, selectedId, onSelect])

  return (
    <section className="panel task-browser" id="tasks" aria-labelledby="task-browser-title">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">BENCHMARK LIBRARY</span>
          <h2 id="task-browser-title">bug任务库</h2>
        </div>
        <span className="count-pill">{filtered.length} 条</span>
      </div>
      <div className="task-controls">
        <label className="search-box">
          <span className="search-glyph" aria-hidden="true">⌕</span>
          <span className="sr-only">搜索任务</span>
          <input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索任务 ID 或类型" />
        </label>
        <div className="filter-row" aria-label="任务类型筛选">
          {([['all', '全部任务'], ['real', '真实案例'], ['development', '开发任务']] as const).map(([value, label]) => (
            <button key={value} className={filter === value ? 'filter active' : 'filter'}
              type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>
          ))}
        </div>
      </div>
      <div className="task-list">
        {filtered.length === 0 && <p className="empty compact">没有符合条件的任务。</p>}
        {filtered.map(task => {
          const runCount = runs.filter(run => run.task_id === task.id).length
          const real = task.origin === 'BugsInPy'
          return (
            <button key={task.id} type="button" data-id={task.id}
              className={`task-card ${selectedId === task.id ? 'selected' : ''}`}
              aria-pressed={selectedId === task.id} onClick={() => onSelect(task.id)}>
              <span className="task-card-top">
                <span className={`task-kind ${real ? 'real' : ''}`}>{real ? 'BugsInPy' : 'Development'}</span>
                <span className="task-run-count">{runCount} 次运行</span>
              </span>
              <strong>{task.id}</strong>
              <span className="task-card-bottom"><span>{task.category}</span><span title={task.status === 'candidate' ? '候选任务，尚未纳入正式评测' : undefined}>{displayStatus(task.status)}</span></span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
