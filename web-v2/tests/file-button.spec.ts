import { test, expect } from '@playwright/test';

test('кнопка вибору файла має вигляд кнопки', async ({ page }) => {
  await page.goto('/login');
  await page.evaluate(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.id = 'probe';
    document.body.append(input);
  });
  const style = await page.locator('#probe').evaluate(element => {
    const button = getComputedStyle(element, '::file-selector-button');
    return { background: button.backgroundColor, radius: button.borderTopLeftRadius, border: button.borderTopWidth };
  });
  expect(style.background).not.toBe('rgba(0, 0, 0, 0)');
  expect(style.radius).not.toBe('0px');
  expect(style.border).toBe('1px');
});
