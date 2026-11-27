import { test, expect } from '@playwright/test';
import { activeModules } from './module-menu';

interface MockOptions {
  role?: 'admin' | 'moderator';
  brands?: any[];
  characters?: any[];
  users?: any[];
  workers?: any[];
  jobs?: any[];
  labUrl?: string | null;
  modules?: any[];
  catalog?: string[];
}

const pageErrors: string[] = [];
test.beforeEach(() => { pageErrors.length = 0; });
test.afterEach(() => { expect(pageErrors, 'uncaught UI errors').toEqual([]); });

async function setupApiMocks(page: any, opts: MockOptions = {}) {
  page.on('console', (m: any) => { if (m.type() === 'error' && m.text().includes('GlobalErrorHandler')) pageErrors.push(m.text()); });
  const role = opts.role ?? 'admin';
  const brands = opts.brands ?? [
    { id: 'vertep', name: 'Vertep Studio', enabled: true, metadata: {}, publishing: {} },
    { id: 'legends', name: 'Legends', enabled: true, metadata: {}, publishing: {} },
  ];
  const characters = opts.characters ?? [
    {
      id: 'narrator-1',
      name: 'Дід Оповідач',
      language: 'uk',
      enabled: true,
      active: true,
      owner_brand_id: 'vertep',
      system_prompt: 'Спокійний наратор',
      voice: { provider: 'piper', language: 'uk', voice: 'uk_Kyiv' },
      visual: { style: 'warm documentary', aspect_ratio: '16:9', output_preset: 'youtube' },
      generation: { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 },
      publishing: { enabled: true, channels: 'youtube' },
      references: [{ filename: 'ref1.png', sha256: 'abc', size: 100 }],
      collaborations: [],
      is_owner: true,
    },
    {
      id: 'hero-2',
      name: 'Козак Мамай',
      language: 'uk',
      enabled: true,
      active: false,
      owner_brand_id: 'vertep',
      system_prompt: 'Мудрий мандрівник',
      voice: { provider: 'kokoro', language: 'uk', voice: 'uk_Mamai' },
      visual: { style: 'painterly', aspect_ratio: '9:16', output_preset: 'shorts' },
      generation: { workflow: 'workflows/image/demo.json', min_vram_mb: 4096, max_retries: 3 },
      publishing: { enabled: false, channels: '' },
      references: [],
      collaborations: [],
      is_owner: true,
    },
  ];
  const users = opts.users ?? [
    { user: 'admin', role: 'admin', brands: null, display_name: 'Головний Адмін', email: 'admin@vertep.local', editable: false },
    { user: 'mod1', role: 'moderator', brands: ['vertep'], display_name: 'Олена Модератор', email: 'olena@vertep.local', editable: true },
  ];
  const workers = opts.workers ?? [
    { node_id: 'gpu-1', node_name: 'gpu-node-01', role: 'gpu', status: 'READY', capabilities: ['image', 'video'], gpu_name: 'RTX 4090', gpu_load: 42, temperature: 61 },
  ];
  const jobs = opts.jobs ?? [
    { job_id: 'job-001', topic: 'Історія Карпат', status: 'IN_PROGRESS', character_id: 'narrator-1', priority: 5, created_at: '2026-11-26T09:00:00Z' },
  ];

  await page.route('**/api/**', async (route: any) => {
    const req = route.request();
    const url = new URL(req.url()).pathname;

    if (url.endsWith('/session')) {
      return route.fulfill({
        json: {
          authenticated: true,
          user: role === 'admin' ? 'admin' : 'mod1',
          username: role === 'admin' ? 'admin' : 'mod1',
          display_name: role === 'admin' ? 'Головний Адмін' : 'Олена Модератор',
          email: role === 'admin' ? 'admin@vertep.local' : 'olena@vertep.local',
          role,
          brands: role === 'admin' ? null : ['vertep'],
          password_policy: { min_length: 12 },
        },
      });
    }
    if (url.endsWith('/session/profile') && req.method() === 'PUT') {
      const body = req.postDataJSON();
      return route.fulfill({
        json: {
          authenticated: true,
          user: role === 'admin' ? 'admin' : 'mod1',
          role,
          display_name: body.display_name,
          email: body.email,
          password_policy: { min_length: 12 },
        },
      });
    }
    if (url.endsWith('/status')) {
      return route.fulfill({
        json: {
          version: '0.0.4.50',
          api_major: 1,
          system: { state: 'NORMAL', reason: 'Штатний режим' },
          resources: { cpu: 28, ram: 54, disk: 41, cpu_cores: 8, cpu_used_cores: 2 },
        },
      });
    }
    if (url.endsWith('/lab/panel')) {
      return route.fulfill({ json: { url: opts.labUrl ?? null } });
    }
    if (url.endsWith('/workers')) {
      return route.fulfill({ json: workers });
    }
    if (url.endsWith('/jobs')) {
      return route.fulfill({ json: jobs });
    }
    if (url.endsWith('/tasks/queue')) {
      return route.fulfill({ json: { ready: [], inflight: [], dead_letter: [] } });
    }
    if (url.endsWith('/brands')) {
      return route.fulfill({ json: brands });
    }
    if (/\/brands\/[^/]+\/channels$/.test(url)) {
      return route.fulfill({ json: [] });
    }
    if (url.endsWith('/shared-content')) {
      return route.fulfill({ json: { brand_id: new URL(req.url()).searchParams.get('brand_id'), items: [] } });
    }
    if (url.endsWith('/modules/characters/v1/characters')) {
      return route.fulfill({ json: characters });
    }
    if (/\/modules\/characters\/v1\/characters\/[^/]+$/.test(url)) {
      const id = decodeURIComponent(url.split('/').pop() || '');
      if (req.method() === 'PUT') {
        const body = req.postDataJSON();
        return route.fulfill({ json: { ...characters[0], ...body, id } });
      }
      const found = characters.find(c => c.id === id);
      return found ? route.fulfill({ json: found }) : route.fulfill({ status: 404, json: { detail: 'Not found' } });
    }
    if (/\/references\/[^/]+$/.test(url)) {
      // 1x1 transparent PNG
      const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');
      return route.fulfill({ status: 200, contentType: 'image/png', body: png });
    }
    if (url.endsWith('/settings/users')) {
      return route.fulfill({ json: { users } });
    }
    if (url.endsWith('/alerts') || url.endsWith('/logs')) {
      return route.fulfill({ json: [] });
    }
    if (url.endsWith('/health')) {
      return route.fulfill({ json: { status: 'ok', service: 'core', jobs: {}, checks: {} } });
    }
    return route.fulfill({ json: req.method() === 'GET' ? [] : {} });
  });

  await activeModules(page, opts.modules, opts.catalog);
}

test('#536: перемикач v2 ↔ v3 зберігає стан у localStorage і коректно мапить маршрути та deep-links', async ({ page }) => {
  await setupApiMocks(page, { role: 'admin' });
  await page.goto('/alerts');
  await expect(page.getByTestId('switch-to-v3')).toBeVisible({ timeout: 20000 });

  await page.getByTestId('switch-to-v3').click();
  await expect(page).toHaveURL(/\/v3\/monitor\?tab=alerts$/);
  await expect(page.getByTestId('v3-layout')).toBeVisible();
  await expect(page.getByTestId('v3-tab-alerts')).toHaveAttribute('aria-selected', 'true');
  expect(await page.evaluate(() => localStorage.getItem('vertep_ui_version'))).toBe('v3');

  // Switch back to v2 from v3 navbar
  await page.getByTestId('switch-to-v2').click();
  await expect(page).toHaveURL(/\/alerts$/);
  expect(await page.evaluate(() => localStorage.getItem('vertep_ui_version'))).toBe('v2');

  await page.goto('/v3/settings?tab=integrations');
  await expect(page.getByTestId('v3-settings-workspace')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('settings-publishing')).toHaveCount(0);  // moved to Brands and General
  await page.goto('/v3/settings?tab=general');
  const address = page.locator('[data-testid="settings-publishing"][data-part="address"]');
  await expect(address.getByTestId('publishing-public-url')).toBeVisible();
  await expect(address.getByTestId('publishing-credentials')).toHaveCount(0);

  // Direct v3 deep links map to the proper tabbed workspaces
  await page.goto('/v3/settings?tab=models');
  await expect(page).toHaveURL(/\/v3\/pipeline\?tab=models$/);
  await expect(page.getByTestId('v3-tab-models')).toHaveAttribute('aria-selected', 'true');

  await page.goto('/v3/queue');
  await expect(page.getByTestId('v3-jobs-workspace')).toBeVisible();
  await expect(page.getByTestId('v3-tab-queue')).toHaveAttribute('aria-selected', 'true');

  // Switching tabs inside /v3/jobs must stay inside v3 (never bounce to v2 /queue or /jobs)
  await page.goto('/v3/jobs');
  await expect(page.getByTestId('v3-jobs-workspace')).toBeVisible();
  await page.getByTestId('v3-tab-queue').click();
  await expect(page).toHaveURL(/\/v3\/jobs\?tab=queue$/);
  await expect(page.getByTestId('v3-layout')).toBeVisible();
  await expect(page.getByTestId('v3-tab-queue')).toHaveAttribute('aria-selected', 'true');
  await page.getByTestId('v3-tab-list').click();
  await expect(page).toHaveURL(/\/v3\/jobs\?tab=list$/);
  await expect(page.getByTestId('v3-layout')).toBeVisible();
});

test('#536: реорганізоване меню V3Layout враховує RBAC, динамічні модулі, Лабораторію та офлайн-ресурси', async ({ page }) => {
  const externalUrls: string[] = [];
  page.on('request', req => {
    const url = req.url();
    if (!url.startsWith('http://localhost:4200') && !url.startsWith('http://127.0.0.1:4200') && !url.startsWith('data:')) {
      externalUrls.push(url);
    }
  });

  await setupApiMocks(page, { role: 'admin', labUrl: 'https://lab.local' });
  await page.goto('/v3');
  const nav = page.getByTestId('v3-sidebar-nav');
  await expect(nav.getByText('Налаштування', { exact: true })).toBeVisible({ timeout: 20000 });

  const adminGroups = await nav.evaluate((el: HTMLElement) => {
    const result: Record<string, string[]> = { '': [] };
    let current = '';
    for (const node of Array.from(el.querySelectorAll('[data-testid^="v3-nav-group-"], a'))) {
      const h = node as HTMLElement;
      if (h.dataset['testid']?.startsWith('v3-nav-group-')) { current = (h.textContent || '').trim(); result[current] = []; }
      else result[current].push((h.textContent || '').trim());
    }
    return result;
  });

  expect(Object.keys(adminGroups)).toEqual(['', 'Студія', 'Інфраструктура', 'Керування']);
  expect(adminGroups['']).toEqual(['Дашборд']);
  expect(adminGroups['Студія']).toEqual(['Завдання і черга', 'Бренди', 'Персонажі', 'Опубліковане']);
  expect(adminGroups['Інфраструктура']).toEqual(['Вузли', 'Моніторинг', 'Модулі та обслуговування', 'Лабораторія']);
  expect(adminGroups['Керування']).toEqual(['Конвеєр', 'Налаштування']);

  // Verify light paper background and zero external CDN requests
  const bg = await page.getByTestId('v3-layout').evaluate(el => getComputedStyle(el).backgroundColor);
  expect(bg).toBe('rgb(244, 243, 239)');
  expect(externalUrls).toEqual([]);

  // Moderator sees only non-admin groups & items
  await setupApiMocks(page, { role: 'moderator', labUrl: 'https://lab.local' });
  await page.goto('/v3');
  await expect(nav.getByText('Вузли', { exact: true })).toBeVisible({ timeout: 20000 });
  await expect(nav.getByText('Керування', { exact: true })).toHaveCount(0);
  await expect(nav.getByText('Налаштування', { exact: true })).toHaveCount(0);
  await expect(nav.getByText('Модулі та обслуговування', { exact: true })).toHaveCount(0);
  await expect(nav.getByText('Лабораторія', { exact: true })).toHaveCount(0);
});

test('#536: MVP-сторінки v3 — Dashboard, Profile, Brands (Персонажі та Модератори бренду), Characters і Character Detail', async ({ page }) => {
  await setupApiMocks(page, { role: 'admin' });

  // 1. Dashboard: KPI cards + Workers MembersListCard + Jobs table
  await page.goto('/v3');
  await expect(page.getByTestId('v3-dashboard')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('v3-stat-workers')).toContainText('1');
  await expect(page.getByTestId('v3-dashboard-workers')).toContainText('gpu-node-01');
  await expect(page.getByTestId('v3-dashboard-jobs')).toContainText('Історія Карпат');

  // 2. Profile (EntityProfileLayout)
  await page.goto('/v3/profile');
  await expect(page.getByTestId('v3-profile-page')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('v3-profile-title')).toHaveText('Головний Адмін');
  await expect(page.getByTestId('v3-profile-permissions')).toBeVisible();
  await page.getByTestId('v3-display-name-input').fill('Оновлений Адмін');
  await page.getByTestId('v3-account-save').click();
  await expect(page.getByTestId('v3-profile-title')).toHaveText('Оновлений Адмін');

  // 3. Brands: Team Members blocks (Brand Characters + Brand Moderators)
  await page.goto('/v3/brands');
  await expect(page.getByTestId('v3-brands-page')).toBeVisible({ timeout: 20000 });
  const brandChars = page.getByTestId('v3-brand-characters-vertep');
  await expect(brandChars).toContainText('Дід Оповідач');
  await expect(brandChars).toContainText('Козак Мамай');
  const brandMods = page.getByTestId('v3-brand-moderators-vertep');
  await expect(brandMods).toContainText('Олена Модератор');
  // Shared platform credentials live with the brands; Settings keeps the public address in «Загальне».
  const platforms = page.locator('[data-testid="settings-publishing"][data-part="platforms"]');
  await expect(platforms).toContainText('Платформи публікації');
  await expect(platforms.getByTestId('publishing-credentials')).toBeVisible();
  await expect(platforms.getByTestId('publishing-public-url')).toHaveCount(0);

  // Navigate directly from Brand -> Character Profile (EntityProfileLayout)
  await brandChars.getByText('Дід Оповідач').click();
  await expect(page).toHaveURL(/\/v3\/characters\/narrator-1$/);
  await expect(page.getByTestId('v3-character-profile')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('v3-profile-title')).toHaveText('Дід Оповідач');
  await expect(page.getByTestId('v3-profile-avatar-img')).toBeVisible();
  await page.getByTestId('v3-character-name').fill('Дід Оповідач Оновлений');
  await page.getByTestId('v3-character-save-btn').click();
  await expect(page.getByTestId('v3-profile-title')).toHaveText('Дід Оповідач Оновлений');

  // 4. Characters catalog with brand filter
  await page.goto('/v3/characters');
  await expect(page.getByTestId('v3-characters-page')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('v3-character-card-narrator-1')).toBeVisible();
  await expect(page.getByTestId('v3-character-card-hero-2')).toBeVisible();
});

test('#554: модератор бачить «Шаблони обробки» у v3, перемикається v2 ↔ v3 без AdminGuard і не бачить адмінських вкладок', async ({ page }) => {
  await setupApiMocks(page, { role: 'moderator' });
  await page.goto('/workflows');
  await expect(page.getByTestId('switch-to-v3')).toBeVisible({ timeout: 20000 });

  await page.getByTestId('switch-to-v3').click();
  await expect(page).toHaveURL(/\/v3\/pipeline\?tab=workflows$/);
  await expect(page.getByTestId('v3-pipeline-workspace')).toBeVisible();
  await expect(page.getByTestId('v3-tab-workflows')).toBeVisible();
  await expect(page.getByTestId('v3-tab-models')).toHaveCount(0);
  await expect(page.getByTestId('v3-tab-video-engine')).toHaveCount(0);
  await expect(page.getByTestId('v3-tab-workflow-mode')).toHaveCount(0);

  const nav = page.getByTestId('v3-sidebar-nav');
  await expect(nav.getByTestId('v3-nav-workflows')).toBeVisible();
  await expect(nav.getByTestId('v3-nav-workflows')).toHaveAttribute('aria-current', 'page');
  await expect(nav.getByText('Керування', { exact: true })).toHaveCount(0);

  // Moderator attempting an admin tab stays on workflows.
  await page.goto('/v3/pipeline?tab=models');
  await expect(page.getByTestId('v3-tab-workflows')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('v3-tab-models')).toHaveCount(0);

  // Switch back to v2 returns to /workflows.
  await page.getByTestId('switch-to-v2').click();
  await expect(page).toHaveURL(/\/workflows$/);
});

test('#554: облікові дані платформ доступні при вимкненому brands і deep-links publishing/integrations працюють у v2 ↔ v3', async ({ page }) => {
  // Active brands: /settings?tab=publishing -> v3 redirects to /v3/brands#platforms; /v3/settings?tab=integrations -> v2 goes to telegram.
  await setupApiMocks(page, { role: 'admin' });
  await page.goto('/settings?tab=publishing');
  await expect(page.getByTestId('switch-to-v3')).toBeVisible({ timeout: 20000 });
  await page.getByTestId('switch-to-v3').click();
  await expect(page).toHaveURL(/\/v3\/brands#platforms$/);
  await expect(page.locator('[data-testid="settings-publishing"][data-part="platforms"]')).toBeVisible();

  // Brand moderators link opens /v3/settings?tab=users directly.
  await page.getByTestId('v3-brand-moderators-vertep').getByText('Олена Модератор').click();
  await expect(page).toHaveURL(/\/v3\/settings\?tab=users$/);
  await expect(page.getByTestId('v3-tab-users')).toHaveAttribute('aria-selected', 'true');

  // Integrations tab in v3 maps back to telegram in v2 (not publishing).
  await page.goto('/v3/settings?tab=integrations');
  await expect(page.getByTestId('v3-tab-integrations')).toHaveAttribute('aria-selected', 'true');
  await page.getByTestId('switch-to-v2').click();
  await expect(page).toHaveURL(/\/settings\?tab=telegram$/);

  // Inactive brands: /v3/brands is guarded -> /v3, and platform credentials stay accessible in Settings -> Integrations.
  await setupApiMocks(page, { role: 'admin', modules: [] });
  await page.goto('/v3/brands');
  await expect(page).toHaveURL(/\/v3$/);

  await page.goto('/v3/settings?tab=publishing');
  await expect(page.getByTestId('v3-tab-integrations')).toHaveAttribute('aria-selected', 'true');
  const fallback = page.locator('[data-testid="settings-publishing"][data-part="platforms"]');
  await expect(fallback).toBeVisible();
  await expect(fallback.getByTestId('publishing-credentials')).toBeVisible();
});

test('#554: миттєве оновлення меню при зміні модулів/LAB, без колізії monitoring, заголовки модулів і підсвічування вкладок/аліасів', async ({ page }) => {
  let navItems: any[] = [
    { module_id: 'brands', id: 'brands', label: 'Бренди', path: '/brands', group: 'content', admin_only: false },
  ];
  let backupOn = true;
  let labUrl: string | null = null;
  let catalogModules: any[] = [
    {
      module_id: 'brands', label: 'Бренди', version: '0.0.4.17', kind: 'application', bundled: true, state: 'ACTIVE',
      runtime_role: null, requires_gpu: false, target: 'any', scaling: 'single_owner', resources: {},
      dependencies: [], permissions: [], approved_permissions: [], placement: ['core'],
      nodes: [{ node_id: 'core', compatible_reasons: [], runtime_missing: false, instance: { state: 'READY', reasons: [], version: '0.0.4.17' } }],
    },
    {
      module_id: 'monitoring', label: 'Моніторинг', version: '0.0.4.17', kind: 'application', bundled: true, state: 'INACTIVE',
      runtime_role: null, requires_gpu: false, target: 'any', scaling: 'single_owner', resources: {},
      dependencies: [], permissions: [], approved_permissions: [], placement: ['core'],
      nodes: [{ node_id: 'core', compatible_reasons: [], runtime_missing: false, instance: { state: 'INSTALLED', reasons: [], version: '0.0.4.17' } }],
    },
    {
      module_id: 'storyboard', label: 'Сторіборд', version: '0.0.4.19', kind: 'application', bundled: true, state: 'INACTIVE',
      runtime_role: null, requires_gpu: false, target: 'any', scaling: 'single_owner', resources: {},
      dependencies: [], permissions: [], approved_permissions: [], placement: ['core'],
      nodes: [{ node_id: 'core', compatible_reasons: [], runtime_missing: false, instance: { state: 'INSTALLED', reasons: [], version: '0.0.4.19' } }],
    },
    {
      module_id: 'backup', label: 'Резервні копії', version: '0.0.3.91', kind: 'role', bundled: true, state: 'ACTIVE',
      runtime_role: 'backup', requires_gpu: false, target: 'any', scaling: 'single_owner', resources: {},
      dependencies: [], permissions: [], approved_permissions: [], placement: ['core'],
      nodes: [{ node_id: 'core', compatible_reasons: [], runtime_missing: false, instance: { state: 'READY', reasons: [], version: '0.0.3.91' } }],
    },
  ];

  await setupApiMocks(page, { role: 'admin' });
  await page.route('**/api/modules/navigation', (route: any) => route.fulfill({ json: { items: navItems } }));
  await page.route('**/api/system/backups/available', (route: any) => route.fulfill({ json: { available: backupOn } }));
  await page.route('**/api/lab/panel', (route: any) => route.fulfill({ json: { url: labUrl } }));
  await page.route('**/api/settings/lab-panel', (route: any) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      labUrl = body.url || null;
      return route.fulfill({ json: { url: labUrl } });
    }
    return route.fulfill({ json: { url: labUrl } });
  });
  await page.route('**/api/modules/ui-extensions', (route: any) => route.fulfill({
    json: {
      extensions: [
        { module_id: 'storyboard', version: '0.0.4.19', url: '/api/modules/ui/storyboard/index.html', navigation: [{ id: 'storyboard', label: 'Сторіборд' }] },
      ],
    },
  }));
  await page.route('**/api/modules/ui/storyboard/index.html', (route: any) =>
    route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><html><body>Storyboard UI</body></html>' }));
  await page.route('**/api/nodes/module-catalog', (route: any) => route.fulfill({
    json: { modules: catalogModules, nodes: [{ node_id: 'core', role: 'core', environment: 'docker', isolation: 'container' }] },
  }));
  await page.route('**/api/nodes/module-registry/lifecycle/**', (route: any) => {
    const action = route.request().url().split('/').pop();
    const { module_id } = route.request().postDataJSON();
    catalogModules = catalogModules.map(m => m.module_id === module_id ? { ...m, state: action === 'activate' ? 'ACTIVE' : 'INACTIVE' } : m);
    if (module_id === 'brands') {
      navItems = action === 'activate'
        ? [...navItems, { module_id: 'brands', id: 'brands', label: 'Бренди', path: '/brands', group: 'content', admin_only: false }]
        : navItems.filter(i => i.module_id !== 'brands');
    }
    if (module_id === 'monitoring') {
      navItems = action === 'activate'
        ? [...navItems, { module_id: 'monitoring', id: 'monitoring', label: 'Моніторинг', path: '/modules/monitoring', group: 'modules', admin_only: false }]
        : navItems.filter(i => i.module_id !== 'monitoring');
    }
    if (module_id === 'storyboard') {
      navItems = action === 'activate'
        ? [...navItems, { module_id: 'storyboard', id: 'storyboard', label: 'Сторіборд', path: '/modules/storyboard', group: 'modules', admin_only: false }]
        : navItems.filter(i => i.module_id !== 'storyboard');
    }
    if (module_id === 'backup') {
      backupOn = action === 'activate';
    }
    return route.fulfill({ json: { ok: true } });
  });

  await page.goto('/v3/modules');
  const nav = page.getByTestId('v3-sidebar-nav');
  await expect(nav.getByText('Бренди', { exact: true })).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('v3-tab-backup')).toBeVisible();

  // Active highlight stays on «Модулі та обслуговування» when switching tabs (?tab=update).
  await page.getByTestId('v3-tab-update').click();
  await expect(page).toHaveURL(/\/v3\/modules\?tab=update$/);
  await expect(nav.getByTestId('v3-nav-modules')).toHaveAttribute('aria-current', 'page');
  await page.getByTestId('v3-tab-catalog').click();

  // Deactivate brands & backup -> sidebar and «Бекапи» tab update immediately without reload.
  await page.getByTestId('deactivate-brands').click();
  await expect(nav.getByText('Бренди', { exact: true })).toHaveCount(0);
  await page.getByTestId('deactivate-backup').click();
  await expect(page.getByTestId('v3-tab-backup')).toHaveCount(0);

  // Activate monitoring & storyboard -> immediate update, no duplicate «Моніторинг» label.
  await page.getByTestId('activate-monitoring').click();
  await page.getByTestId('activate-storyboard').click();
  await expect(nav.getByText('Моніторинг (модуль)', { exact: true })).toBeVisible();
  await expect(nav.getByText('Сторіборд', { exact: true })).toBeVisible();

  const allLabels = await nav.locator('a').allTextContents();
  const trimmed = allLabels.map(s => s.trim());
  expect(new Set(trimmed).size).toBe(trimmed.length);

  // Clicking module extension shows module title in header and does NOT highlight «Модулі та обслуговування».
  await nav.getByText('Сторіборд', { exact: true }).click();
  await expect(page).toHaveURL(/\/v3\/modules\/storyboard$/);
  await expect(page.getByTestId('v3-header-title')).toHaveText('Сторіборд');
  await expect(page.getByTestId('v3-header-subtitle')).toHaveText('Розширення модуля');
  await expect(nav.getByTestId('v3-nav-modules')).not.toHaveAttribute('aria-current', 'page');

  // Alias routes highlight their parent nav item.
  await page.goto('/v3/queue');
  await expect(nav.getByTestId('v3-nav-jobs')).toHaveAttribute('aria-current', 'page', { timeout: 20000 });
  await page.goto('/v3/alerts');
  await expect(nav.getByTestId('v3-nav-monitor')).toHaveAttribute('aria-current', 'page', { timeout: 20000 });
  await page.goto('/v3/workflows');
  await expect(nav.getByTestId('v3-nav-pipeline')).toHaveAttribute('aria-current', 'page', { timeout: 20000 });

  // Saving LAB URL updates «Лабораторія» item immediately without reload.
  await page.goto('/v3/settings?tab=general');
  await expect(nav).toBeVisible({ timeout: 20000 });
  await expect(nav.getByTestId('v3-nav-lab')).toHaveCount(0);
  await page.getByTestId('lab-panel-url').fill('https://lab.local');
  await page.getByTestId('lab-panel-save').click();
  await expect(nav.getByTestId('v3-nav-lab')).toBeVisible({ timeout: 20000 });
});
