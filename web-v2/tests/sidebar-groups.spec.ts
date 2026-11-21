import { test, expect } from '@playwright/test';

async function open(page: any, role: string, path = '/jobs') {
  await page.route('**/api/**', async (route: any) => {
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: role, username: role, role } });
    if (url.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (url.endsWith('/queue')) return route.fulfill({ json: { ready: [], inflight: [], dead_letter: [] } });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await page.goto(path);
}

/** Menu as rendered: items before the first group title, then each group with its items. */
const menu = (page: any) => page.getByTestId('sidebar-nav').evaluate((nav: HTMLElement) => {
  const result: Record<string, string[]> = { '': [] };
  let current = '';
  for (const node of Array.from(nav.querySelectorAll('[data-testid^="nav-group-"], a'))) {
    const element = node as HTMLElement;
    if (element.dataset['testid']?.startsWith('nav-group-')) { current = (element.textContent || '').trim(); result[current] = []; }
    else result[current].push((element.textContent || '').trim());
  }
  return result;
});

const CONTENT = ['Завдання', 'Черга виконання', 'Опубліковане', 'Персонажі', 'Бренди', 'Шаблони обробки'];
const SYSTEM = ['Стан системи', 'Алерти', 'Логи', 'Стан підключень', 'Вузли', 'Модулі', 'Оновлення', 'Бекапи', 'Реальні тести'];
const SETTINGS = ['Загальне', 'Публікація', 'Telegram-бот', 'Моделі', 'Движок відео', 'Режим workflow', 'Брендинг',
  'Ключі інтеграцій', 'Безпека', 'Користувачі'];

test('#518: Дашборд першим, далі Контент, Система й Налаштування у погодженому порядку', async ({ page }) => {
  await open(page, 'admin');
  await expect(page.getByTestId('sidebar-nav').getByText('Налаштування', { exact: true })).toBeVisible({ timeout: 20000 });
  const groups = await menu(page);
  expect(Object.keys(groups)).toEqual(['', 'Контент', 'Система', 'Налаштування']);
  expect(groups['']).toEqual(['Дашборд']);
  expect(groups['Контент']).toEqual(CONTENT);
  expect(groups['Система']).toEqual(SYSTEM);
  expect(groups['Налаштування']).toEqual(SETTINGS);
  const settings = groups['Налаштування'];
  expect(settings.indexOf('Движок відео') - settings.indexOf('Моделі')).toBe(1);
  expect(settings.indexOf('Режим workflow') - settings.indexOf('Движок відео')).toBe(1);
  expect(settings.indexOf('Користувачі') - settings.indexOf('Безпека')).toBe(1);
  const labels = Object.values(groups).flat();
  expect(new Set(labels).size).toBe(labels.length);
});

test('#518: назви в меню збігаються із заголовками сторінок, перехід за чинними маршрутами й підсвічування', async ({ page }) => {
  await open(page, 'admin', '/');
  const nav = page.getByTestId('sidebar-nav');
  const cases: [string, RegExp][] = [
    ['Черга виконання', /\/queue$/], ['Шаблони обробки', /\/workflows$/], ['Дашборд', /\/$/],
    ['Стан системи', /\/health$/], ['Модулі', /\/modules$/], ['Реальні тести', /\/real-tests$/]];
  for (const [label, url] of cases) {
    const item = nav.getByText(label, { exact: true });
    await item.click({ timeout: 20000 });
    await expect(page).toHaveURL(url);
    await expect(item.locator('..')).toHaveAttribute('aria-current', 'page');
    if (label === 'Черга виконання' || label === 'Шаблони обробки') {
      await expect(page.getByTestId('header-title')).toHaveText(label);
    }
  }
  await nav.getByText('Оновлення', { exact: true }).click();
  await expect(page).toHaveURL(/\/settings\?tab=update$/);
  await nav.getByText('Режим workflow', { exact: true }).click();
  await expect(page).toHaveURL(/\/settings\?tab=workflow-mode$/);
});

test('#518: модератор бачить лише свої пункти в тому самому порядку', async ({ page }) => {
  await open(page, 'moderator');
  const nav = page.getByTestId('sidebar-nav');
  await expect(nav.getByText('Вузли', { exact: true })).toBeVisible({ timeout: 20000 });
  const groups = await menu(page);
  expect(Object.keys(groups)).toEqual(['', 'Контент', 'Система']);
  expect(groups['']).toEqual(['Дашборд']);
  expect(groups['Контент']).toEqual(CONTENT);
  expect(groups['Система']).toEqual(['Стан системи', 'Алерти', 'Логи', 'Вузли']);
});
