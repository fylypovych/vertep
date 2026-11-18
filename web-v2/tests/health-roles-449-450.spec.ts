import { test, expect } from '@playwright/test';

test('Health відокремлює metadata, optional і минулу GPU відмову; uk/en', async ({ page }) => {
  const historyQueries: string[] = [];
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/health/history')) historyQueries.push(url.searchParams.get('changes') || '');
    if (path.endsWith('/health/history')) return route.fulfill({ json: { history: [
      // Two polls of one failure fold into one incident; the same failure an hour later is separate.
      ...['10:00:00', '10:00:10', '11:00:00'].map(time => ({ timestamp: `2026-11-10T${time}Z`, role: 'core', status: 'UNHEALTHY',
        checks: { role: 'core', checked_at: time, required: ['gpu'], gpu: [false, 'health_probe_failed'] } })),
    ] } });
    if (path.endsWith('/health')) return route.fulfill({ json: { status: 'HEALTHY', service: 'core', jobs: 0, checks: {
      role: 'core', checked_at: 'now', required: ['docker', 'core_api'], selected_roles: ['core'],
      docker: [true, 'health_probe_ok'], core_api: [true, 'local'], monitoring: [null, 'not-applicable'], backup: [true, 'writable: /app/backups'],
    } } });
    return route.fulfill({ json: {} });
  });
  await page.goto('/health');
  const health = page.getByTestId('health-page');
  await expect(health).toContainText('Система справна');
  await expect(health).toContainText('API основного вузла');
  await expect(health).toContainText('Не застосовується');
  await expect(health).toContainText('Сховище доступне для запису');
  await expect(health).toContainText('Відеокарта: Несправний');
  await expect(health).not.toContainText('role:');
  await expect(health).not.toContainText('checked_at');
  await expect(health).not.toContainText('required');
  await expect(health).toContainText('Несправності за останню добу');
  await expect(health.getByText('Відеокарта: Несправний', { exact: true })).toHaveCount(2);
  // Incidents come from the server journal, not from the last minutes of raw polling.
  expect(historyQueries[0]).toBe('10');
  await page.evaluate(() => localStorage.setItem('vertep_language', 'en'));
  await page.reload();
  await expect(health).toContainText('Core API');
  await expect(health).toContainText('Not applicable');
  await expect(health).toContainText('Graphics card: Failed');
  await expect(health).toContainText('Failures in the last day');
  await expect(health).not.toContainText('Система справна');
});

for (const role of ['voice', 'publisher']) {
  for (const terminal of ['SUCCEEDED', 'FAILED']) {
    test(`середовище ${role}: QUEUED → APPLYING → ${terminal}, reload`, async ({ page }) => {
      const active = ['text', 'backup', 'monitoring'];
      const moduleId = role === 'voice' ? 'audio' : role;
      let state = '';
      const requests: string[][] = [];
      await page.route('**/api/**', async route => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
        if (path.endsWith('/nodes/module-catalog')) return route.fulfill({ json: {
          nodes: [{ node_id: 'core', role: 'core' }],
          modules: [{ module_id: moduleId, label: role, version: '1.0.0', state: 'INSTALLED',
            runtime_role: role, placement: [], permissions: [],
            nodes: [{ node_id: 'core', compatible_reasons: [], runtime_missing: state !== 'SUCCEEDED', instance: null }] }],
        } });
        if (path.endsWith('/system/roles')) {
          if (request.method() === 'POST') {
            requests.push(request.postDataJSON().roles);
            state = 'QUEUED';
          }
          return route.fulfill({ json: {
            active_roles: state === 'SUCCEEDED' ? [...active, role] : active,
            queued: ['QUEUED', 'APPLYING'].includes(state),
            deployment: { state, phase: state === 'APPLYING' ? 'VERIFYING' : state,
              error: state === 'FAILED' ? 'Не вдалося запустити службу' : null },
          } });
        }
        return route.fulfill({ json: {} });
      });

      await page.goto('/modules');
      const prepare = page.getByTestId(`prepare-core-${moduleId}`);
      const deployment = page.getByTestId('deployment-state');
      await prepare.click();
      await expect.poll(() => requests).toEqual([[...active, role]]);
      await page.reload();
      await expect(prepare).toBeDisabled();
      await expect(deployment).toHaveText('Очікує системного виконавця');
      state = 'APPLYING';
      await expect(deployment).toHaveText('Перевірка готовності служб', { timeout: 10000 });
      state = terminal;
      const outcome = terminal === 'SUCCEEDED' ? 'Локальне розгортання застосовано' : 'Застосування не вдалося';
      await expect(deployment).toContainText(outcome, { timeout: 10000 });
      if (terminal === 'FAILED') {
        await expect(page.getByTestId('deployment-error')).toContainText('Не вдалося запустити');
        await expect(prepare).toBeEnabled();
      }
      await page.reload();
      await expect(deployment).toContainText(outcome);
      expect(requests).toHaveLength(1);
    });
  }
}

test('застарілий запит показує діагностику executor після reload', async ({ page }) => {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const json = path.endsWith('/session') ? { authenticated: true, user: 'admin', username: 'admin', role: 'admin' }
      : path.endsWith('/nodes/module-catalog') ? { modules: [], nodes: [] }
      : path.endsWith('/system/roles') ? { active_roles: [], requested_roles: ['voice'], queued: true,
        deployment: { state: 'QUEUED', stalled: true, message: 'Системний виконавець не прийняв запит. Перевірте службу розгортання.' } } : {};
    return route.fulfill({ json });
  });
  await page.goto('/modules');
  await page.reload();
  await expect(page.getByTestId('deployment-state')).toHaveText('Потрібна перевірка системного виконавця');
  await expect(page.getByTestId('core-runtime')).toContainText('Системний виконавець не прийняв запит');
});
