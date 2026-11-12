import { test, expect } from '@playwright/test';

const CATALOG = { source: 'https://registry.ollama.ai/v2/library', verified_at: '2026-11-12', models: [
  { name: 'llama3.2:3b', family: 'Llama 3.2', parameters: '3B', download_bytes: 2019393189 },
  { name: 'qwen2.5:7b', family: 'Qwen 2.5', parameters: '7B', download_bytes: 4683087332 },
] };
const NODE = { node_name: 'text-1', role: 'text', status: 'READY', models: ['qwen2.5:7b'], model_count: 1, stale: false, ready: true, pending_command: false };

interface State {
  modelsFail: number;
  catalogFail: number;
  installed: string[];
  pulls: any[];
  operation: (call: number) => any;
}

function mockApi(page: any, state: State) {
  let polls = 0;
  return page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/system/models/catalog')) {
      if (state.catalogFail > 0) { state.catalogFail--; return route.fulfill({ status: 503, json: { detail: 'Model catalog is unavailable', code: 'model_catalog_unavailable' } }); }
      return route.fulfill({ json: CATALOG });
    }
    if (path.endsWith('/system/models/nodes')) return route.fulfill({ json: { nodes: [NODE] } });
    if (path.endsWith('/system/models/pull') && method === 'POST') {
      const body = JSON.parse(request.postData() || '{}');
      state.pulls.push(body);
      polls = 0;
      return route.fulfill({ json: { operation_id: 'op-1', status: 'QUEUED', progress: 0, current_phase: 'queued', model: body.name, error: null, result: null } });
    }
    if (path.endsWith('/operations/op-1')) return route.fulfill({ json: state.operation(++polls) });
    if (path.endsWith('/system/models') && method === 'GET') {
      if (state.modelsFail > 0) { state.modelsFail--; return route.fulfill({ status: 500, json: { detail: 'Ollama list failed' } }); }
      return route.fulfill({ json: { models: state.installed.map(name => ({ name })), nodes: [NODE] } });
    }
    if (path.endsWith('/models/voices')) return route.fulfill({ json: { voices: [] } });
    return route.fulfill({ json: method === 'GET' ? [] : {} });
  });
}

const running = (model: string) => ({ operation_id: 'op-1', status: 'RUNNING', progress: 40, current_phase: 'pulling', model, error: null, result: null });

test('помилка списку не ховає форму, «Повторити» відновлює список; каталог окремо від встановлених', async ({ page }) => {
  // Dependencies stay down until the test restores them, independent of how many requests the page makes.
  const state: State = { modelsFail: Infinity, catalogFail: Infinity, installed: ['llama3.2:3b'], pulls: [], operation: () => running('') };
  await mockApi(page, state);
  await page.goto('/settings?tab=models');
  const installed = page.getByTestId('installed-models');
  await expect(installed.getByTestId('error-state')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('pull-model-input')).toBeVisible();
  await expect(page.getByTestId('catalog-error')).toContainText('Каталог моделей недоступний');
  state.modelsFail = 0;
  await installed.getByRole('button', { name: 'Повторити' }).click();
  await expect(page.getByTestId('model-row-llama3.2:3b')).toBeVisible();
  await expect(installed.getByTestId('error-state')).toHaveCount(0);
  state.catalogFail = 0;
  await page.getByTestId('catalog-retry').click();
  await expect(page.getByTestId('catalog-error')).toHaveCount(0);
  const options = page.getByTestId('pull-catalog-select').locator('option');
  await expect(options).toHaveCount(3);
  // Installed state follows the chosen target: CORE has llama3.2, text-1 has qwen2.5.
  await expect(options.nth(1)).toContainText('llama3.2:3b — Llama 3.2 3B, 2.0 ГБ · встановлено');
  await expect(options.nth(2)).not.toContainText('встановлено');
  await page.getByTestId('pull-node-select').selectOption('text-1');
  await expect(options.nth(2)).toContainText('встановлено');
  await expect(options.nth(1)).not.toContainText('встановлено');
});

test('вибір з каталогу передає назву й вузол у pull; прогрес і успіх за фактичним станом', async ({ page }) => {
  const state: State = { modelsFail: 0, catalogFail: 0, installed: [], pulls: [],
    operation: (call) => call < 2 ? running('qwen2.5:7b')
      : { operation_id: 'op-1', status: 'COMPLETED', progress: 100, current_phase: 'success', model: 'qwen2.5:7b', error: null, result: { pulled: true } } };
  await mockApi(page, state);
  await page.goto('/settings?tab=models');
  await expect(page.getByTestId('installed-empty')).toBeVisible({ timeout: 20000 });
  await page.getByTestId('pull-node-select').selectOption('text-1');
  await page.getByTestId('pull-catalog-select').selectOption('qwen2.5:7b');
  await expect(page.getByTestId('pull-model-input')).toHaveValue('qwen2.5:7b');
  await page.getByTestId('pull-model-button').click();
  await expect(page.getByTestId('pull-status')).toContainText('Завантажується');
  await expect(page.getByTestId('pull-progress')).toContainText('40%');
  expect(state.pulls).toEqual([{ name: 'qwen2.5:7b', node: 'text-1' }]);
  state.installed = ['qwen2.5:7b'];
  await expect(page.getByTestId('pull-status')).toContainText('Модель завантажено', { timeout: 10000 });
  await expect(page.getByTestId('model-row-qwen2.5:7b')).toBeVisible();
});

test('неправильна назва лишається у формі для виправлення; відсутній тег не видається за встановлену модель', async ({ page }) => {
  const state: State = { modelsFail: 0, catalogFail: 0, installed: [], pulls: [],
    operation: () => ({ operation_id: 'op-1', status: 'FAILED', progress: 0, current_phase: 'failed', model: 'llama3.2:999b',
      error: 'pull model manifest: file does not exist', result: null }) };
  await mockApi(page, state);
  await page.goto('/settings?tab=models');
  const input = page.getByTestId('pull-model-input');
  await expect(input).toBeVisible({ timeout: 20000 });
  await input.fill('ollama pull llama3.2');
  await page.getByTestId('pull-model-button').click();
  await expect(page.getByTestId('pull-error')).toContainText('Неправильна назва моделі');
  await expect(page.getByTestId('pull-retry')).toHaveCount(0);
  expect(state.pulls).toEqual([]);
  await expect(input).toHaveValue('ollama pull llama3.2');
  // Surrounding spaces are trimmed; a syntactically valid but unknown tag fails at Ollama.
  await input.fill('  llama3.2:999b ');
  await expect(page.getByTestId('pull-error')).toHaveCount(0);
  await page.getByTestId('pull-model-button').click();
  await expect(page.getByTestId('pull-error')).toContainText('Модель або тег не знайдено', { timeout: 10000 });
  await expect(page.getByTestId('pull-status')).toContainText('Завантаження не вдалося');
  expect(state.pulls).toEqual([{ name: 'llama3.2:999b' }]);
  await expect(input).toHaveValue('llama3.2:999b');
  await expect(page.getByTestId('model-row-llama3.2:999b')).toHaveCount(0);
});

test('недоступна Ollama: «Повторити завантаження» повторює pull; англійська локаль', async ({ page }) => {
  const state: State = { modelsFail: 0, catalogFail: 0, installed: [], pulls: [],
    operation: () => ({ operation_id: 'op-1', status: 'FAILED', progress: 0, current_phase: 'failed', model: 'llama3.2:3b',
      error: 'pull failed: [Errno 111] Connection refused', result: null }) };
  await mockApi(page, state);
  await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
  await page.goto('/settings?tab=models');
  await page.getByTestId('pull-catalog-select').selectOption('llama3.2:3b', { timeout: 20000 });
  await page.getByTestId('pull-model-button').click();
  await expect(page.getByTestId('pull-error')).toContainText('Ollama is unavailable on the selected node', { timeout: 10000 });
  await page.getByTestId('pull-retry').click();
  await expect.poll(() => state.pulls.length).toBe(2);
  expect(state.pulls[1]).toEqual({ name: 'llama3.2:3b' });
  await expect(page.getByTestId('model-pull')).toContainText('Download model');
});
