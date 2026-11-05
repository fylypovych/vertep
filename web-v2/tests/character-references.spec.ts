import { test, expect } from '@playwright/test';

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
);
const file = { name: 'ref.png', mimeType: 'image/png', buffer: PNG };

function mockApi(page: any, uploads: string[]) {
  const stored: Array<Record<string, unknown>> = [];
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/characters') && method === 'GET') return route.fulfill({ json: [{ id: 'old_one', name: 'Старий', language: 'uk' }] });
    if (path.endsWith('/characters') && method === 'POST') return route.fulfill({ json: JSON.parse(request.postData() || '{}') });
    if (path.endsWith('/references') && method === 'POST') {
      uploads.push(path);
      stored.push({ filename: `${stored.length}.png`, sha256: 'x', size: PNG.length });
      return route.fulfill({ json: stored[stored.length - 1] });
    }
    if (path.endsWith('/references')) return route.fulfill({ json: stored });
    if (path.includes('/references/')) return route.fulfill({ body: PNG, contentType: 'image/png' });
    return route.fulfill({ json: method === 'GET' ? [] : {} });
  });
}

test('референс старого персонажа завантажується одразу з вікна редагування', async ({ page }) => {
  const uploads: string[] = [];
  await mockApi(page, uploads);
  await page.goto('/characters');
  await page.getByTestId('edit-character-button').click();
  await page.getByTestId('character-reference-input').setInputFiles(file);
  await expect(page.getByTestId('character-reference')).toHaveCount(1);
  expect(uploads).toEqual(['/api/characters/old_one/references']);
});

test('референс нового персонажа завантажується лише після збереження', async ({ page }) => {
  const uploads: string[] = [];
  await mockApi(page, uploads);
  await page.goto('/characters');
  await page.getByTestId('create-character-button').click();
  await page.getByTestId('character-reference-input').setInputFiles(file);
  await expect(page.getByTestId('character-reference-pending')).toHaveCount(1);
  expect(uploads).toEqual([]);
  await page.getByRole('button', { name: 'Зберегти' }).click();
  await expect(page.getByTestId('character-modal')).toHaveCount(0);
  expect(uploads).toHaveLength(1);
  expect(uploads[0]).toMatch(/^\/api\/characters\/character_[0-9a-f]+\/references$/);
});
