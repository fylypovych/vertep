import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { readFileSync } from 'node:fs';
import { test, expect } from '@playwright/test';
import { createHash } from 'node:crypto';

const JOB = { job_id: 'j1', topic: 'Тема', character_id: 'c', events: [], event_log: [], scenes: [], artifacts: [],
  published_to: [], publication_results: {}, stages: {}, approved: true, approval_status: 'approved', channels: ['youtube'] };

function mockApi(page: any, role: string, state: string, extra: Record<string, unknown> = {}) {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: role, user: role, role } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state, reason: null }, version: '0' } });
    for (const [suffix, json] of Object.entries(extra)) {
      if (path.endsWith(suffix)) return route.fulfill({ json });
    }
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
}

const ACTIONS: [string, string, (page: any) => any][] = [
  ['retry_job', 'FAILED', page => page.locator('button', { hasText: 'Повторити' }).first()],
  ['cancel_job', 'SCRIPT_GENERATING', page => page.locator('button[title], button', { hasText: 'Скасувати' }).first()],
  ['approve_job', 'SCRIPT_PENDING_APPROVAL', page => page.getByTestId('approve-script-button').first()],
  ['publish_job', 'READY', page => page.locator('button', { hasText: 'Опублікувати' }).first()],
];

for (const [action, status, button] of ACTIONS) {
  test(`${action}: admin у NORMAL може`, async ({ page }) => {
    await mockApi(page, 'admin', 'NORMAL', { '/jobs/j1': { ...JOB, status } });
    await page.goto('/jobs/j1');
    await expect(button(page)).toBeEnabled({ timeout: 20000 });
  });
  test(`${action}: viewer не може`, async ({ page }) => {
    await mockApi(page, 'viewer', 'NORMAL', { '/jobs/j1': { ...JOB, status } });
    await page.goto('/jobs/j1');
    await expect(button(page)).toBeDisabled({ timeout: 20000 });
  });
  test(`${action}: admin у READ_ONLY не може`, async ({ page }) => {
    await mockApi(page, 'admin', 'READ_ONLY', { '/jobs/j1': { ...JOB, status } });
    await page.goto('/jobs/j1');
    await expect(button(page)).toBeDisabled({ timeout: 20000 });
  });
}

test('посилання дає файл копії з іменем і хешем із панелі', async ({ page }) => {
  // The 600 MB transfer through CORE and Backup Service is covered by test_issues_427_429.py.
  const body = Buffer.from('encrypted-backup-bytes');
  const server = createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/octet-stream', 'content-disposition': 'attachment; filename="snap-1.vtbackup"' });
    response.end(body);
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const sha256 = createHash('sha256').update(body).digest('hex');
  await mockApi(page, 'admin', 'NORMAL', { '/system/backups': { snapshots: [{ snapshot_id: 'snap-1', created_at: '2026-11-06T10:00:00Z', sha256 }] } });
  await page.goto('/settings?tab=backup');
  const link = page.getByTestId('backup-download-link');
  await expect(link).toHaveAttribute('download', 'snap-1.vtbackup', { timeout: 20000 });
  await expect(link).toHaveAttribute('title', `SHA-256: ${sha256}`);
  await expect(link).toHaveAttribute('href', '/api/system/backups/snap-1/download');
  // A browser download bypasses page.route, so the panel's link is pointed at the local endpoint.
  await link.evaluate((element: HTMLAnchorElement, url: string) => { element.href = url; }, `${origin}/snap-1`);
  const [download] = await Promise.all([page.waitForEvent('download'), link.click()]);
  expect(download.suggestedFilename()).toBe('snap-1.vtbackup');
  const received = readFileSync(await download.path());
  expect(received.equals(body)).toBe(true);
  expect(createHash('sha256').update(received).digest('hex')).toBe(sha256);
  server.close();
});

test('сповіщення про збій GPU називає провайдера, причину й перезапуск', async ({ page }) => {
  const message = 'Збій GPU-провайдера primary на вузлі gpu-1: не відповідає. Автоматичного перемикання немає, потрібен перезапуск.';
  await mockApi(page, 'admin', 'NORMAL', { '/api/alerts': [{ id: '1', severity: 'error', type: 'GPU_PROVIDER_FAILED', source: 'gpu', node_name: 'gpu-1', state: 'firing', message, details: { provider: 'primary' }, created_at: '2026-11-06T10:00:00Z' }] });
  await page.goto('/alerts');
  await expect(page.getByText(message)).toBeVisible({ timeout: 20000 });
  await expect(page.getByText('gpu-1').first()).toBeVisible();
});

for (const [language, text] of [['uk', 'Перевірте введені дані'], ['en', 'Check the entered data']] as const) {
  test(`помилка перевірки даних показується мовою ${language}`, async ({ page }) => {
    await page.addInitScript((value: string) => localStorage.setItem('vertep_language', value), language);
    await mockApi(page, 'admin', 'NORMAL', { '/jobs/j1': { ...JOB, status: 'FAILED' } });
    await page.route('**/api/jobs/j1/retry', (route: any) => route.fulfill({ status: 422, json: { detail: [{ loc: ['body'], msg: 'bad', type: 'value_error' }], code: 'validation_error' } }));
    await page.goto('/jobs/j1');
    await page.locator('button', { hasText: language === 'en' ? 'Retry' : 'Повторити' }).first().click({ timeout: 20000 });
    await page.locator('app-confirm-dialog button').last().click();
    await expect(page.getByText(text).first()).toBeVisible({ timeout: 10000 });
  });
}
