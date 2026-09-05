import { test, expect } from '@playwright/test';

test.describe('Web V2 Admin', () => {
  test('homepage loads', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('body')).toContainText('Vertep');
  });

  test('login page is accessible', async ({ page }) => {
    await page.goto('/login');
    await expect(page.locator('h2')).toContainText('Вхід');
  });
});
