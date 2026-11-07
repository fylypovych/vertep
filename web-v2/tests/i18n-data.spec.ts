import { test, expect } from '@playwright/test';

const job = (id: string, status: string) => ({ job_id: id, topic: 'Topic', status, character_id: 'c1', priority: 5, created_at: '2026-01-31T15:00:00Z',
  events: ['NEW'], event_log: [], scenes: [{ scene_id: 's1', index: 1, status: 'READY', prompt: 'p', voiceover: 'v', duration: 3 }], artifacts: [], published_to: [],
  publication_results: {}, stages: { script: { status: 'COMPLETED' } }, approved: false, approval_status: 'pending', retries: 0, max_retries: 3, task_type: 'video', version: 1 });
const statuses = ['NEW', 'SCRIPT_GENERATING', 'SCRIPT_PENDING_APPROVAL', 'STORYBOARD_PENDING_APPROVAL', 'ASSET_GENERATION', 'VIDEO_GENERATION', 'READY', 'PUBLISHED', 'FAILED', 'CANCELLED', 'PAUSED', 'WAITING_FOR_SYSTEM'];
const jobs = statuses.map((status, index) => job(`j${index}`, status));
const worker = (status: string, id: string) => ({ node_id: id, node_name: id, role: 'gpu', status, runtime_status: 'READY', capabilities: ['image_generation'], version: '1', last_seen: '2026-01-31T15:00:00Z', gpu_name: 'GPU', vram_mb: 8192, supported_tasks: [], supported_workflows: ['*'] });
const fixtures: Record<string, unknown> = {
  '/api/status': { system: { state: 'MAINTENANCE', reason: null }, version: '1', core: 'OK', postgres: 'OK', redis: 'OK', storage: 'OK', queue: { depth: 1, inflight: 1, dead_letter: 1 }, scheduler: { pending: 1 }, orchestration: { active_jobs: 1, active_scenes: 1 }, resources: { cpu: 1, ram: 1, disk: 1 }, providers: { llm: { backend: 'ollama', options: ['ollama', 'openai'], configured: true }, tts: { backend: 'none', options: ['none'], configured: false } }, update: {} },
  '/api/jobs': { items: jobs, total: jobs.length, page: 1, per_page: 50, pages: 1, has_more: false },
  '/api/workers': ['READY', 'BUSY', 'OFFLINE', 'DRAINING', 'ERROR', 'QUARANTINED'].map((status, index) => worker(status, `w${index}`)),
  '/api/nodes': ['READY', 'BUSY', 'OFFLINE'].map((status, index) => worker(status, `w${index}`)),
  '/api/nodes/w0': worker('READY', 'w0'),
  '/api/alerts': ['firing', 'acknowledged', 'resolved'].map((state, index) => ({ id: String(index), severity: ['error', 'warning', 'info'][index], type: ['WORKER_OFFLINE', 'UPDATE_FAILED', 'SYSTEM_STATE'][index], state, message: 'm', created_at: '2026-01-31T15:00:00Z' })),
  '/api/characters': [{ id: 'c1', character_id: 'c1', name: 'Name', language: 'uk', enabled: true }, { id: 'c2', character_id: 'c2', name: 'Name 2', language: 'en', enabled: false }],
  '/api/brands': [{ id: 'b1', name: 'Name', enabled: true, active: true }, { id: 'b2', name: 'Name 2', enabled: false, active: false }],
  '/api/real-tests/scenarios': { scenarios: [{ id: 's', name: 'Name', rt_id: 'S01', rt_issue: 1, description: 'd', checks: ['c'] }], available_checks: ['c'], version: '1' },
  '/api/real-tests/runs': { runs: [{ test_run_id: 'r', rt_id: 'S01', status: 'PASSED', initiator: 'a', progress: 'x', started_at: '2026-01-31T15:00:00Z' }, { test_run_id: 'r2', rt_id: 'S01', status: 'FAILED', initiator: 'a', progress: 'x', started_at: '2026-01-31T15:00:00Z' }] },
  '/api/security/check': { ok: false, recommendation: 'Облікові дані середовища та сертифікати відповідають політиці', weak_or_missing: ['X'], checks: { secrets_store: { status: 'warning', detail: 'ключ даних збережено без запечатування' }, certificates: { server_certificate: { status: 'expiring', expires_at: '2027-01-31T10:00:00Z' }, node_ca: { status: 'missing' } }, integrations: [{ name: 'youtube', status: 'not_configured' }], effective_config: {} } },
};

const ROUTES = ['/', '/jobs', '/jobs/j2', '/jobs/j6', '/jobs/j8', '/workers', '/workers/w0', '/alerts', '/characters', '/brands', '/workflows', '/published', '/logs', '/real-tests', '/profile',
  '/settings?tab=security', '/settings?tab=integrations', '/settings?tab=roles', '/settings?tab=system', '/settings?tab=update', '/settings?tab=backup', '/settings?tab=models', '/settings?tab=telegram', '/settings?tab=publishing', '/settings?tab=video-engine', '/settings?tab=workflow-mode'];

for (const route of ROUTES) {
  test(`англійською з даними: ${route}`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
    await page.route('**/api/**', async (r: any) => {
      const path = new URL(r.request().url()).pathname;
      if (path.endsWith('/session')) return r.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
      const detail = path.match(/^\/api\/jobs\/(j\d+)$/);
      if (detail) return r.fulfill({ json: jobs.find(item => item.job_id === detail[1]) });
      return r.fulfill({ json: fixtures[path] ?? (path.includes('/settings') || path.includes('/system') || path.includes('/telegram') ? {} : []) });
    });
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    const leftover = await page.evaluate(() => {
      const found: string[] = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode.textContent?.trim() || '';
        if (/[А-Яа-яЄєІіЇї]/.test(text) && text !== 'УК') found.push(text);
      }
      for (const el of document.querySelectorAll('[title],[placeholder],[aria-label]')) {
        for (const name of ['title', 'placeholder', 'aria-label']) {
          const value = el.getAttribute(name) || '';
          if (/[А-Яа-яЄєІіЇї]/.test(value) && value !== 'Мова / Language') found.push(value);
        }
      }
      return [...new Set(found)];
    });
    expect(leftover).toEqual([]);
  });
}
