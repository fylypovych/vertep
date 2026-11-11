import { test, expect } from '@playwright/test';

async function open(page: any, role: string) {
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: role, username: role, role } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await page.goto('/jobs');
}

const groups = (page: any) => page.locator('nav').first().evaluate((nav: HTMLElement) => {
  const result: Record<string, string[]> = {};
  let current = '';
  for (const node of Array.from(nav.querySelectorAll('[data-testid^="nav-group-"], a'))) {
    const element = node as HTMLElement;
    if (element.dataset['testid']?.startsWith('nav-group-')) { current = (element.textContent || '').trim(); result[current] = []; }
    else if (current) result[current].push((element.textContent || '').trim());
  }
  return result;
});

test('меню адміністратора: Контент, Система й Налаштування без повторів назв', async ({ page }) => {
  await open(page, 'admin');
  await expect(page.locator('nav').first().getByText('Налаштування', { exact: true })).toBeVisible({ timeout: 20000 });
  const menu = await groups(page);
  expect(Object.keys(menu)).toEqual(['Контент', 'Система', 'Налаштування']);
  expect(menu['Система']).toEqual(['Вузли', 'Стан системи', 'Стан підключень', 'Алерти', 'Логи', 'Оновлення', 'Бекапи', 'Ролі', 'Реальні тести']);
  expect(menu['Налаштування']).toEqual(['Загальне', 'Публікація', 'Telegram-бот', 'Моделі', 'Движок відео', 'Режим workflow', 'Брендинг', 'Ключі інтеграцій', 'Безпека']);
  const labels = Object.values(menu).flat();
  expect(new Set(labels).size).toBe(labels.length);
  expect(labels.filter(label => Object.keys(menu).includes(label))).toEqual([]);
});

test('нові пункти меню відкривають свої сторінки', async ({ page }) => {
  await open(page, 'admin');
  const nav = page.locator('nav').first();
  await nav.getByText('Стан системи', { exact: true }).click({ timeout: 20000 });
  await expect(page).toHaveURL(/\/health$/);
  await nav.getByText('Логи', { exact: true }).click();
  await expect(page).toHaveURL(/\/logs$/);
  await nav.getByText('Движок відео', { exact: true }).click();
  await expect(page).toHaveURL(/\/settings\?tab=video-engine$/);
  await nav.getByText('Режим workflow', { exact: true }).click();
  await expect(page).toHaveURL(/\/settings\?tab=workflow-mode$/);
});

test('переглядач не бачить групи Налаштування', async ({ page }) => {
  await open(page, 'viewer');
  const nav = page.locator('nav').first();
  await expect(nav.getByText('Вузли', { exact: true })).toBeVisible({ timeout: 20000 });
  await expect(nav.getByText('Налаштування', { exact: true })).toHaveCount(0);
});
