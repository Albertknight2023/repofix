import { useState } from 'react'
import type { ExperimentOptions, GradeResult, Job, PatchReview, RunOptions, RunSummary, TaskSummary } from '../types'

const defaultOptions: RunOptions = { workflow: 'two-agent', engine: 'legacy', context: 'ast-callgraph', max_tokens: 6000 }

function OptionFields({ options, onChange }: { options: RunOptions; onChange: (value: RunOptions) => void }) {
  return <div className="action-fields engine-options">
    <label>协作方式<select value={options.workflow} onChange={event => onChange({ ...options, workflow: event.target.value as RunOptions['workflow'] })}>
      <option value="two-agent">双 Agent · 单候选基线</option>
      <option value="single-agent">单 Agent · 直接修复基线</option>
      <option value="competitive">候选竞争 · 争取两份不同补丁</option>
      <option value="dynamic">反馈驱动 · 必要时生成第二份</option>
    </select></label>
    <label>执行引擎<select value={options.engine} onChange={event => onChange({ ...options, engine: event.target.value as RunOptions['engine'] })}>
      <option value="legacy">原有调度 · 历史实验基线</option>
      <option value="langgraph">LangGraph · 状态图调度</option>
    </select><small>只改变编排方式，不更换模型或测试规则</small></label>
    <label>代码上下文<select value={options.context} onChange={event => onChange({ ...options, context: event.target.value as RunOptions['context'] })}>
      <option value="ast-callgraph">AST 与调用图</option><option value="traceback-only">仅错误堆栈</option>
    </select></label>
    <label>每次模型回复上限<input type="number" min={512} max={12000} step={1} value={options.max_tokens}
      onChange={event => onChange({ ...options, max_tokens: Number(event.target.value) })} /><small>Token · 不是整个实验的费用上限</small></label>
    <p className="mode-explanation">{options.workflow === 'competitive' ? '最多 5 次模型调用（定位 1 次、修复最多 4 次），争取两份不同且可评测的补丁；各候选独立测试，择优保存。' : options.workflow === 'dynamic' ? '第一份补丁未通过公开测试、被拒绝或有审查提醒时，才请求第二份。' : '基线模式只生成一份候选补丁。'}</p>
  </div>
}

function JobNotice({ job }: { job: Job | undefined }) {
  if (!job) return null
  const state = { queued: '排队中', running: '正在运行', succeeded: '已完成', failed: '运行失败' }[job.state]
  const patchReview = job.result?.patch_review as PatchReview | null | undefined
  return <div className={`job-notice ${job.state}`} role="status">
    <span className="job-signal" aria-hidden="true" /><div><strong>{state}</strong><small>{job.kind === 'repair' ? 'Agent 修复' : job.kind === 'grade' ? '独立验收' : '批量实验'} · {job.id}</small>
      {job.error && <p>{job.error}</p>}
      {job.kind === 'repair' && job.state === 'succeeded' && <p>公开测试：{job.result?.public_tests_passed ? '通过' : '未通过'}；{job.result?.patch_path ? '候选补丁已保存' : '没有生成可验收补丁'}。</p>}
      {job.kind === 'repair' && job.state === 'succeeded' && patchReview && <p>质量审查：{patchReview.status === 'passed' ? '规则通过' : patchReview.status === 'warnings' ? '需人工复核' : '已拒绝'}。</p>}
      {job.kind === 'repair' && job.state === 'succeeded' && typeof job.result?.candidate_count === 'number' && <p>候选 {job.result.candidate_count} 份；选中第 {String(job.result.selected_attempt)} 份，模型调用 {String(job.result.model_calls)} 次。</p>}
      {job.kind === 'grade' && job.state === 'succeeded' && <p>验收结果：{job.result?.status === 'passed' ? '公开与隐藏测试通过' : String(job.result?.status ?? '未通过')}。</p>}
      {job.kind === 'experiment' && job.state === 'succeeded' && <p>{job.result?.formal_status === 'incomplete' ? '实验未完成；请查看运行错误与调用记录。' : '实验已结束，评测结果可在下方查看。'}</p>}
    </div>
  </div>
}

export function RepairLauncher({ task, run, grade, jobs, onRepair, onGrade }: {
  task: TaskSummary | null
  run: RunSummary | undefined
  grade: GradeResult | null
  jobs: Job[]
  onRepair: (options: RunOptions) => Promise<void>
  onGrade: () => Promise<void>
}) {
  const [options, setOptions] = useState<RunOptions>(defaultOptions)
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const related = jobs.filter(job => job.details.task_id === task?.id && job.kind !== 'experiment')
  const active = related.some(job => job.state === 'queued' || job.state === 'running')

  async function submit(operation: () => Promise<void>) {
    setSending(true); setError(null)
    try { await operation() } catch (failure) { setError(failure instanceof Error ? failure.message : '提交失败') }
    finally { setSending(false) }
  }

  return <section className="panel action-panel" aria-labelledby="repair-launch-title">
    <div className="panel-heading"><div><span className="eyebrow">RUN CONTROL</span><h2 id="repair-launch-title">运行修复</h2></div><span className="count-pill">单任务</span></div>
    <div className="action-body">
      <p className="action-help">对当前任务运行 Agent；只使用公开测试。生成补丁后，再单独验收隐藏测试。</p>
      <OptionFields options={options} onChange={setOptions} />
      <div className="action-footer"><button className="button primary-action" type="button" disabled={!task || task.status !== 'ready' || active || sending || options.max_tokens < 512 || options.max_tokens > 12000} onClick={() => void submit(() => onRepair(options))}>运行 Agent 修复 ↗</button>
        <button className="button" type="button" disabled={!task || !run?.has_patch || active || sending} onClick={() => void submit(onGrade)}>验收当前补丁</button></div>
      {task?.status !== 'ready' && <p className="action-footnote">仅 ready 任务可运行；未就绪任务需先完成复现与参考补丁检查。</p>}
      {grade && <p className={`grade-result ${grade.status === 'passed' ? 'passed' : ''}`}>当前补丁验收：{grade.status === 'passed' ? '通过' : grade.status === 'rejected_patch' ? '质量审查拒绝' : grade.status} · 公开 {grade.public_test_status ?? '未运行'} / 隐藏 {grade.hidden_test_status ?? '未运行'}{grade.patch_review?.status === 'warnings' ? ' · 存在审查提醒' : ''}</p>}
      {grade?.status === 'rejected_patch' && <p className="form-error">{grade.reason}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <JobNotice job={related[0]} />
    </div>
  </section>
}

export function ExperimentLauncher({ tasks, jobs, onStart }: {
  tasks: TaskSummary[]
  jobs: Job[]
  onStart: (suite: string, method: string, limit: number | null, options: ExperimentOptions) => Promise<void>
}) {
  const suites = Array.from(new Map(tasks.map(task => [task.suite, task.origin === 'BugsInPy' ? '真实案例 · BugsInPy' : '开发任务'])).entries())
  const [suite, setSuite] = useState(suites.find(([id]) => tasks.some(task => task.suite === id && task.origin === 'BugsInPy'))?.[0] ?? suites[0]?.[0] ?? '')
  const [method, setMethod] = useState(`trial-${new Date().toISOString().slice(0, 10).replaceAll('-', '')}`)
  const [limit, setLimit] = useState('1')
  const [options, setOptions] = useState<ExperimentOptions>({ ...defaultOptions, formal: true, max_submission_attempts: 3, max_model_calls: 21 })
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const selectedSuite = suites.some(([id]) => id === suite) ? suite : suites[0]?.[0] ?? ''
  const readyCount = tasks.filter(task => task.suite === selectedSuite && task.status === 'ready').length
  const active = jobs.some(job => job.state === 'queued' || job.state === 'running')
  const parsedLimit = limit.trim() === '' ? null : Number(limit)
  const valid = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(method) &&
    (parsedLimit === null || (Number.isInteger(parsedLimit) && parsedLimit >= 1 && parsedLimit <= readyCount)) &&
    options.max_tokens >= 512 && options.max_tokens <= 12000 &&
    Number.isInteger(options.max_submission_attempts) && options.max_submission_attempts >= 1 && options.max_submission_attempts <= 10 &&
    Number.isInteger(options.max_model_calls) && options.max_model_calls >= 1 && options.max_model_calls <= 200

  async function submit() {
    setSending(true); setError(null)
    try { await onStart(selectedSuite, method, parsedLimit, options) }
    catch (failure) { setError(failure instanceof Error ? failure.message : '提交失败') }
    finally { setSending(false) }
  }

  return <section className="panel action-panel experiment-launcher" aria-labelledby="experiment-launch-title">
    <div className="panel-heading"><div><span className="eyebrow">EXPERIMENT SETUP</span><h2 id="experiment-launch-title">新建批量实验</h2></div><span className="count-pill">{readyCount} 条 ready</span></div>
    <div className="action-body">
      <p className="action-help">按清单顺序运行 Agent，再由独立评测器验收补丁。模型调用可能产生费用。</p>
      <div className="action-fields">
        <label>任务集<select value={selectedSuite} onChange={event => setSuite(event.target.value)}>{suites.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
        <label>方法标签<input value={method} maxLength={64} onChange={event => setMethod(event.target.value)} placeholder="例如 two-agent-ast-v1" /><small>用于标识实验，不决定 Agent 数量</small></label>
        <label>运行前 N 条 ready 任务<input type="number" min={1} max={readyCount} value={limit} onChange={event => setLimit(event.target.value)} placeholder="留空表示全部" /><small>留空＝全部；部分试跑仅展示已运行任务的通过率，不算正式全量成绩</small></label>
      </div>
      <OptionFields options={options} onChange={next => setOptions(current => ({ ...current, ...next }))} />
      <div className="protocol-panel" role="group" aria-label="实验协议与费用上限">
        <div className="protocol-heading"><span className="eyebrow">EXPERIMENT PROTOCOL</span><strong>提交门槛与预算</strong></div>
        <div className="action-fields">
          <label>实验协议<select value={options.formal ? 'formal' : 'legacy'} onChange={event => setOptions(current => ({ ...current, formal: event.target.value === 'formal' }))}>
            <option value="formal">正式实验 · 预检与有限重试</option><option value="legacy">旧版试跑 · 单轮生成</option>
          </select></label>
          <label>每题最多尝试次数<input type="number" min={1} max={10} step={1} disabled={!options.formal} value={options.max_submission_attempts}
            onChange={event => setOptions(current => ({ ...current, max_submission_attempts: Number(event.target.value) }))} /><small>仅正式实验生效；无效补丁会反馈重试</small></label>
          <label>整轮模型调用硬上限<input type="number" min={1} max={200} step={1} value={options.max_model_calls}
            onChange={event => setOptions(current => ({ ...current, max_model_calls: Number(event.target.value) }))} /><small>调用 API 可能产生费用；达到上限即停止</small></label>
        </div>
        <p className="protocol-note">{options.formal ? '启动前检查复现与参考补丁；公开或隐藏测试失败仍如实计为补丁失败。全题完成才是正式全量实验。' : '旧版协议用于复现历史试跑；不会自动重试无效候选。'}</p>
      </div>
      <div className="action-footer"><button className="button primary-action" type="button" disabled={!readyCount || !valid || active || sending} onClick={() => void submit()}>开始实验 ↗</button><span>最多同时排队 3 项，本地串行执行</span></div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <JobNotice job={jobs.find(job => job.kind === 'experiment')} />
    </div>
  </section>
}
