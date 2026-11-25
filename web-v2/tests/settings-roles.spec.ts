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

test('«Розмістити тут» одразу зберігає вузол, після reload він розміщений, а активація чекає на вузол', async ({ page }) => {
  const placements = await openModules(page);
  await expect(page.getByTestId('activate-blocker-image')).toHaveText('Спершу розмістіть модуль на вузлі');
  await expect(page.getByTestId('activate-image')).toBeDisabled();
  await page.getByTestId('place-image-gpu').click();
  await expect.poll(() => placements).toEqual([['gpu']]);
  await page.reload();
  await expect(page.getByTestId('placed-image-gpu')).toHaveText('Розміщено');
  await expect(page.getByTestId('waiting-node-image')).toBeVisible();
  await expect(page.getByTestId('activate-blocker-image')).toHaveText('Чекаємо на запуск модуля на вузлі');
  await expect(page.getByTestId('ready-image-gpu')).toHaveText('Вузол не повідомляв модуль');
  await page.getByTestId('unplace-image-gpu').click();
  await expect.poll(() => placements).toEqual([['gpu'], []]);
});

test('несумісний вузол показує причину й не приймає модуль', async ({ page }) => {
  await openModules(page);
  await expect(page.getByTestId('place-image-cpu')).toBeDisabled();
  await expect(page.getByTestId('incompatible-image-cpu')).toHaveText('Потрібен GPU');
  await expect(page.getByTestId('ready-image-cpu')).toContainText('Самоперевірка не пройдена');
});
