import { useEffect, useMemo, useRef, useState } from 'react'
import { loadEvaluation, loadGrade, loadJobs, loadOverview, loadPatch, loadRun, startExperiment, startGrade, startRepair } from './api'
import { ExperimentLauncher, RepairLauncher } from './components/ActionPanels'
import EvaluationPanel from './components/EvaluationPanel'
import RunInspector from './components/RunInspector'
import TaskBrowser from './components/TaskBrowser'
import type { EvaluationDetail, ExperimentOptions, GradeResult, Job, Overview, RunOptions, RunReport } from './types'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误'
}

function isAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

type Page = 'workspace' | 'experiments'

function pageFromHash(): Page {
  return window.location.hash === '#/experiments' ? 'experiments' : 'workspace'
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [overviewLoading, setOverviewLoading] = useState(true)
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [report, setReport] = useState<RunReport | null>(null)
  const [patch, setPatch] = useState('')
  const [runLoading, setRunLoading] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [grade, setGrade] = useState<GradeResult | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const handledJobs = useRef(new Set<string>())
  const [selectedEvaluationId, setSelectedEvaluationId] = useState<string | null>(null)
  const [evaluation, setEvaluation] = useState<EvaluationDetail | null>(null)
  const [evaluationLoading, setEvaluationLoading] = useState(false)
  const [evaluationError, setEvaluationError] = useState<string | null>(null)

  async function refresh(preferredRunId?: string, preferredEvaluationId?: string) {
    setOverviewLoading(true)
    setOverviewError(null)
    try {
      const data = await loadOverview()
      setOverview(data)
      const taskId = data.tasks.some(item => item.id === selectedTaskId) ? selectedTaskId : data.tasks[0]?.id ?? null
      setSelectedTaskId(taskId)
      const taskRuns = data.runs.filter(item => item.task_id === taskId)
      const desiredRun = preferredRunId ?? selectedRunId
      setSelectedRunId(taskRuns.some(item => item.run_id === desiredRun) ? desiredRun : taskRuns[0]?.run_id ?? null)
      const nextEvaluation = data.evaluations.find(item => !item.is_reference_fixture) ?? data.evaluations[0]
      const desiredEvaluation = preferredEvaluationId ?? selectedEvaluationId
      setSelectedEvaluationId(data.evaluations.some(item => item.id === desiredEvaluation)
        ? desiredEvaluation : nextEvaluation?.id ?? null)
    } catch (error) {
      setOverviewError(errorMessage(error))
    } finally {
      setOverviewLoading(false)
    }
  }

  useEffect(() => { void refresh() }, [])

  useEffect(() => { loadJobs().then(data => setJobs(data.jobs)).catch(() => undefined) }, [])

  useEffect(() => {
    if (!jobs.some(job => job.state === 'queued' || job.state === 'running')) return
    const timer = window.setInterval(() => { loadJobs().then(data => setJobs(data.jobs)).catch(() => undefined) }, 2000)
    return () => window.clearInterval(timer)
  }, [jobs])

  useEffect(() => {
    for (const job of jobs) {
      if ((job.state !== 'succeeded' && job.state !== 'failed') || handledJobs.current.has(job.id)) continue
      handledJobs.current.add(job.id)
      if (job.state === 'succeeded') {
        const preferred = job.kind === 'repair' && typeof job.result?.run_directory === 'string'
          ? job.result.run_directory.split(/[\\/]/).at(-1) : undefined
        const preferredEvaluation = job.kind === 'experiment' && typeof job.result?.evaluation_path === 'string'
          ? job.result.evaluation_path.split(/[\\/]/).at(-2) : undefined
        if (job.kind === 'grade' && job.details.run_id === selectedRunId && job.result) {
          setGrade(job.result as GradeResult)
        }
        void refresh(preferred, preferredEvaluation)
      }
    }
  }, [jobs])

  useEffect(() => {
    const syncPage = () => setPage(pageFromHash())
    window.addEventListener('hashchange', syncPage)
    return () => window.removeEventListener('hashchange', syncPage)
  }, [])

  useEffect(() => {
    if (!selectedRunId) { setReport(null); setPatch(''); setGrade(null); setRunError(null); setRunLoading(false); return }
    const controller = new AbortController()
    setRunLoading(true)
    setRunError(null)
    setReport(null)
    setPatch('')
    setGrade(null)
    Promise.all([loadRun(selectedRunId, controller.signal), loadPatch(selectedRunId, controller.signal), loadGrade(selectedRunId, controller.signal)])
      .then(([nextReport, nextPatch, nextGrade]) => { setReport(nextReport); setPatch(nextPatch); setGrade(nextGrade) })
      .catch(error => { if (!isAbort(error)) setRunError(errorMessage(error)) })
      .finally(() => { if (!controller.signal.aborted) setRunLoading(false) })
    return () => controller.abort()
  }, [selectedRunId])

  useEffect(() => {
    if (!selectedEvaluationId) { setEvaluation(null); setEvaluationError(null); setEvaluationLoading(false); return }
    const controller = new AbortController()
    setEvaluationLoading(true)
    setEvaluationError(null)
    setEvaluation(null)
    loadEvaluation(selectedEvaluationId, controller.signal)
      .then(setEvaluation)
      .catch(error => { if (!isAbort(error)) setEvaluationError(errorMessage(error)) })
      .finally(() => { if (!controller.signal.aborted) setEvaluationLoading(false) })
    return () => controller.abort()
  }, [selectedEvaluationId])

  const task = overview?.tasks.find(item => item.id === selectedTaskId) ?? null
  const taskRuns = useMemo(() => overview?.runs.filter(item => item.task_id === selectedTaskId) ?? [], [overview, selectedTaskId])
  const selectedRun = taskRuns.find(item => item.run_id === selectedRunId)
  const readyCount = overview?.tasks.filter(item => item.status === 'ready').length ?? 0
  const realCount = overview?.tasks.filter(item => item.origin === 'BugsInPy' && item.status === 'ready').length ?? 0

  function selectTask(id: string) {
    setSelectedTaskId(id)
    setSelectedRunId(overview?.runs.find(item => item.task_id === id)?.run_id ?? null)
  }

  async function launchRepair(options: RunOptions) {
    if (!task || task.status !== 'ready') throw new Error('请选择 ready 任务')
    const job = await startRepair(task.id, options)
    setJobs(current => [job, ...current])
  }

  async function launchGrade() {
    if (!task || !selectedRun?.has_patch) throw new Error('当前运行没有可验收补丁')
    const job = await startGrade(task.id, selectedRun.run_id)
    setJobs(current => [job, ...current])
  }

  async function launchExperiment(suite: string, method: string, limit: number | null, options: ExperimentOptions) {
    const job = await startExperiment(suite, method, limit, options)
    setJobs(current => [job, ...current])
  }

  return <div className="app-shell">
    <header className="app-header">
      <div className="header-inner">
        <a className="brand" href="#/workspace" aria-label="RepoFix 修复工作台"><span className="brand-mark">R<span>F</span></span><span><strong>RepoFix</strong><small>RESEARCH CONSOLE</small></span></a>
        <nav className="top-nav" aria-label="主导航">
          <a href="#/workspace" aria-current={page === 'workspace' ? 'page' : undefined}>修复工作台</a>
          <a href="#/experiments" aria-current={page === 'experiments' ? 'page' : undefined}>实验测评</a>
        </nav>
        <div className="header-actions"><span className="local-chip"><span className="online-dot" />本地 · 可运行</span><button type="button" className="button refresh" onClick={() => void refresh()} disabled={overviewLoading}>刷新数据 ↻</button></div>
      </div>
    </header>
    <main className="content">
      {page === 'workspace' ? <>
        <div className="page-intro"><div><div className="intro-kicker"><span className="kicker-line" />REPOFIX / EVIDENCE-LED REPAIR</div><h1>自动修复实验台</h1><p>选择已就绪任务，运行 Agent 修复与独立验收，并查看完整证据。</p></div><div className="intro-aside"><span>实验环境</span><strong>Python · Pytest · Docker</strong><small>本地运行 · 模型调用可能产生费用</small></div></div>
        {overviewError && <div className="page-error" role="alert"><strong>无法加载任务数据</strong><span>{overviewError}</span><button type="button" onClick={() => void refresh()}>重试</button></div>}
        <section className="stats-grid" aria-label="任务概览">
          <div className="stat-card"><span>任务总数</span><strong>{overview?.tasks.length ?? 0}</strong><small>含开发任务和真实案例</small></div>
          <div className="stat-card"><span>已就绪任务</span><strong>{readyCount}</strong><small>可进入正式评测</small></div>
          <div className="stat-card accent"><span>已就绪真实案例</span><strong>{realCount}</strong><small>BugsInPy 可评测任务</small></div>
        </section>
        <div className="workspace-grid">
          <TaskBrowser tasks={overview?.tasks ?? []} runs={overview?.runs ?? []} selectedId={selectedTaskId} onSelect={selectTask} />
          <div className="workspace-main"><RepairLauncher task={task} run={selectedRun} grade={grade} jobs={jobs} onRepair={launchRepair} onGrade={launchGrade} /><RunInspector task={task} runs={taskRuns} selectedRunId={selectedRunId} onSelectRun={setSelectedRunId} report={report} patch={patch} loading={runLoading} error={runError} /></div>
        </div>
      </> : <div className="evaluation-page">
        <div className="page-intro"><div><div className="intro-kicker"><span className="kicker-line" />REPOFIX / EXPERIMENT RESULTS</div><h1>实验测评</h1><p>查看模型实验、部分任务试点与参考补丁自检；评测记录数量不代表修复成功次数。</p></div><div className="intro-aside"><span>已保存报告</span><strong>{overview?.evaluations.length ?? 0} 份评测记录</strong><small>包含自检与模型实验</small></div></div>
        {overviewError && <div className="page-error" role="alert"><strong>无法加载评测数据</strong><span>{overviewError}</span><button type="button" onClick={() => void refresh()}>重试</button></div>}
        <ExperimentLauncher tasks={overview?.tasks ?? []} jobs={jobs} onStart={launchExperiment} />
        <EvaluationPanel evaluations={overview?.evaluations ?? []} selectedId={selectedEvaluationId} onSelect={setSelectedEvaluationId} detail={evaluation} loading={evaluationLoading} error={evaluationError} />
      </div>}
      <footer className="footer"><span>RepoFix · 可复现的软件修复实验</span><span>证据优先于结论 · 2026</span></footer>
    </main>
  </div>
}
