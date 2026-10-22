import type { RunReport, RunSummary, TaskSummary } from '../types'
import { compactId, displayStatus, localTime, statusTone } from '../presentation'

type Props = {
  task: TaskSummary | null
  runs: RunSummary[]
  selectedRunId: string | null
  onSelectRun: (id: string) => void
  report: RunReport | null
  patch: string
  loading: boolean
  error: string | null
}

function Diff({ patch }: { patch: string }) {
  if (!patch) return <p className="empty">这次运行没有生成可展示的补丁。</p>
  return <div className="diff-code" role="region" aria-label="统一格式补丁">
    {patch.split('\n').map((line, index) => {
      const kind = line.startsWith('+++ ') || line.startsWith('--- ') ? 'file'
        : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'remove'
          : line.startsWith('@@') ? 'hunk' : 'context'
      return <div className={`diff-line ${kind}`} key={index}><span className="line-number">{String(index + 1).padStart(2, '0')}</span><span>{line || ' '}</span></div>
    })}
  </div>
}

function reproductionStatus(status?: string): string {
  if (status === 'failed') return 'Bug 已复现'
  if (status === 'passed') return '未复现（测试通过）'
  if (status === 'timeout') return '复现超时'
  if (status === 'error') return '复现异常'
  return status ? displayStatus(status) : '等待记录'
}

export default function RunInspector({ task, runs, selectedRunId, onSelectRun, report, patch, loading, error }: Props) {
  const selectedRun = runs.find(run => run.run_id === selectedRunId)
  const validation = report?.validation
  const hypothesis = report?.diagnosis?.hypothesis
  const evidence = report?.evidence ?? []
  const targetLocation = evidence.find(item =>
    item.kind === 'traceback_location' && item.file_path === hypothesis?.target_file &&
    item.symbol_name === hypothesis?.target_symbol && item.line_number != null
  )
  const targetFileLine = targetLocation?.file_path && targetLocation.line_number != null
    ? `${targetLocation.file_path}:${targetLocation.line_number}` : null
  const targetLocationLabel = targetLocation?.file_path && targetLocation.line_number != null
    ? `${targetLocation.file_path.split(/[\\/]/).at(-1)}:${targetLocation.line_number}` : null
  const stages: { number: string; label: string; detail: string; location?: string; locationLabel?: string }[] = [
    { number: '01', label: '修复前测试', detail: reproductionStatus(report?.initial_public_test?.status) },
    { number: '02', label: '定位', detail: hypothesis?.target_symbol ?? '无定位假设', location: targetFileLine ?? undefined, locationLabel: targetLocationLabel ?? undefined },
    { number: '03', label: '修复', detail: report?.repair?.candidate ? '已生成候选补丁' : '未生成补丁' },
    { number: '04', label: '验证', detail: displayStatus(validation?.status) },
  ]

  async function copyCommand() {
    if (!task) return
    const command = `.\\.venv\\Scripts\\python.exe -m repofix.cli repair --task "${task.config}" --provider api --runner docker`
    try { await navigator.clipboard.writeText(command) } catch { window.prompt('复制以下命令：', command) }
  }

  return (
    <section className="panel inspector" id="review" aria-labelledby="inspector-title">
      <div className="panel-heading inspector-heading">
        <div><span className="eyebrow">RUN INSPECTOR</span><h2 id="inspector-title">修复过程与证据</h2></div>
        <span className="read-only-tag">证据存档</span>
      </div>
      {!task ? <p className="empty">选择一条任务，查看相关运行记录。</p> : <>
        <div className="selected-issue">
          <div className="selected-issue-main">
            <div className="issue-label">当前任务 <span className={`status-dot ${statusTone(task.status)}`} /></div>
            <h3>{task.id}</h3>
            <div className="issue-description-label">待修复问题</div>
            <p>{task.issue ?? report?.task.issue ?? `类别：${task.category} · 来源：${task.origin}`}</p>
          </div>
          <div className="issue-actions">
            <span className={`status-pill ${statusTone(validation?.status ?? selectedRun?.validation_status)}`}>
              {displayStatus(validation?.status ?? selectedRun?.validation_status)}
            </span>
            <button type="button" className="button subtle" onClick={copyCommand}>复制运行命令 ↗</button>
          </div>
        </div>
        <div className="run-toolbar">
          <label htmlFor="run-select">运行记录</label>
          <select id="run-select" value={selectedRunId ?? ''} disabled={runs.length === 0}
            onChange={event => onSelectRun(event.target.value)}>
            {runs.length === 0 && <option value="">暂无记录</option>}
            {runs.map(run => <option value={run.run_id} key={run.run_id}>
              {compactId(run.run_id)} · {displayStatus(run.validation_status)} · {localTime(run.created_at_utc)}
            </option>)}
          </select>
          {selectedRun?.has_patch && <a className="text-link" href={`/api/runs/${encodeURIComponent(selectedRun.run_id)}/patch`} download={`${task.id}.patch`}>下载补丁 ↓</a>}
          {report?.candidate_selection && selectedRun && <a className="text-link" href={`/api/runs/${encodeURIComponent(selectedRun.run_id)}/report.md`} download={`${task.id}-report.md`}>证据报告 ↓</a>}
        </div>
        {error && <div className="inline-error" role="alert">{error}</div>}
        {loading && <p className="empty">正在读取运行报告…</p>}
        {!loading && !error && !report && <p className="empty">此任务尚无 Agent 运行记录。可在上方点击“运行 Agent 修复”。</p>}
        {!loading && report && <>
          <div className="stages" aria-label="运行阶段">
            {stages.map(stage => <div className="stage" key={stage.number}>
              <span className="stage-number">{stage.number}</span><strong>{stage.label}</strong><small title={stage.detail}>{stage.detail}</small>
              {stage.location && <code className="stage-location" title={stage.location} aria-label={`触发位置 ${stage.location}`}>{stage.locationLabel}</code>}
            </div>)}
          </div>
          {report.candidate_selection && report.candidate_selection.attempts.length > 1 &&
            <div className="candidate-selection" aria-label="候选补丁比较">
              <div className="subheading"><div><span className="eyebrow">CANDIDATE COMPARISON</span><h4>候选补丁比较</h4></div><span className="count-pill">{report.candidate_selection.attempts.length} 份</span></div>
              <p>仅依据公开测试、质量审查和改动规模选择；隐藏测试未参与。</p>
              <div className="candidate-comparison-list">{report.candidate_selection.attempts.map(attempt =>
                <div className="candidate-comparison-item" key={attempt.number}>
                  <strong>候选 {attempt.number}{attempt.number === report.candidate_selection?.selected_attempt ? ' · 已选中' : ''}</strong>
                  <span>{attempt.strategy} · {displayStatus(attempt.validation_status ?? attempt.repair_status)}{attempt.review ? ` · ${attempt.review.changed_lines} 行变动` : ''}</span>
                  {attempt.candidate_explanation && <small>{attempt.candidate_explanation}</small>}
                  {attempt.patch && selectedRun && <a href={`/api/runs/${encodeURIComponent(selectedRun.run_id)}/attempts/${encodeURIComponent(attempt.patch)}`} download={attempt.patch}>下载这份补丁 ↓</a>}
                </div>
              )}</div>
            </div>}
          <div className="inspector-grid">
            <div className="evidence-panel">
              <div className="subheading"><div><span className="eyebrow">GROUNDED EVIDENCE</span><h4>定位依据</h4></div><span className="count-pill">{evidence.length}</span></div>
              {hypothesis && <div className="hypothesis-box"><span>目标位置</span><strong>{targetFileLine ?? hypothesis.target_file}</strong><code>{hypothesis.target_symbol}</code></div>}
              <div className="evidence-list">
                {evidence.length === 0 && <p className="empty compact">报告未记录结构化证据。</p>}
                {evidence.map(item => <article className="evidence-card" key={item.id}>
                  <div><b>{item.id}</b><span>{item.kind}</span></div>
                  {item.file_path && <small>{item.file_path}{item.line_number ? `:${item.line_number}` : ''}</small>}
                  <pre>{item.content}</pre>
                </article>)}
              </div>
            </div>
            <div className="patch-panel">
              <div className="subheading"><div><span className="eyebrow">CANDIDATE PATCH</span><h4>代码差异</h4></div><span className="diff-label">UNIFIED DIFF</span></div>
              {report.repair?.candidate?.explanation && <p className="patch-rationale">{report.repair.candidate.explanation}</p>}
              <Diff patch={patch} />
            </div>
          </div>
          {validation?.patch_review && <div className={`patch-review ${validation.patch_review.status}`} aria-label="补丁质量审查">
            <div className="patch-review-heading"><div><span className="eyebrow">PATCH POLICY REVIEW</span><strong>补丁质量审查</strong></div>
              <span>{validation.patch_review.status === 'passed' ? '规则通过' : validation.patch_review.status === 'warnings' ? '需人工复核' : '已拒绝'}</span></div>
            <p>变动约 {validation.patch_review.changed_lines} 行 · {validation.patch_review.findings.length === 0 ? '未发现已定义的投机模式' : `发现 ${validation.patch_review.findings.length} 项审查提示`}</p>
            {validation.patch_review.findings.length > 0 && <ul>{validation.patch_review.findings.map((finding, index) =>
              <li key={`${finding.code}-${index}`}><b>{finding.severity === 'block' ? '阻断' : '提醒'}</b><code>{finding.code}</code><span>{finding.message}{finding.line_number != null ? `（第 ${finding.line_number} 行）` : ''}</span></li>
            )}</ul>}
          </div>}
          <div className="validation-note"><span className={`status-dot ${statusTone(validation?.status)}`} />
            <span>{validation?.reason ?? '暂无公开验证结果。'}</span>
            <small>隐藏验收结果只在实验评测中展示。</small>
          </div>
        </>}
      </>}
    </section>
  )
}
