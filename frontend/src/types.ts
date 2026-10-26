export type TaskSummary = {
  id: string
  suite: string
  category: string
  status: string
  config: string
  issue?: string | null
  origin: string
}

export type RunSummary = {
  run_id: string
  created_at_utc?: string | null
  task_id: string
  title?: string | null
  validation_status: string
  has_patch: boolean
  grade_status?: string | null
}

export type GradeResult = {
  task_id: string
  status: string
  public_test_status: string | null
  hidden_test_status: string | null
  reason: string
  patch_review?: PatchReview | null
}

export type PatchReview = {
  policy_version?: string
  status: 'passed' | 'warnings' | 'rejected'
  changed_lines: number
  findings: { code: string; severity: 'block' | 'warning'; message: string; line_number: number | null }[]
}

export type Job = {
  id: string
  kind: 'repair' | 'grade' | 'experiment'
  state: 'queued' | 'running' | 'succeeded' | 'failed'
  created_at_utc: string
  details: Record<string, string | number | null>
  result: Record<string, unknown> | null
  error: string | null
}

export type RunOptions = {
  workflow: 'two-agent' | 'single-agent' | 'competitive' | 'dynamic'
  engine: 'legacy' | 'langgraph'
  context: 'ast-callgraph' | 'traceback-only'
  max_tokens: number
}

export type ExperimentOptions = RunOptions & {
  formal: boolean
  max_submission_attempts: number
  max_model_calls: number
}

export type EvaluationMethod = {
  method: string
  passed: number
  total?: number
  tasks?: number
  success_rate: number
  status_counts?: Record<string, number>
}

export type EvaluationSummary = {
  id: string
  created_at_utc?: string | null
  task_count: number
  backend?: string
  methods?: EvaluationMethod[]
  partial_run: boolean
  selected_task_count?: number | null
  is_reference_fixture: boolean
  experiment_protocol?: string | null
  formal_status?: 'complete' | 'partial' | 'incomplete' | null
}

export type EvaluationRecord = {
  method: string
  task_id: string
  status: string
  public_test_status?: string | null
  holdout_test_status?: string | null
  no_submission_reason?: string
}

export type SubmissionMetrics = {
  selected_tasks: number
  submitted_patches: number
  valid_patches: number
  passed_patches: number
  valid_submission_rate: number
  conditional_acceptance_rate: number | null
  end_to_end_success_rate: number
}

export type EvaluationDetail = EvaluationSummary & {
  records?: EvaluationRecord[]
  preflight?: { task_id: string }[]
  usage_records?: { task_id: string }[]
  limitations?: string[]
  provider?: string
  task_errors?: { task_id: string; error: string }[]
  submission_metrics?: SubmissionMetrics
  retry_records?: { task_id: string; attempt: number; kind: string; reason: string }[]
}

export type Overview = {
  tasks: TaskSummary[]
  runs: RunSummary[]
  evaluations: EvaluationSummary[]
}

export type Evidence = {
  id: string
  kind: string
  file_path?: string | null
  line_number?: number | null
  symbol_name?: string | null
  content?: string
}

export type RunReport = {
  run_id: string
  task: { id: string; title?: string; issue?: string }
  initial_public_test?: {
    status?: string
    failures?: unknown[]
    duration_seconds?: number
  }
  diagnosis?: {
    hypothesis?: { target_file?: string; target_symbol?: string; explanation?: string }
  }
  repair?: {
    candidate?: { explanation?: string }
    status?: string
  }
  validation?: {
    status?: string
    reason?: string
    public_tests_passed?: boolean
    patch_review?: PatchReview | null
  }
  evidence?: Evidence[]
  candidate_selection?: {
    selected_attempt: number
    reason: string
    attempts: {
      number: number
      strategy: string
      repair_status: string
      validation_status: string | null
      public_tests_passed: boolean
      public_failure_count: number | null
      candidate_explanation: string | null
      review: PatchReview | null
      patch: string | null
    }[]
  }
}
