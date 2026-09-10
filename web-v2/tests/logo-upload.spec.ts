import { test, expect } from '@playwright/test';

for (const fail of [false, true]) {
  test(`Завантаження логотипа: ${fail ? 'помилка' : 'успіх'}`, async ({ page }) => {
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII=', 'base64');
    let uploaded = false;
    await page.route('**/api/**', async route => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      if (path === '/api/settings/logo') {
        if (req.method() === 'GET') {
          await route.fulfill(uploaded ? { contentType: 'image/png', body: png } : { status: 404, json: {} });
          return;
        }
        expect(req.method()).toBe('PUT');
        expect(req.headers()['content-type']).toBe('image/png');
        expect(req.postDataBuffer()).toEqual(png);
        uploaded = !fail;
        await route.fulfill(fail ? { status: 403, json: { detail: 'Завантаження заборонено' } } : { json: { saved: true } });
        return;
      }
      await route.fulfill(path.endsWith('/session')
        ? { json: { username: 'admin', role: 'admin' } }
        : { status: 503, json: { detail: 'Недоступно в тесті логотипа' } });
    });
    await page.goto('/settings');
    await page.getByTestId('logo-upload').setInputFiles({ name: 'logo.png', mimeType: 'image/png', buffer: png });
    if (fail) {
      await expect(page.getByRole('alert')).toBeVisible();
      await expect(page.getByTestId('logo-upload')).toBeEnabled();
    } else {
      const logo = page.getByRole('img', { name: 'Logo', exact: true });
      await expect(logo).toBeVisible();
      await expect.poll(() => logo.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(1);
      await page.reload();
      await expect(logo).toBeVisible();
    }
  });
}
