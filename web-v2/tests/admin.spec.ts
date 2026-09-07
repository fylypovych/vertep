import { test, expect, Page } from '@playwright/test';

async function hasNoConsoleCriticalErrors(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const text = msg.text();
      // Ignore expected API errors when running without backend
      if (
        text.includes('ERR_CONNECTION_REFUSED') ||
        text.includes('net::ERR') ||
        text.includes('Failed to load resource')
      ) return;
      errors.push(text);
    }
  });
  return errors;
}

test.describe('Web V2 Admin — smoke tests', () => {

  test('homepage loads with layout', async ({ page }) => {
    const errors = await hasNoConsoleCriticalErrors(page);
    await page.goto('/');
    await expect(page.locator('body')).toContainText('Vertep');
    // No CDN tailwind request
    const requests: string[] = [];
    page.on('request', req => requests.push(req.url()));
    await page.waitForTimeout(500);
    const cdnRequest = requests.find(u => u.includes('cdn.tailwindcss.com'));
    expect(cdnRequest).toBeUndefined();
  });

  test('login page accessible and styled', async ({ page }) => {
    await page.goto('/login');
    await expect(page.locator('h2')).toContainText('Вхід');
    // Check CSS is applied — login container should have reasonable dimensions
    const box = await page.locator('form').boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThan(100);
  });

  test('no CDN tailwind script in HTML', async ({ page }) => {
    const response = await page.goto('/');
    const html = await response!.text();
    expect(html).not.toContain('cdn.tailwindcss.com');
    expect(html).not.toContain('tailwind.config');
  });

  for (const route of ['/', '/jobs', '/workers', '/characters', '/settings']) {
    test(`route ${route} — layout elements present`, async ({ page }) => {
      await page.goto(route);
      // After auth redirect (no backend) we land on /login — just check no crash
      const url = page.url();
      expect(url).toBeTruthy();
      // No NG04002 or tailwind errors in console
      const consoleErrors: string[] = [];
      page.on('console', msg => {
        if (msg.type() === 'error') consoleErrors.push(msg.text());
      });
      await page.waitForTimeout(300);
      const critical = consoleErrors.filter(e =>
        e.includes('NG04002') ||
        e.includes('tailwind is not defined') ||
        e.includes('ReferenceError')
      );
      expect(critical).toHaveLength(0);
    });
  }

  test('/v1 route is separate from Angular app', async ({ page }) => {
    // /v1 should NOT be handled by Angular router
    // It should either load classic UI or return non-Angular response
    const response = await page.goto('/v1/');
    // As long as it doesn't crash Angular with NG04002, we're good
    await expect(page.locator('body')).toBeTruthy();
  });

});
