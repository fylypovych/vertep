import { test, expect } from '@playwright/test';

async function openModules(page: import('@playwright/test').Page) {
  const placements: string[][] = [];
  let placement: string[] = [];
  await page.route('**/api/**', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/nodes/module-registry/placement/image')) {
      placement = request.postDataJSON().node_ids;
      placements.push(placement);
      return route.fulfill({ json: { node_ids: placement } });
    }
    if (path.endsWith('/nodes/module-catalog')) return route.fulfill({ json: {
      modules: [{ module_id: 'image', label: 'GPU Node', version: '1.0.0', bundled: true,
        state: 'INSTALLED', requires_gpu: true, placement, permissions: [],
        nodes: [
          { node_id: 'gpu', compatible_reasons: [], runtime_missing: false, instance: null },
          { node_id: 'cpu', compatible_reasons: ['Потрібен GPU'], runtime_missing: false,
            instance: { state: 'NOT_READY', reasons: ['self_test_not_passed'], version: '1.0.0' } },
        ],
      }],
      nodes: [{ node_id: 'gpu', role: 'gpu' }, { node_id: 'cpu', role: 'core' }],
    } });
    if (path.endsWith('/system/roles')) return route.fulfill({ json: { active_roles: [], queued: false, deployment: null } });
    return route.fulfill({ json: {} });
  });
  await page.goto('/modules');
  return placements;
}

test('розміщення модуля зберігає вибраний вузол після reload', async ({ page }) => {
  const placements = await openModules(page);
  const choice = page.getByTestId('place-image-gpu');
  const save = page.getByTestId('save-placement-image');
  await expect(save).toBeDisabled();
  await choice.check();
  await save.click();
  await expect.poll(() => placements).toEqual([['gpu']]);
  await page.reload();
  await expect(choice).toBeChecked();
  await expect(save).toBeDisabled();
  await expect(page.getByTestId('ready-image-gpu')).toHaveText('Вузол не повідомляв модуль');
});

test('несумісний вузол показує причину й не приймає модуль', async ({ page }) => {
  await openModules(page);
  await expect(page.getByTestId('place-image-cpu')).toBeDisabled();
  await expect(page.getByTestId('incompatible-image-cpu')).toHaveText('Потрібен GPU');
  await expect(page.getByTestId('ready-image-cpu')).toContainText('Самоперевірка не пройдена');
});
