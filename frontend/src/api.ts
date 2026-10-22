import type { EvaluationDetail, ExperimentOptions, GradeResult, Job, Overview, RunOptions, RunReport } from './types'

async function json<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal })
  if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
  return response.json() as Promise<T>
}

export const loadOverview = (signal?: AbortSignal) => json<Overview>('/api/overview', signal)
export const loadRun = (id: string, signal?: AbortSignal) =>
  json<RunReport>(`/api/runs/${encodeURIComponent(id)}`, signal)
export const loadEvaluation = (id: string, signal?: AbortSignal) =>
  json<EvaluationDetail>(`/api/evaluations/${encodeURIComponent(id)}`, signal)

export async function loadGrade(id: string, signal?: AbortSignal): Promise<GradeResult | null> {
  const response = await fetch(`/api/runs/${encodeURIComponent(id)}/grade`, { signal })
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`验收结果读取失败（HTTP ${response.status}）`)
  return response.json() as Promise<GradeResult>
}

export const loadJobs = () => json<{ jobs: Job[] }>('/api/jobs')

async function submitJob(path: string, payload: object): Promise<Job> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-RepoFix-Action': '1' },
    body: JSON.stringify(payload),
  })
  const data = await response.json() as Job & { error?: string }
  if (!response.ok) throw new Error(data.error ?? `提交失败（HTTP ${response.status}）`)
  return data
}

export const startRepair = (task_id: string, options: RunOptions) =>
  submitJob('/api/jobs/repair', { task_id, ...options })

export const startGrade = (task_id: string, run_id: string) =>
  submitJob('/api/jobs/grade', { task_id, run_id })

export const startExperiment = (suite: string, method: string, limit: number | null, options: ExperimentOptions) =>
  submitJob('/api/jobs/experiment', { suite, method, limit, ...options })

export async function loadPatch(id: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(`/api/runs/${encodeURIComponent(id)}/patch`, { signal })
  if (response.status === 404) return ''
  if (!response.ok) throw new Error(`补丁读取失败（HTTP ${response.status}）`)
  return response.text()
}
