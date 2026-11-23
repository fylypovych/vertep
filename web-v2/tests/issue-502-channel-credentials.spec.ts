import { activeModules } from './module-menu';
import { test, expect } from '@playwright/test';

// #502: the administrator sees which credentials a channel publishes with, stores
// the channel's own token (never shown back) or explicitly allows the global ones.
test('адміністратор задає токен каналу або явно дозволяє глобальні облікові дані', async ({ page }) => {
  let state: any = { channel_id: 'ch-1', mode: 'missing', configured: [], use_global_credentials: false,
                     warning: 'Для каналу не задано облікових даних: публікація в нього заблокована.' };
  const puts: any[] = [];
  await page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin', brands: null } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path.endsWith('/brands')) return route.fulfill({ json: [{ id: 'brand-a', name: 'A', enabled: true }] });
    if (path.endsWith('/brands/brand-a/channels')) return route.fulfill({ json: [{ channel_id: 'ch-1', brand_id: 'brand-a', channel_type: 'telegram', target: '@news', enabled: true, metadata: {} }] });
    if (path.endsWith('/channels/ch-1/credentials')) {
      if (request.method() === 'PUT') {
        const body = request.postDataJSON(); puts.push(body);
        state = body.secrets ? { ...state, mode: 'own', configured: ['telegram_publisher_bot_token'], warning: undefined }
                             : { ...state, mode: body.use_global_credentials ? 'global' : 'missing', use_global_credentials: body.use_global_credentials };
      }
      return route.fulfill({ json: state });
    }
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
  await activeModules(page);
  await page.goto('/brands');
  await page.getByTestId('channel-credentials-ch-1').click({ timeout: 20000 });
  await expect(page.getByTestId('channel-credentials-state-ch-1')).toContainText('публікацію заблоковано');
  await page.getByTestId('channel-global-ch-1').check();
  await expect(page.getByTestId('channel-credentials-state-ch-1')).toContainText('глобальні облікові дані');
  await page.getByTestId('channel-token-ch-1').fill('SECRET-TOKEN');
  await page.getByTestId('channel-token-save-ch-1').click();
  await expect(page.getByTestId('channel-credentials-state-ch-1')).toContainText('Власний токен каналу задано');
  expect(puts).toEqual([{ use_global_credentials: true }, { secrets: { telegram_publisher_bot_token: 'SECRET-TOKEN' } }]);
  await expect(page.getByTestId('channel-token-ch-1')).toHaveValue('');
});
