import { test, expect } from '@playwright/test';

for (const variant of ['scenes', 'missing', 'invalid']) {
  test(`Перегляд сценарію: ${variant}`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown = [];
      if (path.endsWith('/session')) body = { username: 'admin', role: 'admin' };
      else if (path.endsWith('/status')) body = { system: { state: 'NORMAL' } };
      else if (path.endsWith('/jobs/script-test')) body = {
        job_id: 'script-test', topic: 'Тест', status: 'SCRIPT_PENDING_APPROVAL',
        scenes: [], events: [], storyboards: [], published_to: [], publication_results: {},
        script: { title: 'Тестовий сценарій', description: 'Опис',
          ...(variant === 'missing' ? {} : { scenes: variant === 'invalid' ? {}
            : [{ prompt: 'Кадр біля річки', voiceover: 'Ранок у селі' }, null] }),
        },
      };
      await route.fulfill({ json: body });
    });
    await page.goto('/jobs/script-test');
    const preview = page.getByTestId('job-script');
    await expect(preview).toContainText('Тестовий сценарій');
    if (variant === 'scenes') {
      await expect(preview).toContainText('Кадр біля річки');
      await expect(preview).toContainText('Ранок у селі');
    } else await expect(preview).not.toContainText('Сцена 1');
    expect(errors).toEqual([]);
  });
}
