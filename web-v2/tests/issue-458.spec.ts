import { test, expect } from '@playwright/test';

interface State { statusCalls: number; hangStatus: boolean; pending: Array<() => void>; saves: any[]; failSave: boolean }

function mockApi(page: any, state: State) {
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) {
      state.statusCalls++;
      // The background poll after the page is loaded never answers: a hung or slow server.
      if (state.hangStatus) return new Promise<void>(resolve => state.pending.push(() => { route.abort().catch(() => {}); resolve(); }));
      return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    }
    if (path.endsWith('/settings/publishing')) {
      if (request.method() === 'PUT') {
        state.saves.push(JSON.parse(request.postData() || '{}'));
        if (state.failSave) return route.fulfill({ status: 503, json: { detail: 'Сервер тимчасово недоступний' } });
        return route.fulfill({ json: JSON.parse(request.postData() || '{}') });
      }
      return route.fulfill({ json: { public_url: '', telegram_channel_id: '@news_channel' } });
    }
    if (path.endsWith('/settings/secrets')) return route.fulfill({ status: 500, json: { detail: 'down' } });  // a concurrent failing request
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
}

test('завислий фоновий polling не перекриває сторінку: ввід, фокус і навігація працюють', async ({ page }) => {
  const state: State = { statusCalls: 0, hangStatus: false, pending: [], saves: [], failSave: false };
  await page.clock.install();
  await mockApi(page, state);
  await page.goto('/settings?tab=publishing');
  const input = page.getByTestId('publishing-public-url');
  await expect(input).toBeEnabled({ timeout: 20000 });
  await input.fill('https://draft.example');
  const before = state.statusCalls;
  state.hangStatus = true;
  // The header polls /api/status every 30 seconds on every page.
  await page.clock.fastForward('00:31');
  await expect.poll(() => state.statusCalls).toBeGreaterThan(before);
  await expect(page.getByTestId('global-loading-overlay')).toHaveCount(0);
  // Work continues while the poll hangs: typing, focus and the unsaved value stay.
  await input.press('End');
  await input.type('.com');
  await expect(input).toBeFocused();
  await expect(input).toHaveValue('https://draft.example.com');
  // Navigation is not blocked either; a hanging request does not leave a stuck overlay.
  await page.locator("a[href='/settings?tab=secrets']").click();
  await expect(page).toHaveURL(/tab=secrets/);
  await page.locator("a[href='/settings?tab=publishing']").first().click();
  await expect(page.getByTestId('global-loading-overlay')).toHaveCount(0);
  state.pending.forEach(release => release());  // cancel the hung requests
  await expect(page.getByTestId('global-loading-overlay')).toHaveCount(0);
});

test('явна операція має власний стан помилки й успіху без глобального блокування', async ({ page }) => {
  const state: State = { statusCalls: 0, hangStatus: false, pending: [], saves: [], failSave: true };
  await mockApi(page, state);
  await page.goto('/settings?tab=publishing');
  const input = page.getByTestId('publishing-public-url');
  await expect(input).toBeEnabled({ timeout: 20000 });
  await input.fill('https://vertep.example.com');
  await page.getByTestId('publishing-save').click();
  await expect(page.getByTestId('publishing-error')).toContainText('Сервер тимчасово недоступний');
  await expect(page.getByTestId('global-loading-overlay')).toHaveCount(0);
  await expect(input).toHaveValue('https://vertep.example.com');
  state.failSave = false;
  await page.getByTestId('publishing-save').click();
  await expect.poll(() => state.saves.length).toBe(2);
  await expect(page.getByTestId('publishing-error')).toHaveCount(0);
  await expect(page.getByTestId('global-loading-overlay')).toHaveCount(0);
});
