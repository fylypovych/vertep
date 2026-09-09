import { test, expect } from '@playwright/test';

for (const action of ['delete-list', 'delete-detail', 'reject', 'reject-storyboard'] as const) {
  test(`Підтвердження дії ${action}`, async ({ page }) => {
    let job = {
      job_id: 'confirmation-test', topic: 'Тестове завдання',
      status: action === 'reject-storyboard' ? 'STORYBOARD_PENDING_APPROVAL' : action === 'reject' ? 'PENDING_APPROVAL' : 'NEW',
      active_storyboard_version: action === 'reject-storyboard' ? 1 : null,
      created_at: '2026-09-09T10:00:00Z',
      scenes: [], events: [], storyboards: [], published_to: [], publication_results: {},
    };
    let deleted = false;
    const mutations: string[] = [];
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      let body: unknown = [];
      if (path.endsWith('/session')) body = { username: 'admin', role: 'admin' };
      else if (path.endsWith('/status')) body = { system: { state: 'NORMAL' } };
      else if (path.endsWith('/jobs/confirmation-test/storyboards/reject')) {
        expect(request.postDataJSON()).toEqual({ version: 1, actor: 'web-v2' });
        mutations.push('reject-storyboard');
        job = { ...job, status: 'STORYBOARD_REJECTED' };
        body = job;
      }
      else if (path.endsWith('/jobs/confirmation-test/cancel')) {
        mutations.push('cancel');
        job = { ...job, status: 'CANCELLED' };
        body = job;
      } else if (path.endsWith('/jobs/confirmation-test')) {
        if (request.method() === 'DELETE') {
          mutations.push('delete');
          deleted = true;
          body = { deleted: job.job_id };
        } else body = job;
      } else if (path.endsWith('/jobs')) body = deleted ? [] : [job];
      await route.fulfill({ json: body });
    });

    await page.goto(action === 'delete-list' ? '/jobs' : '/jobs/confirmation-test');
    const button = action === 'delete-list'
      ? page.getByRole('button', { name: 'Видалити', exact: true })
      : page.getByTestId(action.startsWith('reject') ? 'reject-job-button' : 'delete-job-button');
    await button.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    expect(mutations).toEqual([]);
    await dialog.getByRole('button', { name: 'Скасувати', exact: true }).click();
    await expect(dialog).toBeHidden();
    expect(mutations).toEqual([]);

    await button.click();
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: 'Підтвердити', exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect.poll(() => mutations).toEqual([action === 'reject' ? 'cancel' : action === 'reject-storyboard' ? 'reject-storyboard' : 'delete']);
    if (action.startsWith('delete')) {
      await expect(page).toHaveURL(/\/jobs$/);
      await expect(page.getByTestId('jobs-empty')).toBeVisible();
    }
  });
}
