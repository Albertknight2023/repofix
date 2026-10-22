export function displayStatus(value?: string | null): string {
  const labels: Record<string, string> = {
    passed: '通过',
    failed: '失败',
    error: '环境错误',
    timeout: '超时',
    no_submission: '未提交',
    infrastructure_error: '基础设施中断',
    workflow_error: '工作流错误',
    public_tests_passed: '公开测试通过',
    public_tests_failed: '公开测试失败',
    rejected: '已拒绝',
    rejected_patch: '质量审查拒绝',
    invalid_patch: '无效补丁',
    not_run: '未运行',
    ready: '已就绪',
    candidate: '未就绪',
  }
  return value ? labels[value] ?? value : '待检查'
}

export function statusTone(value?: string | null): 'success' | 'danger' | 'warning' | 'neutral' {
  if (value === 'passed' || value === 'public_tests_passed' || value === 'ready') return 'success'
  if (value === 'failed' || value === 'public_tests_failed' || value === 'rejected' || value === 'rejected_patch' || value === 'invalid_patch') return 'danger'
  if (value === 'timeout' || value === 'error' || value === 'infrastructure_error' || value === 'workflow_error' || value === 'candidate') return 'warning'
  return 'neutral'
}

export function localTime(value?: string | null): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).format(date)
}

export function compactId(value: string): string {
  return value.length > 26 ? `${value.slice(0, 18)}…${value.slice(-6)}` : value
}
