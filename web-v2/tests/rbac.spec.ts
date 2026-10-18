import { test, expect } from '@playwright/test';

interface RbacMocks {
  role: 'admin' | 'viewer';
  state?: string;
  reason?: string | null;
  jobs?: Array<Record<string, unknown>>;
}

function mockApi(page: any, opts: RbacMocks = { role: 'admin' }) {
  const state = opts.state || 'NORMAL';
  const reason = opts.reason || null;
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    if (p.endsWith('/session')) {
      return route.fulfill({ json: { authenticated: true, username: opts.role === 'admin' ? 'admin' : 'reader', user: opts.role === 'admin' ? 'admin' : 'reader', role: opts.role } });
    }
    if (p.endsWith('/status')) {
      return route.fulfill({ json: { system: { state, reason }, version: '0.0.1.52' } });
    }
    if (p.endsWith('/jobs') && m === 'GET') return route.fulfill({ json: opts.jobs || [] });
    if (p.endsWith('/jobs') && m === 'POST') return route.fulfill({ status: 403, json: { detail: 'Insufficient role' } });
    if (p.endsWith('/workers')) return route.fulfill({ json: [] });
    if (p.endsWith('/characters')) return route.fulfill({ json: [] });
    if (p.endsWith('/brands')) return route.fulfill({ json: [] });
    if (p.endsWith('/workflows')) return route.fulfill({ json: [] });
    if (p.endsWith('/alerts')) return route.fulfill({ json: [] });
    if (m === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
}

test.describe('RBAC — Issue #29', () => {
  test('admin бачить налаштування та може створювати завдання', async ({ page }) => {
    await mockApi(page, { role: 'admin' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const nav = page.locator('nav').first();
    await expect(nav.locator('a[href="/settings?tab=system"]')).toBeVisible();
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(false);
    expect(await createButton.getAttribute('title')).toBe('Створити нове завдання');
  });

  test('viewer: приховує admin nav та блоковує admin мутацію в UI', async ({ page }) => {
    await mockApi(page, { role: 'viewer' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const nav = page.locator('nav').first();
    await expect(nav.locator('a[href="/settings?tab=system"]')).toHaveCount(0, { timeout: 20000 });
    // Admin mutation заблокована через policy: кнопка неактивна, причина пояснена.
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    expect(await createButton.getAttribute('title')).toContain('Недостатньо прав');
  });

  test('viewer: прямий admin mutation через API повертає 403', async ({ page }) => {
    await mockApi(page, { role: 'viewer' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    // fetch з контексту сторінки перехоплюється page.route → перевірка 403.
    const status = await page.evaluate(async () => {
      const res = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ topic: 'blocked' }) });
      return res.status;
    });
    expect(status).toBe(403);
  });

  test('system-state: READ_ONLY блоковує мутацію з поясненням причини', async ({ page }) => {
    await mockApi(page, { role: 'admin', state: 'READ_ONLY', reason: 'Планова зупинка' });
    await page.goto('/jobs');
    await page.waitForLoadState('networkidle').catch(() => {});
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    const title = (await createButton.getAttribute('title')) || '';
    expect(title).toContain('READ_ONLY');
    expect(title).toContain('Планова зупинка');
  });
});

type StatusHandler = (route: any) => Promise<void> | void;

function mockWithStatus(page: any, statusHandler: StatusHandler) {
  return page.route('**/api/**', async (route: any) => {
    const r = route.request();
    const p = new URL(r.url()).pathname;
    const m = r.method();
    if (p.endsWith('/session')) {
      return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    }
    if (p.endsWith('/status')) return statusHandler(route);
    if (p.endsWith('/jobs') && m === 'GET') return route.fulfill({ json: [] });
    if (p.endsWith('/jobs') && m === 'POST') return route.fulfill({ status: 201, json: {} });
    if (p.endsWith('/workers')) return route.fulfill({ json: [] });
    if (p.endsWith('/characters')) return route.fulfill({ json: [] });
    if (p.endsWith('/brands')) return route.fulfill({ json: [] });
    if (p.endsWith('/workflows')) return route.fulfill({ json: [] });
    if (p.endsWith('/alerts')) return route.fulfill({ json: [] });
    if (m === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
}

test.describe('Policy fail-closed — Issue #168.3/#114', () => {
  test('тривале очікування /api/status не дає мутації — причину пояснено', async ({ page }) => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    await mockWithStatus(page, async (route) => {
      await gate;
      return route.fulfill({ json: { system: { state: 'NORMAL', reason: null } } });
    });
    await page.goto('/jobs');
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    // Поки стан системи невідомий — мутація fail-closed, не оптимістичний NORMAL.
    expect(await createButton.isDisabled()).toBe(true);
    expect((await createButton.getAttribute('title')) || '').toContain('ще не визначено');
    // Щойно авторитетна відповідь надходить — кнопка активується.
    release();
    await expect(createButton).toBeEnabled({ timeout: 20000 });
    expect(await createButton.getAttribute('title')).toBe('Створити нове завдання');
  });

  test('відповідь без system.state не стає NORMAL', async ({ page }) => {
    await mockWithStatus(page, (route) => route.fulfill({ json: { version: '0.0.1.52' } }));
    await page.goto('/jobs');
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    const title = (await createButton.getAttribute('title')) || '';
    expect(title).toContain('Стан системи невідомий');
  });

  test('невідомий рядок стану не стає NORMAL', async ({ page }) => {
    await mockWithStatus(page, (route) => route.fulfill({ json: { system: { state: 'TOTALLY_BOGUS' } } }));
    await page.goto('/jobs');
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    expect((await createButton.getAttribute('title')) || '').toContain('Невідомий стан системи');
  });

  test('помилка /api/status не стає NORMAL', async ({ page }) => {
    await mockWithStatus(page, (route) => route.fulfill({ status: 500, json: { detail: 'boom' } }));
    await page.goto('/jobs');
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    expect((await createButton.getAttribute('title')) || '').toContain('Неможливо отримати стан системи');
  });

  test('порожній system-обʼєкт не стає NORMAL', async ({ page }) => {
    await mockWithStatus(page, (route) => route.fulfill({ json: { system: {} } }));
    await page.goto('/jobs');
    await expect(page.getByTestId('jobs-page')).toBeVisible({ timeout: 20000 });
    const createButton = page.getByTestId('create-job-button');
    await expect(createButton).toBeVisible();
    expect(await createButton.isDisabled()).toBe(true);
    expect((await createButton.getAttribute('title')) || '').toContain('Стан системи невідомий');
  });
});

// Issue #168.3/#168.4: the UI guard matrix must match the server
// STATE_OPERATION_POLICY (core/system_state.py). create_job stays admitted in
// MAINTENANCE/UPDATING/RECOVERING (job is durably queued as WAITING_FOR_SYSTEM)
// and resources stay mutable in MAINTENANCE but are blocked in READ_ONLY.
test.describe('System-state mutation matrix — Issue #168.3/#168.4', () => {
  async function gotoAndWait(page: any, path: string) {
    await page.goto(path);
    await page.waitForLoadState('networkidle').catch(() => {});
  }

  for (const state of ['NORMAL', 'MAINTENANCE', 'UPDATING', 'RECOVERING']) {
    test(`create-job дозволено у стані ${state}`, async ({ page }) => {
      await mockApi(page, { role: 'admin', state });
      await gotoAndWait(page, '/jobs');
      const btn = page.getByTestId('create-job-button');
      await expect(btn).toBeVisible({ timeout: 20000 });
      await expect(btn).toBeEnabled();
      expect(await btn.getAttribute('title')).toBe('Створити нове завдання');
    });
  }

  for (const state of ['READ_ONLY', 'EMERGENCY']) {
    test(`create-job заблоковано у стані ${state}`, async ({ page }) => {
      await mockApi(page, { role: 'admin', state });
      await gotoAndWait(page, '/jobs');
      const btn = page.getByTestId('create-job-button');
      await expect(btn).toBeVisible({ timeout: 20000 });
      expect(await btn.isDisabled()).toBe(true);
      expect((await btn.getAttribute('title')) || '').toContain(state);
    });
  }

  const resourcePages = [
    { path: '/characters', testId: 'create-character-button' },
    { path: '/brands', testId: 'create-brand-button' },
    { path: '/workflows', testId: 'create-workflow-button' },
  ];

  for (const rp of resourcePages) {
    test(`${rp.testId}: заблоковано у READ_ONLY з поясненням`, async ({ page }) => {
      await mockApi(page, { role: 'admin', state: 'READ_ONLY', reason: 'Планова зупинка' });
      await gotoAndWait(page, rp.path);
      const btn = page.getByTestId(rp.testId);
      await expect(btn).toBeVisible({ timeout: 20000 });
      expect(await btn.isDisabled()).toBe(true);
      const title = (await btn.getAttribute('title')) || '';
      expect(title).toContain('READ_ONLY');
      expect(title).toContain('Планова зупинка');
    });

    test(`${rp.testId}: дозволено у MAINTENANCE (resource_management)`, async ({ page }) => {
      await mockApi(page, { role: 'admin', state: 'MAINTENANCE' });
      await gotoAndWait(page, rp.path);
      const btn = page.getByTestId(rp.testId);
      await expect(btn).toBeVisible({ timeout: 20000 });
      await expect(btn).toBeEnabled();
    });
  }

  test('model pull: дозволено у MAINTENANCE, заблоковано у UPDATING', async ({ page }) => {
    await mockApi(page, { role: 'admin', state: 'UPDATING' });
    await gotoAndWait(page, '/settings?tab=models');
    const input = page.getByTestId('pull-model-input');
    await expect(input).toBeVisible({ timeout: 20000 });
    await input.fill('llama3');
    const btn = page.getByTestId('pull-model-button');
    expect(await btn.isDisabled()).toBe(true);
    expect((await btn.getAttribute('title')) || '').toContain('UPDATING');
  });

  test('model pull: дозволено у MAINTENANCE', async ({ page }) => {
    await mockApi(page, { role: 'admin', state: 'MAINTENANCE' });
    await gotoAndWait(page, '/settings?tab=models');
    const input = page.getByTestId('pull-model-input');
    await expect(input).toBeVisible({ timeout: 20000 });
    await input.fill('llama3');
    const btn = page.getByTestId('pull-model-button');
    await expect(btn).toBeEnabled();
  });
});
