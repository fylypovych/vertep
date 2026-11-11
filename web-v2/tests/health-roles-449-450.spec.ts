import { test, expect } from '@playwright/test';

test('Health відокремлює metadata, optional і минулу GPU відмову; uk/en', async ({ page }) => {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/health/history')) return route.fulfill({ json: { history: [
      { timestamp: '2026-11-10T10:00:00Z', role: 'core', status: 'UNHEALTHY', checks: { role: 'core', required: ['gpu'], gpu: [false, 'health_probe_failed'] } },
      ...['10:01', '10:02'].map(time => ({ timestamp: `2026-11-10T${time}:00Z`, role: 'core', status: 'HEALTHY', checks: { role: 'core', checked_at: time, required: ['docker'], docker: [true, 'health_probe_ok'] } })),
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
  await expect(health.getByText('Docker: Справний', { exact: true })).toHaveCount(1);
  await page.evaluate(() => localStorage.setItem('vertep_language', 'en'));
  await page.reload();
  await expect(health).toContainText('Core API');
  await expect(health).toContainText('Not applicable');
  await expect(health).toContainText('Graphics card: Failed');
  await expect(health).not.toContainText('Система справна');
});

for (const roles of [['voice'], ['publisher'], ['voice', 'publisher']]) {
  for (const terminal of ['SUCCEEDED', 'FAILED']) {
    test(`ролі ${roles.join('+')}: QUEUED → APPLYING → ${terminal}, reload`, async ({ page }) => {
      let active: string[] = ['text', 'backup', 'monitoring'];
      let requested: string[] | null = null;
      let state = '';
      let posts = 0;
      await page.route('**/api/**', async route => {
        const req = route.request();
        const path = new URL(req.url()).pathname;
        if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
        if (path.endsWith('/system/roles')) {
          if (req.method() === 'POST') {
            posts++;
            requested = req.postDataJSON().roles;
            state = 'QUEUED';
            return route.fulfill({ json: { state, requested_roles: requested } });
          }
          return route.fulfill({ json: { node_role: 'core', active_roles: active, requested_roles: requested,
            queued: !!requested, deployment: { state, phase: state === 'APPLYING' ? 'VERIFYING' : state, error: state === 'FAILED' ? 'Не вдалося запустити службу синтезу мовлення' : undefined },
            available_roles: ['gpu', 'voice', 'publisher', 'text', 'backup', 'monitoring'].map(id => ({ id, label: id,
              runtime_status: active.includes(id) ? 'READY' : id === 'gpu' ? 'DEGRADED' : 'READY',
              runtime_evidence: active.includes(id) ? { source: 'local' } : { source: 'nodes', nodes: [{ node_id: 'remote', live: true }] },
            })),
          } });
        }
        return route.fulfill({ json: {} });
      });
      await page.goto('/settings?tab=roles');
      await expect(page.getByTestId('role-status-gpu')).toHaveText('Локально вимкнено');
      for (const role of roles) {
        await expect(page.getByTestId(`role-status-${role}`)).toHaveText('Локально вимкнено');
        await page.getByLabel(role, { exact: true }).check();
      }
      await page.getByTestId('roles-save-button').click();
      await expect.poll(() => posts).toBe(1);
      await expect(page.getByTestId('roles-save-button')).toBeDisabled();
      await page.reload();
      for (const role of roles) await expect(page.getByLabel(role, { exact: true })).toBeChecked();
      await expect(page.getByTestId('deployment-state')).toHaveText('Очікує системного виконавця');
      state = 'APPLYING';
      await expect(page.getByTestId('deployment-state')).toHaveText('Перевірка готовності служб', { timeout: 10000 });
      state = terminal;
      if (terminal === 'SUCCEEDED') active = requested!;
      requested = null;
      await expect(page.getByTestId('deployment-state')).toContainText(terminal === 'SUCCEEDED' ? 'Локальне розгортання застосовано' : 'Застосування не вдалося', { timeout: 10000 });
      for (const role of roles) {
        if (terminal === 'SUCCEEDED') await expect(page.getByLabel(role, { exact: true })).toBeChecked();
        else await expect(page.getByLabel(role, { exact: true })).not.toBeChecked();
      }
      if (terminal === 'FAILED') await expect(page.getByTestId('deployment-error')).toContainText('Не вдалося запустити');
      await expect(page.getByTestId('role-status-text')).toHaveText('Готовий');
      await expect(page.getByTestId('role-status-backup')).toHaveText('Готовий');
      await expect(page.getByTestId('role-status-monitoring')).toHaveText('Готовий');
      await page.reload();
      await expect(page.getByTestId('deployment-state')).toContainText(terminal === 'SUCCEEDED' ? 'Локальне розгортання застосовано' : 'Застосування не вдалося');
      expect(posts).toBe(1);
    });
  }
}

test('застарілий запит показує діагностику executor після reload', async ({ page }) => {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const json = path.endsWith('/session') ? { authenticated: true, user: 'admin', username: 'admin', role: 'admin' }
      : path.endsWith('/system/roles') ? { node_role: 'core', active_roles: [], requested_roles: ['voice'], queued: true,
        deployment: { state: 'QUEUED', stalled: true, message: 'Системний виконавець не прийняв запит. Перевірте службу розгортання.' },
        available_roles: [{ id: 'voice', label: 'Voice Node', runtime_status: 'UNKNOWN' }] } : {};
    return route.fulfill({ json });
  });
  await page.goto('/settings?tab=roles');
  await expect(page.getByTestId('deployment-state')).toHaveText('Потрібна перевірка системного виконавця');
  await expect(page.getByTestId('settings-roles')).toContainText('Системний виконавець не прийняв запит');
  await expect(page.getByTestId('roles-save-button')).toBeDisabled();
});
