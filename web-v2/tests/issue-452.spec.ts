import { test, expect } from '@playwright/test';

const SHA_A = 'a'.repeat(64);
const SHA_B = 'b'.repeat(64);
const SHA_C = 'c'.repeat(64);
// Real hexadecimal fingerprints, not only repeated letters.
const HEX = ['9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08',
  '60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752',
  'fd61a03af4f77d870fc21e05e7e80678095c92d808cfb3b5c279ee04c74aca13'];

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

// Wide, tablet, phone and 200% browser zoom (a 1280px window shows a 640px CSS viewport).
for (const viewport of [{ width: 1440, height: 900 }, { width: 900, height: 900 }, { width: 375, height: 800 }, { width: 640, height: 450 }]) {
  test(`SHA-256 лишаються в межах картки й не накладаються на «Інтеграції» при ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.route('**/api/**', async (route: any) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/session')) return route.fulfill({ json: { authenticated: true, username: 'admin', user: 'admin', role: 'admin' } });
      if (path.endsWith('/status')) return route.fulfill({ json: { system: { state: 'NORMAL', reason: null }, version: '0' } });
      if (path.endsWith('/security/check')) return route.fulfill({ json: { ok: true, weak_or_missing: [], recommendation: '', checks: {
        secrets_store: { status: 'ok', detail: '' },
        certificates: {
          node_ca: { status: 'ok', sha256: HEX[0] },
          server_certificate: { status: 'ok', sha256: HEX[1] },
          server_key: { status: 'ok', sha256: HEX[2] },
        },
        integrations: [{ name: 'telegram', status: 'ok' }], effective_config: {},
      } } });
      return route.fulfill({ json: {} });
    });
    await page.goto('/settings?tab=security');
    const certificates = page.locator('li[data-testid^="security-cert-"]');
    await expect(certificates).toHaveCount(3, { timeout: 20000 });
    const card = certificates.first().locator('xpath=ancestor::div[contains(@class,"rounded-xl")][1]');
    const integrations = page.getByRole('heading', { name: 'Інтеграції' }).locator('xpath=..');
    const cardBox = (await card.boundingBox())!;
    const integrationsBox = (await integrations.boundingBox())!;
    for (const [index, sha] of HEX.entries()) {
      const value = certificates.nth(index).locator('span.font-mono');
      // The complete fingerprint is rendered and selectable, never truncated.
      expect((await value.textContent())!.replace(/\s+/g, '')).toContain(sha);
      expect(await value.evaluate(element => getComputedStyle(element).userSelect)).not.toBe('none');
      const box = (await value.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(cardBox.x - 0.5);
      expect(box.x + box.width).toBeLessThanOrEqual(cardBox.x + cardBox.width + 0.5);
      expect(await value.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
    }
    expect(await card.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
    const overlap = !(cardBox.x + cardBox.width <= integrationsBox.x + 0.5 || integrationsBox.x + integrationsBox.width <= cardBox.x + 0.5
      || cardBox.y + cardBox.height <= integrationsBox.y + 0.5 || integrationsBox.y + integrationsBox.height <= cardBox.y + 0.5);
    expect(overlap).toBe(false);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  });
}
