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