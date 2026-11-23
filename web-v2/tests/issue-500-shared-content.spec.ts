import { activeModules } from './module-menu';
import { test, expect } from '@playwright/test';

// #500: the owner brand's «Спільний контент» tab shows other brands' material with
// its characters, sends a decision bound to the exact version/hash, can revoke and
// can start its own publication of the same artifact.
test('власник погоджує, відкликає й створює власну публікацію спільного матеріалу', async ({ page }) => {
  const calls: { path: string; body: any }[] = [];
  let state = 'pending';
  await page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'mod-b', username: 'mod-b', role: 'moderator', brands: ['brand-b'] } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path.endsWith('/brands')) return route.fulfill({ json: [{ id: 'brand-b', name: 'Бренд B', enabled: true }] });
    if (path.endsWith('/shared-content')) return route.fulfill({ json: { brand_id: 'brand-b', items: [{
      job_id: 'job-a1', brand_id: 'brand-a', character_id: 'hero', version: 1, sha256: 'ab'.repeat(32),
      owners: { 'brand-b': state }, history: [] }] } });
    if (request.method() === 'POST') {
      calls.push({ path, body: request.postDataJSON() });
      if (path.endsWith('/shared-approval')) state = 'approved';
      if (path.endsWith('/shared-approval/revoke')) state = 'revoked';
      if (path.endsWith('/shared-publications')) return route.fulfill({ json: { job_id: 'job-b1' } });
      return route.fulfill({ json: {} });
    }
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
  await activeModules(page);
  await page.goto('/brands');
  const item = page.getByTestId('shared-job-a1');
  await expect(item).toContainText('Очікує погодження', { timeout: 20000 });
  await item.getByText('Погодити', { exact: true }).click();
  await expect(item).toContainText('Погоджено');
  expect(calls[0]).toEqual({ path: '/api/jobs/job-a1/shared-approval',
    body: { owner_brand: 'brand-b', decision: 'approve', version: 1, expected_sha256: 'ab'.repeat(32) } });
  await expect(page.getByTestId('shared-view-job-a1')).toHaveAttribute('href', '/api/jobs/job-a1/videos/1?sha256=' + 'ab'.repeat(32));
  await page.getByTestId('own-publication-job-a1').click();
  await page.getByTestId('own-title').fill('Версія бренду B');
  await page.getByTestId('own-description').fill('Опис B');
  await page.getByTestId('own-hashtags').fill('#b, #news');
  await page.getByTestId('own-cover').fill('https://cdn.example/b.jpg');
  await page.getByTestId('own-create').click();
  await expect(page).toHaveURL(/\/jobs\/job-b1$/);
  expect(calls[1]).toEqual({ path: '/api/jobs/job-a1/shared-publications', body: {
    brand_id: 'brand-b', title: 'Версія бренду B', description: 'Опис B', hashtags: ['#b', '#news'], cover: 'https://cdn.example/b.jpg' } });
  await activeModules(page);
  await page.goto('/brands');
  await page.getByTestId('shared-job-a1').getByText('Відкликати', { exact: true }).click();
  await expect(page.getByTestId('shared-job-a1')).toContainText('Відкликано');
});
