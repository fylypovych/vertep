import { test, expect } from '@playwright/test';

const mockBrands: any[] = [
  { id: 'brand_1', name: 'Brand One', enabled: true, metadata: {}, publishing: {} },
  { id: 'brand_2', name: 'Brand Two', enabled: false, metadata: { tag: 'vip' }, publishing: { youtube: {} } },
];

test.describe('Brands CRUD', () => {
  let brandsState = [...mockBrands];

  async function setupRoutes(page: any) {
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const method = request.method();

      if (path.endsWith('/session')) {
        return route.fulfill({ json: { username: 'admin', role: 'admin' } });
      }
      if (path.endsWith('/status')) {
        return route.fulfill({ json: { system: { state: 'NORMAL' } } });
      }
      if (path === '/api/brands') {
        if (method === 'GET') {
          return route.fulfill({ json: brandsState });
        }
        if (method === 'POST') {
          const payload = request.postDataJSON();
          const newBrand = { ...payload, id: payload.id };
          brandsState.push(newBrand);
          return route.fulfill({ json: newBrand });
        }
      }
      if (path.match(/\/api\/brands\/[^/]+$/)) {
        const brandId = path.split('/').pop()!;
        if (method === 'PUT') {
          const payload = request.postDataJSON();
          const idx = brandsState.findIndex(b => b.id === brandId);
          if (idx >= 0) {
            brandsState[idx] = { ...brandsState[idx], ...payload };
            return route.fulfill({ json: brandsState[idx] });
          }
          return route.fulfill({ status: 404, json: { detail: 'Not found' } });
        }
        if (method === 'DELETE') {
          const idx = brandsState.findIndex(b => b.id === brandId);
          if (idx >= 0) {
            brandsState.splice(idx, 1);
            return route.fulfill({ json: { deleted: brandId } });
          }
          return route.fulfill({ status: 404, json: { detail: 'Not found' } });
        }
      }
      return route.fulfill({ json: [] });
    });
  }

  test('Список брендів відображається після завантаження', async ({ page }) => {
    await setupRoutes(page);
    await page.goto('/brands');
    await expect(page.getByTestId('brands-page')).toBeVisible();
    await expect(page.getByText('Brand One')).toBeVisible();
    await expect(page.getByText('Brand Two')).toBeVisible();
  });

  test('Створення нового бренду', async ({ page }) => {
    await setupRoutes(page);
    await page.goto('/brands');
    await page.getByTestId('create-brand-button').click();
    await expect(page.getByTestId('brand-editor-modal')).toBeVisible();
    await page.locator('input[placeholder="наприклад, my_brand"]').fill('brand_3');
    await page.locator('input[placeholder="Назва бренду"]').fill('Brand Three');
    await page.locator('textarea').first().fill('{"key": "value"}');
    await page.getByRole('button', { name: 'Зберегти', exact: true }).click();
    await expect(page.getByTestId('brand-editor-modal')).toBeHidden();
    await expect(page.getByText('Brand Three')).toBeVisible();
    expect(brandsState.find(b => b.id === 'brand_3')).toBeTruthy();
  });

  test('Редагування існуючого бренду', async ({ page }) => {
    await setupRoutes(page);
    await page.goto('/brands');
    await page.getByRole('button', { name: 'Редагувати', exact: true }).first().click();
    await expect(page.getByTestId('brand-editor-modal')).toBeVisible();
    await page.locator('input[placeholder="Назва бренду"]').fill('Brand One Updated');
    await page.getByRole('button', { name: 'Зберегти', exact: true }).click();
    await expect(page.getByTestId('brand-editor-modal')).toBeHidden();
    await expect(page.getByText('Brand One Updated')).toBeVisible();
    expect(brandsState.find(b => b.id === 'brand_1')?.name).toBe('Brand One Updated');
  });

  test('Видалення бренду викликає DELETE API', async ({ page }) => {
    let deleteCalled = false;
    await page.route('**/api/brands/brand_1', async route => {
      if (route.request().method() === 'DELETE') {
        deleteCalled = true;
        brandsState = brandsState.filter(b => b.id !== 'brand_1');
        return route.fulfill({ json: { deleted: 'brand_1' } });
      }
      return route.continue();
    });
    await setupRoutes(page);
    await page.goto('/brands');
    page.on('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: 'Видалити', exact: true }).first().click();
    await page.waitForTimeout(500);
    expect(deleteCalled).toBe(true);
    expect(brandsState.find(b => b.id === 'brand_1')).toBeFalsy();
  });

  test('Відображення стану бренду (активний/неактивний)', async ({ page }) => {
    await setupRoutes(page);
    await page.goto('/brands');
    await expect(page.getByText('Активний').first()).toBeVisible();
    await expect(page.getByText('Неактивний')).toBeVisible();
  });
});