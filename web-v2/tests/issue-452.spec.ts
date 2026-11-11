import { test, expect } from '@playwright/test';

const SHA_A = 'a'.repeat(64);
const SHA_B = 'b'.repeat(64);
const SHA_C = 'c'.repeat(64);

test('SHA-256 у картці сертифікатів не виходять за межі й не створюють горизонтального overflow', async ({ page }) => {
  await page.route('**/api/**', async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
    if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
    if (path.endsWith('/security/check')) return route.fulfill({ json: {
      ok: true,
      weak_or_missing: [],
      recommendation: '',
      checks: {
        secrets_store: { status: 'ok', detail: '' },
        certificates: {
          server_certificate: { status: 'ok', sha256: SHA_A },
          server_key: { status: 'ok', sha256: SHA_B },
          node_ca: { status: 'ok', sha256: SHA_C },
        },
        integrations: [],
        effective_config: {},
      },
    } });
    if (path.endsWith('/system/certificates')) return route.fulfill({ json: { status: 'HEALTHY', subject: 'CN=vertep', issuer: 'CN=vertep', expires_at: '2027-01-31T10:00:00Z', pem: 'X' } });
    return route.fulfill({ json: {} });
  });

  await page.goto('/settings?tab=security');
  await expect(page.getByTestId('security-cert-server_certificate')).toBeVisible({ timeout: 20000 });

  // повні 64-символьні відбитки збережені
  const body = await page.content();
  expect(body).toContain(SHA_A);
  expect(body).toContain(SHA_B);
  expect(body).toContain(SHA_C);

  // без горизонтального overflow сторінки
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});