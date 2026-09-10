import { test, expect } from '@playwright/test';

test('Створення завдання показує причину відмови API та дозволяє повторити запит', async ({ page }) => {
  const detail = 'Invalid workflow: Workflow must use workflows/<task_type>/<name>.json';
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/jobs' && request.method() === 'POST') {
      await route.fulfill({ status: 400, json: { detail } });
      return;
    }
    await route.fulfill({ json: path.endsWith('/session')
      ? { username: 'admin', role: 'admin' } : [] });
  });
  await page.goto('/jobs');
  await page.getByTestId('create-job-button').click();
  await page.getByTestId('job-topic-input').fill('Тестова тема');
  const modal = page.getByTestId('create-job-modal');
  const submit = modal.getByRole('button', { name: 'Створити', exact: true });
  await submit.click();
  await expect(page.getByText(detail, { exact: true }).first()).toBeVisible();
  await expect(modal).toBeVisible();
  await expect(submit).toBeEnabled();
  await expect(page.getByTestId('job-topic-input')).toHaveValue('Тестова тема');
});
