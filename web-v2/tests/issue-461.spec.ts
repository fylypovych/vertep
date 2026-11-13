import { test, expect } from '@playwright/test';

// The backend contract of these states is proven against a controlled Ollama HTTP
// endpoint in tests/test_issue_461_installed_models.py; this checks what the UI makes of it.
function mockApi(page: any, models: () => any) {
  return page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/system/models')) return route.fulfill({ json: models() });
    if (path.endsWith('/system/models/nodes')) return route.fulfill({ json: { nodes: [] } });
    if (path.endsWith('/system/models/catalog')) return route.fulfill({ json: { source: '', verified_at: '', models: [] } });
    if (path.endsWith('/models/voices')) return route.fulfill({ json: { voices: [] } });
    return route.fulfill({ json: {} });
  });
}

test('недоступна Ollama не видається за порожній список; «Повторити» відновлює без reload', async ({ page }) => {
  let answer: any = { models: [], nodes: [], local_status: 'unavailable', local_error: 'Сервіс OLLAMA_URL недоступний.' };
  await mockApi(page, () => answer);
  await page.goto('/settings?tab=models');
  const unavailable = page.getByTestId('installed-unavailable');
  await expect(unavailable).toContainText('Не вдалося отримати список моделей', { timeout: 20000 });
  await expect(unavailable).toContainText('OLLAMA_URL');
  await expect(page.getByTestId('installed-empty')).toHaveCount(0);
  answer = { models: [{ name: 'llama3.2:3b' }], nodes: [], local_status: 'ok', local_error: null };
  await page.getByTestId('installed-retry').click();
  await expect(page.getByTestId('model-row-llama3.2:3b')).toBeVisible();
  await expect(unavailable).toHaveCount(0);
});

test('CORE без локальної Ollama і справді порожній список розрізняються; англійська', async ({ page }) => {
  let answer: any = { models: [], nodes: [], local_status: 'not_selected', local_error: null };
  await mockApi(page, () => answer);
  await page.goto('/settings?tab=models');
  await expect(page.getByTestId('installed-not-local')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('installed-empty')).toHaveCount(0);
  answer = { models: [], nodes: [], local_status: 'ok', local_error: null };
  await page.evaluate(() => localStorage.setItem('vertep_language', 'en'));
  await page.reload();
  await expect(page.getByTestId('installed-empty')).toBeVisible({ timeout: 20000 });
  answer = { models: [], nodes: [], local_status: 'unavailable', local_error: null };
  await page.reload();
  await expect(page.getByTestId('installed-unavailable')).toContainText('The local Ollama model list could not be retrieved', { timeout: 20000 });
});
