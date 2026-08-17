import type { RunStatus } from '@/api/types'

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

export function formatUtcDate(value: string | null | undefined): string {
  if (!value) return '—'
  return `${new Date(value).toISOString().slice(0, 16).replace('T', ' ')} UTC`
}

export function shortDigest(value: string): string {
  return `${value.slice(0, 14)}…${value.slice(-8)}`
}

export function runTagType(
  status: RunStatus,
): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'PASSED') return 'success'
  if (status === 'FAILED' || status === 'INFRA_ERROR' || status === 'TIMED_OUT') return 'danger'
  if (status === 'RUNNING' || status === 'PREPARING') return 'warning'
  if (status === 'QUEUED') return 'primary'
  return 'info'
}

export function changeStatusLabel(status: string): string {
  return (
    {
      DRAFT: '草稿',
      IN_REVIEW: '待审批',
      CHANGES_REQUESTED: '需修改',
      CANDIDATE: '候选版本',
      PUBLISHED: '已发布',
    }[status] ?? status
  )
}

export function changeTagType(
  status: string,
): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'PUBLISHED') return 'success'
  if (status === 'CHANGES_REQUESTED') return 'danger'
  if (status === 'IN_REVIEW' || status === 'CANDIDATE') return 'warning'
  if (status === 'DRAFT') return 'info'
  return 'primary'
}
