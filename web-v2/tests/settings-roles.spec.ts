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
  await expect(text).toHaveText('Не вибрано', { timeout: 20000 });
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

  await page.getByLabel('GPU Node').uncheck();
  await expect(page.getByTestId('role-status-gpu')).toHaveText('Буде вимкнено після збереження');
});
