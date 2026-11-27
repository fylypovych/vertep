import { test, expect } from '@playwright/test';

async function open(page: any, frame: { url: string | null; panel?: string }, path = '/lab') {
  await page.route('https://lab.example:8444/**', (route: any) => route.fulfill({ body: '<p>LAB</p>', contentType: 'text/html' }));
  await page.route('**/api/**', (route: any) => {
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith('/session')) return route.fulfill({ json: { authenticated: true, user: 'admin', username: 'admin', role: 'admin' } });
    if (url.endsWith('/lab/frame')) return route.fulfill({ json: frame });
    return route.fulfill({ json: {} });
  });
  await page.goto(path);
}

for (const path of ['/lab', '/v3/lab']) {
  test(`«Лабораторія» (${path}) відкриває панель LAB у рамці Vertep з одноразовим входом`, async ({ page }) => {
    await open(page, { url: 'https://lab.example:8444/#vertep_identity=abc.def', panel: 'https://lab.example:8444' }, path);
    const frame = page.getByTestId('lab-frame');
    await expect(frame).toHaveAttribute('src', 'https://lab.example:8444/#vertep_identity=abc.def');
    await expect(page.getByTestId('lab-open-separately'))
      .toHaveAttribute('href', '/api/lab/login?return_to=' + encodeURIComponent('https://lab.example:8444/'));
    await expect(page).toHaveURL(new RegExp(path.replace('/', '\\/') + '$'));  // Vertep stays, no redirect away
  });
}

test('без підключеної лабораторії сторінка пояснює, де її налаштувати, і нічого не вбудовує', async ({ page }) => {
  await open(page, { url: null });
  await expect(page.getByTestId('lab-entry-missing')).toBeVisible();
  await expect(page.getByTestId('lab-frame')).toHaveCount(0);
});
