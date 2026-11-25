import { activeModules } from './module-menu';
import { test, expect } from '@playwright/test';

async function open(page: any, role: string, path = '/jobs') {
  await page.route('**/api/**', async (route: any) => {
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: role, username: role, role } });
    if (url.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (url.endsWith('/queue')) return route.fulfill({ json: { ready: [], inflight: [], dead_letter: [] } });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await activeModules(page);
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

test('сторінки модулів є в меню й відкриваються лише поки модуль активний', async ({ page }) => {
  await page.route('**/api/**', async (route: any) => {
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (url.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  // Only Brands is READY; a future module brings its own UI into «Розширення модулів».
  await activeModules(page, [
    { module_id: 'brands', id: 'brands', label: 'Бренди', path: '/brands', group: 'content', admin_only: false },
    { module_id: 'demo', id: 'demo', label: 'Демо', path: '/modules/demo', group: 'modules', admin_only: false }]);
  await page.goto('/jobs');
  await expect(page.getByTestId('sidebar-nav').getByText('Бренди', { exact: true })).toBeVisible({ timeout: 20000 });
  const groups = await menu(page);
  expect(groups['Контент']).toEqual(['Завдання', 'Черга виконання', 'Бренди', 'Шаблони обробки']);
  expect(groups['Розширення модулів']).toEqual(['Демо']);
  await page.goto('/characters');
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId('characters-page')).toHaveCount(0);
  await page.goto('/published');
  await expect(page).toHaveURL(/\/$/);
});

test('без активного модуля «Резервні копії» пункту «Бекапи» немає, а сторінка пояснює причину', async ({ page }) => {
  await page.route('**/api/**', async (route: any) => {
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (url.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL' } } });
    if (url.endsWith('/backups')) return route.fulfill({ status: 503, json: { detail: 'Сервіс BACKUP_URL недоступний' } });
    return route.fulfill({ json: route.request().method() === 'GET' ? [] : {} });
  });
  await activeModules(page, undefined, []);
  await page.goto('/settings?tab=backup');
  await expect(page.getByTestId('backup-module-missing')).toBeVisible({ timeout: 20000 });
  await expect(page.getByText('BACKUP_URL')).toHaveCount(0);
  const groups = await menu(page);
  expect(groups['Система']).toEqual(SYSTEM.filter(item => item !== 'Бекапи'));
});
