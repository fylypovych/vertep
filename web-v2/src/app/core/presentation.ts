import { Job } from './models';

export const JOB_STATUS_GROUPS = {
  active: ['SCRIPTING', 'STORYBOARD_GENERATING', 'ASSET_GENERATION', 'VIDEO_GENERATION', 'ASSEMBLY', 'PUBLISHING'],
  queued: ['NEW', 'STORYBOARD_QUEUED'],
  waiting: ['WAITING_FOR_SYSTEM', 'PENDING_APPROVAL', 'STORYBOARD_PENDING_APPROVAL', 'STORYBOARD_REVISION_REQUESTED'],
  completed: ['SCRIPT_READY', 'ASSETS_READY', 'VIDEO_READY', 'READY', 'PUBLISHED'],
  failed: ['FAILED', 'STORYBOARD_FAILED'],
} as const;

const STATUS_LABELS: Record<string, string> = {
  NEW: 'Нове', WAITING_FOR_SYSTEM: 'Очікує систему', SCRIPTING: 'Створення сценарію',
  STORYBOARD_QUEUED: 'Розкадровка в черзі', STORYBOARD_GENERATING: 'Створення розкадровки',
  STORYBOARD_PENDING_APPROVAL: 'Розкадровка очікує затвердження',
  STORYBOARD_REVISION_REQUESTED: 'Запитані правки', STORYBOARD_REJECTED: 'Розкадровку відхилено',
  STORYBOARD_FAILED: 'Помилка розкадровки', SCRIPT_READY: 'Сценарій готовий',
  ASSET_GENERATION: 'Створення матеріалів', ASSETS_READY: 'Матеріали готові',
  VIDEO_GENERATION: 'Створення відео', VIDEO_READY: 'Відео готове', ASSEMBLY: 'Монтаж',
  PENDING_APPROVAL: 'Очікує затвердження', READY: 'Готове', PUBLISHING: 'Публікація',
  PUBLISHED: 'Опубліковано', FAILED: 'Помилка', PAUSED: 'Призупинено', CANCELLED: 'Скасовано',
  ONLINE: 'У мережі', FREE: 'Готовий', BUSY: 'Зайнятий', DRAINING: 'Завершує роботу',
  UPDATING: 'Оновлюється', RECOVERING: 'Відновлюється', OFFLINE: 'Не в мережі',
  ERROR: 'Помилка', QUARANTINED: 'Ізольований', REVOKED: 'Відкликаний', SELF_TESTING: 'Самодіагностика',
};

const ROLE_LABELS: Record<string, string> = {
  core: 'Основний вузол', gpu: 'GPU-вузол', text: 'Текстовий вузол', voice: 'Голосовий вузол',
  publisher: 'Вузол публікації', backup: 'Вузол резервного копіювання', monitoring: 'Вузол моніторингу',
};

export function statusLabel(status?: string): string { return STATUS_LABELS[status || ''] || status || 'Невідомо'; }
export function roleLabel(role?: string): string { return ROLE_LABELS[role || ''] || role || 'Не визначено'; }
export function inStatusGroup(status: string, group: keyof typeof JOB_STATUS_GROUPS): boolean {
  return (JOB_STATUS_GROUPS[group] as readonly string[]).includes(status);
}

export interface JobStatistics {
  total: number;
  active: number;
  queued: number;
  waiting: number;
  completed: number;
  failed: number;
  paused: number;
  cancelled: number;
}

export function computeJobStatistics(jobs: Job[]): JobStatistics {
  const stats: JobStatistics = { total: jobs.length, active: 0, queued: 0, waiting: 0, completed: 0, failed: 0, paused: 0, cancelled: 0 };
  for (const job of jobs) {
    if (inStatusGroup(job.status, 'active')) stats.active++;
    else if (inStatusGroup(job.status, 'queued')) stats.queued++;
    else if (inStatusGroup(job.status, 'waiting')) stats.waiting++;
    else if (inStatusGroup(job.status, 'completed')) stats.completed++;
    else if (inStatusGroup(job.status, 'failed')) stats.failed++;
    else if (job.status === 'PAUSED') stats.paused++;
    else if (job.status === 'CANCELLED') stats.cancelled++;
  }
  return stats;
}

export const JOB_ACTION_STATES: Record<string, readonly string[]> = {
  pause: [...JOB_STATUS_GROUPS.active, 'NEW', 'STORYBOARD_QUEUED'],
  resume: ['PAUSED'],
  retry: ['FAILED', 'STORYBOARD_FAILED'],
  regenerate: ['READY', 'ASSETS_READY', 'VIDEO_READY'],
  cancel: [...JOB_STATUS_GROUPS.active, ...JOB_STATUS_GROUPS.queued, 'SCRIPT_READY', 'ASSETS_READY', 'VIDEO_READY'],
  approve: ['READY', 'PENDING_APPROVAL', 'STORYBOARD_PENDING_APPROVAL'],
  publish: ['READY', 'FAILED'],
  delete: ['NEW', 'PAUSED', 'READY', 'FAILED', 'CANCELLED', 'PUBLISHED', 'STORYBOARD_REJECTED', 'STORYBOARD_FAILED'],
};

export function jobActionAllowed(action: string, status?: string): boolean {
  return !!status && (JOB_ACTION_STATES[action] || []).includes(status);
}

export const WORKER_STATUS_LABELS: Record<string, string> = {
  ONLINE: 'У мережі', FREE: 'Готовий', BUSY: 'Зайнятий', DRAINING: 'Завершує роботу',
  UPDATING: 'Оновлюється', RECOVERING: 'Відновлюється', OFFLINE: 'Не в мережі',
  ERROR: 'Помилка', QUARANTINED: 'Ізольований', REVOKED: 'Відкликаний', SELF_TESTING: 'Самодіагностика',
  READY: 'Готовий',
};

export function workerStatusLabel(status?: string): string { return WORKER_STATUS_LABELS[status || ''] || status || 'Невідомо'; }

export const TASK_TYPE_LABELS: Record<string, string> = {
  image: 'Зображення', video: 'Відео',
};

export function taskTypeLabel(value?: string): string { return TASK_TYPE_LABELS[value || ''] || value || '—'; }

export const CHANNEL_LABELS: Record<string, string> = {
  youtube: 'YouTube', tiktok: 'TikTok', instagram: 'Instagram',
  facebook: 'Facebook', threads: 'Threads', telegram: 'Telegram',
};

export function channelLabel(value?: string): string { return CHANNEL_LABELS[value || ''] || value || '—'; }
