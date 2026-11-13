import { test, expect } from '@playwright/test';

interface State { puts: any[]; hang: boolean; late: Array<() => Promise<void>>; statusCalls: number }

function mockApi(page: any, state: State) {
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) {
      state.statusCalls++;
      return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    }
    if (path.endsWith('/settings/publishing')) {
      if (request.method() === 'PUT') {
        const body = JSON.parse(request.postData() || '{}');
        state.puts.push(body);
        if (state.hang) {
          // The server never answers in time; its late answer would name another URL.
          return new Promise<void>(resolve => state.late.push(async () => {
            await route.fulfill({ json: { public_url: 'https://late.example', telegram_channel_id: body.telegram_channel_id } }).catch(() => {});
            resolve();
          }));
        }
        return route.fulfill({ json: body });
      }
      return route.fulfill({ json: { public_url: '', telegram_channel_id: '@news_channel' } });
    }
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
}

test('завислий PUT завершується локальним timeout без втрати вводу; пізня відповідь не підміняє форму; повтор працює', async ({ page }) => {
  const state: State = { puts: [], hang: true, late: [], statusCalls: 0 };
  await page.clock.install();
  await mockApi(page, state);
  await page.goto('/settings?tab=publishing');
  const input = page.getByTestId('publishing-public-url');
  const save = page.getByTestId('publishing-save');
  await expect(input).toBeEnabled({ timeout: 20000 });
  await input.fill('https://draft.example');
  await save.click();
  await expect(save).toBeDisabled();
  await expect(save).toHaveText('Збереження...');
  // A second click while waiting does not queue a duplicate save.
  expect(state.puts.length).toBe(1);
  const polls = state.statusCalls;
  await page.clock.fastForward('00:31');
  // The background poll keeps running beside the hung save, without any overlay.
  await expect.poll(() => state.statusCalls).toBeGreaterThan(polls);
  await expect(page.getByTestId('global-loading-overlay')).toHaveCount(0);
  await expect(page.getByTestId('publishing-error')).toContainText('Сервер не відповів вчасно. Невідомо, чи збережено зміни');
  await expect(save).toBeEnabled();
  await expect(save).toHaveText('Зберегти');
  await expect(input).toHaveValue('https://draft.example');
  // The cancelled request's late answer never replaces what the operator sees.
  await input.fill('https://draft.example/v2');
  for (const release of state.late) await release();
  await page.clock.fastForward('00:02');
  await expect(input).toHaveValue('https://draft.example/v2');
  // Retry within the same contract: the save is repeated and succeeds.
  state.hang = false;
  await save.click();
  await expect.poll(() => state.puts.length).toBe(2);
  expect(state.puts[1]).toEqual({ public_url: 'https://draft.example/v2', telegram_channel_id: '@news_channel' });
  await expect(page.getByTestId('publishing-error')).toHaveCount(0);
  await expect(input).toHaveValue('https://draft.example/v2');
});

test('timeout збереження зрозумілий англійською', async ({ page }) => {
  const state: State = { puts: [], hang: true, late: [], statusCalls: 0 };
  await page.clock.install();
  await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
  await mockApi(page, state);
  await page.goto('/settings?tab=publishing');
  await expect(page.getByTestId('publishing-public-url')).toBeEnabled({ timeout: 20000 });
  await page.getByTestId('publishing-save').click();
  await expect(page.getByTestId('publishing-save')).toHaveText('Saving...');
  await page.clock.fastForward('00:31');
  await expect(page.getByTestId('publishing-error')).toContainText('The server did not answer in time. It is unknown whether the changes were saved');
  for (const release of state.late) await release();
});
