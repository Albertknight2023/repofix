import type { EvaluationDetail, EvaluationSummary } from '../types'
import { compactId, displayStatus, localTime, statusTone } from '../presentation'

type Props = {
  evaluations: EvaluationSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
  detail: EvaluationDetail | null
  loading: boolean
  error: string | null
}

function evaluationOptionLabel(item: EvaluationSummary): string {
  const fourWorkflows = /^four-workflows-(\d{4})(\d{2})(\d{2})(?:-(.+))?$/.exec(item.id)
  if (fourWorkflows) {
    const [, year, month, day, variant] = fourWorkflows
    return `四工作流对照 · ${year}-${month}-${day}${variant ? ` · ${variant}` : ''}`
  }
  return item.created_at_utc
    ? `${compactId(item.id)} · ${localTime(item.created_at_utc)}`
    : compactId(item.id)
}

export default function EvaluationPanel({ evaluations, selectedId, onSelect, detail, loading, error }: Props) {
  const selected = evaluations.find(item => item.id === selectedId)
  const fixture = selected?.is_reference_fixture ?? false
  const partial = selected?.partial_run ?? false
  const formalStatus = detail?.formal_status ?? selected?.formal_status
  const formal = (detail?.experiment_protocol ?? selected?.experiment_protocol) === 'formal-v1'
  const metrics = detail?.submission_metrics
  const selectedTaskCount = detail?.selected_task_count ?? selected?.selected_task_count
  const selectedTaskIds = new Set([
    ...(detail?.preflight ?? []).map(item => item.task_id),
    ...(detail?.usage_records ?? []).map(item => item.task_id),
  ])
  const hasExactTaskScope = partial && selectedTaskCount !== null && selectedTaskCount !== undefined
    && selectedTaskCount > 0 && selectedTaskIds.size === selectedTaskCount
  const visibleRecords = hasExactTaskScope
    ? detail?.records?.filter(record => selectedTaskIds.has(record.task_id))
    : detail?.records
  const percent = (value: number | null) => value === null ? '—' : `${(value * 100).toFixed(0)}%`

  return <section className="panel evaluation-panel" id="experiments" aria-labelledby="evaluation-title">
    <div className="panel-heading">
      <div><span className="eyebrow">EXPERIMENT RESULTS</span><h2 id="evaluation-title">评测结果明细</h2></div>
      <span className="count-pill">{evaluations.length} 份记录</span>
    </div>
    {evaluations.length === 0 ? <p className="empty">还没有实验评测记录。</p> : <>
      <div className="evaluation-toolbar">
        <label htmlFor="evaluation-select">选择实验</label>
        <select id="evaluation-select" value={selectedId ?? ''} onChange={event => onSelect(event.target.value)}>
          {evaluations.map(item => <option value={item.id} key={item.id}>{evaluationOptionLabel(item)}</option>)}
        </select>
        <span className={`eval-class ${fixture ? 'fixture' : formalStatus === 'incomplete' || partial ? 'partial' : 'model'}`}>
          {fixture ? '本地模拟 / 自检' : formalStatus === 'incomplete' ? '正式实验未完成' : partial ? '部分任务试点' : formal ? '正式实验' : '旧版模型实验'}
        </span>
      </div>
      <div className={`evaluation-warning ${fixture ? 'fixture' : ''}`}>
        <span className="warning-symbol">i</span>
        <p>{fixture ? '这是模拟回复或参考补丁自检，用于检查评测链路，不代表真实模型的修复能力。'
          : formalStatus === 'incomplete' ? '这轮正式实验因环境、服务或评测异常未完成，不能纳入正式对照；已保存的尝试和用量仍可检查。'
          : partial ? `本次只运行 ${selectedTaskCount ?? '部分'} / ${selected?.task_count ?? '全部'} 条任务；下方通过率按本次运行的任务计算，不能当作全量正式成绩。`
            : '这是一次模型实验记录；请同时查看失败、未提交和环境错误，不能只看通过率。'}
          {selected?.task_count !== undefined && <span> 数据集规模：{selected.task_count} 条。</span>}</p>
      </div>
      {error && <div className="inline-error" role="alert">{error}</div>}
      {loading && <p className="empty">正在读取实验记录…</p>}
      {!loading && selected && <>
        <div className="method-list">
          {(selected.methods ?? []).map(method => {
            const denominator = partial && selectedTaskCount !== null && selectedTaskCount !== undefined
              ? selectedTaskCount : method.total ?? method.tasks ?? selected.task_count ?? 0
            const rate = denominator > 0 ? method.passed / denominator : 0
            return <div className="method-row" key={method.method}>
            <div><strong>{method.method}</strong><small>{method.passed} / {denominator} {partial ? '已运行任务通过' : '通过'}</small></div>
            <div className="progress-track" role="progressbar" aria-valuenow={method.passed} aria-valuemax={denominator} aria-valuemin={0} aria-label={`${method.method} ${partial ? '本次试跑' : '全量实验'}通过数`}>
              <span style={{ width: `${Math.max(0, Math.min(100, rate * 100))}%` }} />
            </div>
            <b>{(rate * 100).toFixed(0)}%</b>
          </div>})}
        </div>
        {formal && metrics && <div className="formal-metrics" aria-label={partial ? '部分任务试跑三项指标' : '正式实验三项指标'}>
          <div className="formal-metric"><span>有效补丁提交率</span><strong>{percent(metrics.valid_submission_rate)}</strong><small>{metrics.valid_patches} / {metrics.selected_tasks} 题</small></div>
          <div className="formal-metric"><span>有效补丁验收通过率</span><strong>{percent(metrics.conditional_acceptance_rate)}</strong><small>{metrics.passed_patches} / {metrics.valid_patches} 份补丁</small></div>
          <div className="formal-metric"><span>端到端成功率</span><strong>{percent(metrics.end_to_end_success_rate)}</strong><small>{metrics.passed_patches} / {metrics.selected_tasks} 题</small></div>
        </div>}
        {visibleRecords && <div className="result-table-wrap">
          <table className="result-table">
            <caption>{hasExactTaskScope ? `本次实际运行的 ${selectedTaskCount} 条任务` : partial ? '完整清单（包含未运行任务占位）' : '任务验收结果'}</caption>
            <thead><tr><th scope="col">任务</th><th scope="col">方法</th><th scope="col">最终状态</th><th scope="col">公开 / 留出</th></tr></thead>
            <tbody>{visibleRecords.map((record, index) => <tr key={`${record.method}-${record.task_id}-${index}`}>
              <td><code>{record.task_id}</code></td><td>{record.method}</td>
              <td><span className={`table-status ${statusTone(record.status)}`}><span className="status-dot" />{displayStatus(record.status)}</span>{record.no_submission_reason && <small className="record-reason">{record.no_submission_reason}</small>}</td>
              <td>{displayStatus(record.public_test_status)} / {displayStatus(record.holdout_test_status)}</td>
            </tr>)}</tbody>
          </table>
        </div>}
        {detail?.task_errors && detail.task_errors.length > 0 && <details className="experiment-errors">
          <summary>查看 {detail.task_errors.length} 条运行错误</summary>
          <ul>{detail.task_errors.map(item => <li key={item.task_id}><code>{item.task_id}</code>：{item.error}</li>)}</ul>
        </details>}
      </>}
    </>}
  </section>
}
