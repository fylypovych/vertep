import { test, expect } from '@playwright/test';

const scenarios = [
  { id: 'S01', name: 'Bootstrap', rt_id: 'rt::S01', rt_issue: 442, description: '', checks: ['bootstrap_first_run'] },
  { id: 'S02', name: 'Enrollment', rt_id: 'rt::S02', rt_issue: 42, description: '', checks: ['node_connectivity'] },
  { id: 'S03', name: 'GPU', rt_id: 'rt::S03', rt_issue: 441, description: '', checks: ['comfyui_probe'] },
  { id: 'S04', name: 'Backup', rt_id: 'rt::S04', rt_issue: 442, description: '', checks: ['backup_roundtrip'] },
  { id: 'S05', name: 'Migration', rt_id: 'rt::S05', rt_issue: 442, description: '', checks: ['migration_artifacts'] },
  { id: 'S06', name: 'Update', rt_id: 'rt::S06', rt_issue: 442, description: '', checks: ['update_interrupt'] },
  { id: 'S07', name: 'Release trust', rt_id: 'rt::S07', rt_issue: 54, description: '', checks: ['release_signature'] },
  { id: 'S08', name: 'Publisher', rt_id: 'rt::S08', rt_issue: 47, description: '', checks: ['publisher_receipt'] },
];

test('Реальні тести: показує джерело каталогу, GitHub Issue для кожного сценарію й агрегацію', async ({ page }) => {
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/real-tests/scenarios')) return route.fulfill({ json: { scenarios, available_checks: [], version: '0' } });
    if (path.endsWith('/real-tests/runs')) return route.fulfill({ json: { runs: [] } });
    return route.fulfill({ json: {} });
  });

  await page.goto('/real-tests');
  await expect(page.getByTestId('real-tests-source-note')).toBeVisible({ timeout: 20000 });
  // 8 сценаріїв
  await expect(page.locator('[data-testid^="real-tests-scenario-S"]')).toHaveCount(8);
  // GitHub Issue відображається
  await expect(page.getByTestId('real-tests-scenario-issue-S01')).toContainText('#442');
  // агрегація: два сценарії з Issue #442
  const issue442 = await page.locator('[data-testid^="real-tests-scenario-issue-"]').allTextContents();
  const count442 = issue442.filter(t => t.includes('#442')).length;
  expect(count442).toBeGreaterThanOrEqual(2);
});

const issues = [
  { number: 42, id: 'ir.0.0.0.42', coverage: 'partial', note: 'S02 перевіряє лише multi-host enrollment', scenarios: ['S02'] },
  { number: 63, id: 'ir.0.0.0.63', coverage: 'none', note: 'Перевіряє сам Runner', scenarios: [] },
  { number: 329, id: 'ir.0.0.3.29', coverage: 'none', note: 'Ізоляція LAB', scenarios: [] },
  { number: 442, id: 'ir.0.0.4.42', coverage: 'partial', note: 'S01, S04, S05, S06', scenarios: ['S01', 'S04', 'S05', 'S06'] },
  { number: 459, id: 'ir.0.0.4.59', coverage: 'none', note: 'Приймання vertep remove на реальному Ubuntu/systemd', scenarios: [] },
];

test('Реальні тести: відкриті ir з покриттям, Issue без сценарію і часткове покриття не обіцяє закриття; uk/en', async ({ page }) => {
  const covered = scenarios.map(item => ({ ...item, issue_id: `ir.${item.rt_issue}`, issue_coverage: 'partial', covers: ['AC1'] }));
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/real-tests/scenarios')) return route.fulfill({ json: { source: 'local_catalog', scenarios: covered, issues, issues_snapshot: { verified_at: '2026-11-13', method: 'gh' }, available_checks: [], version: '0' } });
    if (path.endsWith('/real-tests/runs')) return route.fulfill({ json: { runs: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto('/real-tests');
  await expect(page.locator('[data-testid^="real-tests-scenario-S"]')).toHaveCount(8, { timeout: 20000 });
  await expect(page.getByTestId('real-tests-scenario-issue-S01')).toContainText('Перевіряє критерії: AC1');
  await expect(page.getByTestId('real-tests-scenario-partial-S01')).toContainText('автоматично його не закриває');
  const list = page.getByTestId('real-tests-issues');
  await expect(list.locator('li')).toHaveCount(5);
  // #460: the open remove acceptance Issue is visible as uncovered, with no run button or close promise.
  const remove = page.getByTestId('real-tests-issue-459');
  await expect(remove).toContainText('ir.0.0.4.59');
  await expect(remove).toContainText('Немає виконуваного сценарію');
  await expect(remove.getByRole('button')).toHaveCount(0);
  await expect(page.getByTestId('real-tests-issues-snapshot')).toContainText('2026-11-13');
  await expect(page.getByTestId('real-tests-issues-snapshot')).toContainText('не жива синхронізація');
  await expect(page.getByTestId('real-tests-issue-63')).toContainText('Немає виконуваного сценарію');
  await expect(page.getByTestId('real-tests-issue-442')).toContainText('Часткове покриття сценаріями');
  await expect(page.getByTestId('real-tests-issue-442')).toContainText('S01, S04, S05, S06');
  await page.evaluate(() => localStorage.setItem('vertep_language', 'en'));
  await page.reload();
  await expect(page.getByTestId('real-tests-issue-63')).toContainText('No runnable scenario', { timeout: 20000 });
  await expect(page.getByTestId('real-tests-scenario-partial-S01')).toContainText('does not close it automatically');
});
