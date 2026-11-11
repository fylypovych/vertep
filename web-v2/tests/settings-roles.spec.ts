import { test, expect } from '@playwright/test';

const ROLES = [
  { id: 'gpu', label: 'GPU Node', runtime_status: 'READY' },
  { id: 'text', label: 'Text Node', runtime_status: 'UNKNOWN' },
];

async function open(page: any) {
  const posted: string[][] = [];
  let active = ['gpu'];
  await page.route('**/api/**', async (route: any) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path.endsWith('/system/roles') && request.method() === 'GET') {
      return route.fulfill({ json: { node_role: 'core', active_roles: active, available_roles: ROLES } });
    }
    if (path.endsWith('/system/roles') && request.method() === 'POST') {
      const roles = JSON.parse(request.postData() || '{}').roles;
      posted.push(roles);
      return route.fulfill({ json: { state: 'QUEUED', active_roles: active, requested_roles: roles, message: 'Зміни передано системному виконавцю' } });
    }
    return route.fulfill({ json: request.method() === 'GET' ? [] : {} });
  });
  await page.goto('/settings?tab=roles');
  return { posted, apply: () => { active = posted[posted.length - 1]; } };
}

test('позначка ролі одразу показує, що зміниться, а збереження тримає вибір до застосування', async ({ page }) => {
  const server = await open(page);
  const text = page.getByTestId('role-status-text');
  await expect(text).toHaveText('Локально вимкнено', { timeout: 20000 });
  await expect(page.getByTestId('roles-unsaved')).toHaveCount(0);

  await page.getByLabel('Text Node').check();
  await expect(text).toHaveText('Буде увімкнено після збереження');
  await expect(page.getByTestId('roles-unsaved')).toBeVisible();

  await page.getByTestId('roles-save-button').click();
  await expect.poll(() => server.posted).toEqual([['gpu', 'text']]);
  await expect(page.getByLabel('Text Node')).toBeChecked();
  await expect(text).toHaveText('Застосовується…');
  await expect(page.getByTestId('roles-save-message')).toHaveText('Зміни передано системному виконавцю');
  await expect(page.getByTestId('roles-unsaved')).toHaveCount(0);

  await expect(page.getByLabel('GPU Node')).toBeDisabled();
  server.apply();
  await expect(page.getByLabel('GPU Node')).toBeEnabled({ timeout: 10000 });
  await page.getByLabel('GPU Node').uncheck();
  await expect(page.getByTestId('role-status-gpu')).toHaveText('Буде вимкнено після збереження');
});

test('роль, що працює з проблемами, називає причину', async ({ page }) => {
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (path.endsWith('/system/roles')) {
      return route.fulfill({ json: {
        node_role: 'core', active_roles: [], role_runtime_status: { core: 'DEGRADED' },
        node_role_evidence: { local: { failed_checks: ['tts', 'publisher'] } },
        available_roles: [{ id: 'gpu', label: 'GPU Node', runtime_status: 'DEGRADED',
                            runtime_evidence: { nodes: [{ node_id: 'gpu-1', live: false }] } }],
      } });
    }
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await page.goto('/settings?tab=roles');
  await expect(page.getByTestId('role-status-core')).toHaveText('Працює з проблемами', { timeout: 20000 });
  await expect(page.getByTestId('role-problems-current')).toHaveText('Не пройшли перевірки: tts, publisher');
  await expect(page.getByTestId('role-problems-gpu')).toHaveText('Вузлів без зв’язку або self-test: 1');
});
