import { test, expect } from '@playwright/test';

const ROUTES = ['/', '/jobs', '/characters', '/brands', '/workflows', '/workers', '/alerts', '/logs', '/published', '/real-tests',
  '/health', '/profile', '/settings', '/settings?tab=backup', '/settings?tab=update', '/settings?tab=secrets', '/settings?tab=publishing',
  '/settings?tab=workflow-mode', '/jobs/j1', '/setup?token=x', '/login'];

for (const route of ROUTES) {
  test(`англійською без українського тексту й обрізання: ${route}`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('vertep_language', 'en'));
    await page.route('**/api/**', async (r: any) => {
      const path = new URL(r.request().url()).pathname;
      if (path.endsWith('/session')) return r.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
      if (path.endsWith('/status')) return r.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
      if (path.endsWith('/jobs/j1')) return r.fulfill({ json: { job_id: 'j1', topic: 'Topic', status: 'FAILED', character_id: 'c', events: [], event_log: [], scenes: [], artifacts: [], published_to: [], publication_results: {}, stages: {} } });
      if (path.endsWith('/api/setup')) return r.fulfill({ json: { configured: false, selected_role: null, hardware: {}, backends: [], roles: { core: { label: 'Core', modules: [], capabilities: [] } } } });
      return r.fulfill({ json: [] });
    });
    await page.goto(route);
    await page.waitForLoadState('networkidle').catch(() => {});
    const leftover = await page.evaluate(() => {
      const found: string[] = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode.textContent?.trim() || '';
        if (/[А-Яа-яЄєІіЇї]/.test(text) && text !== 'УК') found.push(text);
      }
      return found;
    });
    expect(leftover).toEqual([]);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
