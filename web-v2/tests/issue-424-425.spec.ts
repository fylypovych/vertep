import { test, expect } from '@playwright/test';

function mockApi(page: any, role: string, extra: Record<string, unknown>) {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: role, user: role, role } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    for (const [suffix, json] of Object.entries(extra)) {
      if (path.endsWith(suffix)) return route.fulfill({ json });
    }
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
}

test('резервну копію можна завантажити, а панель нагадує про ключ', async ({ page }) => {
  await mockApi(page, 'admin', { '/system/backups': { snapshots: [{ snapshot_id: 'snap-1', created_at: '2026-11-06T10:00:00Z', sha256: 'abc' }] } });
  await page.goto('/settings?tab=backup');
  const link = page.getByTestId('backup-download-link');
  await expect(link).toHaveAttribute('href', '/api/system/backups/snap-1/download', { timeout: 20000 });
  await expect(page.getByTestId('backup-key-reminder')).toContainText('ключ');
});

test('повернення до статичного режиму заблоковане, поки є динамічні завдання', async ({ page }) => {
  await mockApi(page, 'admin', { '/settings/workflow-mode': { mode: 'dynamic', default: 'static', dynamic_available: true, active_dynamic_jobs: 2 } });
  await page.goto('/settings?tab=workflow-mode');
  await page.getByTestId('workflow-mode-select').selectOption('static', { timeout: 20000 });
  await expect(page.getByTestId('workflow-mode-blocked')).toContainText('2');
  await expect(page.getByTestId('workflow-mode-apply')).toBeDisabled();
});
