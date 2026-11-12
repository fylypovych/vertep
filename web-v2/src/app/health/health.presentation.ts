/** Only the check tuple contract is rendered; metadata never becomes a green check. */
export function serviceChecks(checks: Record<string, unknown>) {
  const required = Array.isArray(checks['required']) ? checks['required'] as string[] : null;
  return Object.entries(checks).filter(([, value]) => Array.isArray(value) && value.length === 2
    && (typeof value[0] === 'boolean' || value[0] === null) && typeof value[1] === 'string')
    .map(([key, value]) => ({ key, value: value as [boolean | null, string], required: required?.includes(key) ?? null }));
}

const NAMES: Record<string, string> = {
  docker: 'Docker', postgres: 'PostgreSQL', redis: 'Redis', core_api: 'API основного вузла',
  gpu: 'Відеокарта', cuda: 'Обчислення CUDA', comfyui: 'ComfyUI', gpu_worker: 'GPU Worker',
  ollama: 'Ollama', tts: 'Синтез мовлення', publisher: 'Публікація', backup: 'Резервні копії',
  monitoring: 'Моніторинг',
};
const REASONS: Record<string, string> = {
  local: 'Локальний сервіс', ok: 'Перевірку пройдено', open: 'З’єднання встановлено',
  health_probe_ok: 'Перевірку пройдено', skipped: 'Перевірку пропущено',
  'not-applicable': 'Не застосовується',
  health_endpoint_unavailable: 'Сервіс не відповідає', health_connection_unavailable: 'З’єднання недоступне',
  health_docker_unavailable: 'Docker недоступний', health_postgres_unavailable: 'PostgreSQL недоступний',
  health_redis_unavailable: 'Redis недоступний', health_gpu_unavailable: 'Відеокарта недоступна',
  health_gpu_runtime_failed: 'Помилка середовища GPU', health_gpu_compute: 'Обчислення GPU перевірено',
  health_worker_ready: 'Worker готовий', health_worker_not_ready: 'Worker не готовий',
  health_worker_pending_enrollment: 'Очікує реєстрації Worker',
  health_observation_unavailable: 'Немає даних перевірки сервера',
  health_observation_stale: 'Дані перевірки сервера застаріли',
  health_observation_expired: 'Дані перевірки сервера застаріли', health_probe_failed: 'Перевірку не пройдено',
};
export function checkName(key: string): string { return NAMES[key] || key; }
export function checkStatus(value: boolean | null): string {
  return value === null ? 'Не застосовується' : value ? 'Справний' : 'Несправний';
}
export function healthLabel(status: string): string {
  return ({ HEALTHY: 'Система справна', UNHEALTHY: 'Система має проблеми', OK: 'Справний' } as Record<string, string>)[status] || status;
}
export function checkReason(detail: string): string {
  if (REASONS[detail]) return REASONS[detail];
  if (detail.startsWith('not-applicable:')) return 'Не застосовується';
  if (detail.startsWith('writable:')) return 'Сховище доступне для запису';
  if (detail.startsWith('HTTP ')) return 'Сервіс відповідає';
  if (detail === 'SELECT 1 = 1' || detail === 'PING = PONG') return 'Перевірку пройдено';
  return 'Діагностичні відомості';
}

/** Same incident rule as the API: identical adjacent events at most this far apart. */
const INCIDENT_GAP_MS = 60_000;

/** Fold repeated polling of one failure; separate incidents and the original events stay. */
export function stateChanges<T extends { checks: Record<string, unknown>; status: string; role: string; timestamp?: string }>(history: T[]): T[] {
  const signature = (item: T) => JSON.stringify([item.role, item.status,
    item.checks['required'], item.checks['selected_roles'], serviceChecks(item.checks)]);
  return history.filter((entry, index) => {
    if (!index) return true;
    const previous = history[index - 1];
    if (signature(entry) !== signature(previous)) return true;
    const gap = Date.parse(entry.timestamp || '') - Date.parse(previous.timestamp || '');
    return Number.isFinite(gap) && gap > INCIDENT_GAP_MS;
  }).reverse();
}
