import { test, expect } from '@playwright/test';

const OPTIONS = [{ id: 'native', label: 'Vertep Native' }, { id: 'money-printer', label: 'MoneyPrinterTurbo' }];
const MPT_FIELDS = [
  { name: 'endpoint', env: 'MONEY_PRINTER_URL', value: '', configured: false, secret: false },
  { name: 'token', env: 'MONEY_PRINTER_TOKEN', value: null, configured: false, secret: true },
];

function engine(over: Record<string, unknown> = {}) {
  return {
    selected: 'native', effective: 'native', agree: true, label: 'Vertep Native', config_revision: 'rev-native',
    endpoint: null, secret: null, fields: [], required_fields: { native: [], 'money-printer': MPT_FIELDS },
    upstream_reference: null, ready: true, reason: null, probed: false, options: OPTIONS,
    local_runtime: { supported: true, runtime: 'moneyprinter', endpoint: 'http://moneyprinter:8098', in_plan: false,
      deploying: false, available: true, reason: null, deployment_state: null, deployment_error: null },
    runtime_mode: null, system_state: 'NORMAL', change_allowed: true, values_exposed: false, ...over,
  };
}

interface State { config: any; switches: any[]; onSwitch: (body: any, call: number) => { status: number; json: any } }

function mockApi(page: any, state: State) {
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/settings/video-engine')) return route.fulfill({ json: state.config });
    if (path.endsWith('/settings/providers/video_engine')) {
      const body = JSON.parse(request.postData() || '{}');
      state.switches.push(body);
      const answer = state.onSwitch(body, state.switches.length);
      return route.fulfill({ status: answer.status, json: answer.json });
    }
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
}

test('Native: готовність стосується лише складання, режим NORMAL не видається за готовність генераторів', async ({ page }) => {
  await mockApi(page, { config: engine(), switches: [], onSwitch: () => ({ status: 200, json: {} }) });
  await page.goto('/settings?tab=video-engine');
  const panel = page.getByTestId('video-engine-panel');
  await expect(panel).toContainText('Готовність движка складання:', { timeout: 20000 });
  await expect(page.getByTestId('video-engine-scope')).toContainText('не потребує GPU');
  await expect(page.getByTestId('video-engine-scope')).toContainText('NORMAL не означає готовність усіх генераторів');
  await expect(panel).toContainText('Режим системи:');
  await expect(page.getByTestId('video-engine-select').locator('option')).toHaveCount(2);
});

test('локальний MoneyPrinterTurbo: без URL/token, розгортання через executor і автоматичне завершення', async ({ page }) => {
  const state: State = { config: engine(), switches: [], onSwitch: (body, call) => {
    if (call === 1) {
      state.config = engine({ local_runtime: { ...engine().local_runtime, deploying: true, deployment_state: 'APPLYING' } });
      return { status: 202, json: { state: 'RUNTIME_DEPLOYING', local_runtime: state.config.local_runtime } };
    }
    state.config = engine({ selected: 'money-printer', effective: 'money-printer', label: 'MoneyPrinterTurbo', runtime_mode: 'local',
      local_runtime: { ...engine().local_runtime, in_plan: true, deployment_state: 'SUCCEEDED' } });
    return { status: 200, json: { slot: 'video_engine', backend: 'money-printer', changed: true, effective_engine: state.config } };
  } };
  await mockApi(page, state);
  await page.goto('/settings?tab=video-engine');
  await page.getByTestId('video-engine-select').selectOption('money-printer', { timeout: 20000 });
  // The form choice is a draft until applied; the applied fields keep Native.
  await expect(page.getByTestId('video-engine-draft')).toContainText('MoneyPrinterTurbo');
  await expect(page.getByTestId('video-engine-effective')).toHaveText('Vertep Native');
  await expect(page.getByTestId('video-engine-runtime-local')).toBeChecked();
  await expect(page.getByTestId('video-engine-endpoint')).toHaveCount(0);
  await expect(page.getByTestId('video-engine-token-state')).toHaveCount(0);
  await page.getByTestId('video-engine-apply').click();
  await expect(page.getByTestId('video-engine-deploying')).toBeVisible();
  // Once the executor finished, the switch is completed with no input from the operator.
  setTimeout(() => { state.config = engine({ local_runtime: { ...engine().local_runtime, in_plan: true, deployment_state: 'SUCCEEDED' } }); }, 1000);
  await expect(page.getByTestId('video-engine-effective')).toHaveText('MoneyPrinterTurbo', { timeout: 15000 });
  expect(state.switches).toEqual([{ backend: 'money-printer', runtime: 'local' }, { backend: 'money-printer', runtime: 'local' }]);
  await expect(page.getByTestId('video-engine-draft')).toHaveCount(0);
});

test('невдале розгортання runtime показує причину й зберігає попередній движок', async ({ page }) => {
  const state: State = { config: engine(), switches: [], onSwitch: () => {
    state.config = engine({ local_runtime: { ...engine().local_runtime, deploying: true, deployment_state: 'QUEUED' } });
    return { status: 202, json: { state: 'RUNTIME_DEPLOYING' } };
  } };
  await mockApi(page, state);
  await page.goto('/settings?tab=video-engine');
  await page.getByTestId('video-engine-select').selectOption('money-printer', { timeout: 20000 });
  await page.getByTestId('video-engine-apply').click();
  await expect(page.getByTestId('video-engine-deploying')).toBeVisible();
  state.config = engine({ local_runtime: { ...engine().local_runtime, deployment_state: 'FAILED', deployment_error: 'pull moneyprinter failed' } });
  await expect(page.getByTestId('video-engine-error')).toContainText('pull moneyprinter failed', { timeout: 15000 });
  await expect(page.getByTestId('video-engine-rollback')).toContainText('Попередній движок збережено');
  await expect(page.getByTestId('video-engine-effective')).toHaveText('Vertep Native');
  expect(state.switches.length).toBe(1);
});

test('зовнішній runtime лишається з адресою й станом токена; недоступний локальний runtime пояснено; англійська', async ({ page }) => {
  const state: State = { config: engine({ local_runtime: { ...engine().local_runtime, available: false, reason: 'architecture_unsupported:aarch64' } }),
    switches: [], onSwitch: () => ({ status: 200, json: {} }) };
  await mockApi(page, state);
  await page.goto('/settings?tab=video-engine');
  await page.getByTestId('video-engine-select').selectOption('money-printer', { timeout: 20000 });
  await expect(page.getByTestId('video-engine-local-unavailable')).toBeVisible();
  await expect(page.getByTestId('video-engine-runtime-local')).toBeDisabled();
  await page.getByTestId('video-engine-runtime-external').check();
  await expect(page.getByTestId('video-engine-endpoint')).toBeVisible();
  await expect(page.getByTestId('video-engine-token-state')).toContainText('MONEY_PRINTER_TOKEN');
  await page.evaluate(() => localStorage.setItem('vertep_language', 'en'));
  await page.reload();
  await expect(page.getByTestId('video-engine-panel')).toContainText('Assembly engine readiness:', { timeout: 20000 });
  await expect(page.getByTestId('video-engine-scope')).toContainText('needs no GPU');
});

test('розгортання, почате до reload, відстежується далі', async ({ page }) => {
  const state: State = { config: engine({ local_runtime: { ...engine().local_runtime, deploying: true, deployment_state: 'APPLYING' } }),
    switches: [], onSwitch: () => {
      state.config = engine({ selected: 'money-printer', effective: 'money-printer', runtime_mode: 'local',
        local_runtime: { ...engine().local_runtime, in_plan: true, deployment_state: 'SUCCEEDED' } });
      return { status: 200, json: { slot: 'video_engine', backend: 'money-printer', changed: true, effective_engine: state.config } };
    } };
  await mockApi(page, state);
  await page.goto('/settings?tab=video-engine');
  await expect(page.getByTestId('video-engine-deploying')).toBeVisible({ timeout: 20000 });
  state.config = engine({ local_runtime: { ...engine().local_runtime, in_plan: true, deployment_state: 'SUCCEEDED' } });
  await expect(page.getByTestId('video-engine-effective')).toHaveText('MoneyPrinterTurbo', { timeout: 15000 });
  expect(state.switches).toEqual([{ backend: 'money-printer', runtime: 'local' }]);
});
