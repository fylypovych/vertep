# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: brands-crud.spec.ts >> Brands CRUD >> Видалення бренду викликає DELETE API
- Location: tests\brands-crud.spec.ts:92:7

# Error details

```
Error: expect(received).toBe(expected) // Object.is equality

Expected: true
Received: false
```

# Page snapshot

```yaml
- generic [ref=e4]:
  - navigation "Навігація" [ref=e6]:
    - generic [ref=e8]:
      - generic [ref=e9]: V
      - generic [ref=e10]: Vertep
    - navigation [ref=e11]:
      - link "Дашборд" [ref=e12] [cursor=pointer]:
        - /url: /
      - link "Завдання" [ref=e15] [cursor=pointer]:
        - /url: /jobs
      - link "Виконання" [ref=e18] [cursor=pointer]:
        - /url: /jobs?tab=queue
      - link "Опубліковане" [ref=e21] [cursor=pointer]:
        - /url: /published
      - link "Вузли" [ref=e24] [cursor=pointer]:
        - /url: /workers
      - link "Персонажі" [ref=e27] [cursor=pointer]:
        - /url: /characters
      - link "Сценарії" [ref=e30] [cursor=pointer]:
        - /url: /workflows
      - link "Бренди" [ref=e33] [cursor=pointer]:
        - /url: /brands
      - link "Операції" [ref=e36] [cursor=pointer]:
        - /url: /operations
      - link "Налаштування" [ref=e39] [cursor=pointer]:
        - /url: /settings
    - generic [ref=e42]: Vertep v...
  - generic [ref=e44]:
    - banner [ref=e46]:
      - generic [ref=e47]:
        - button "Перемикач меню" [ref=e48]
        - generic [ref=e51]:
          - heading "Дашборд" [level=1] [ref=e52]
          - paragraph [ref=e53]: Огляд системи Vertep
      - generic [ref=e54]:
        - generic [ref=e55]: Нормальний
        - button "Темна тема" [ref=e57]
        - button "v1" [ref=e60]
        - button "A" [ref=e61]
    - main "Основний вміст" [ref=e63]:
      - generic [ref=e65]:
        - generic [ref=e66]:
          - heading "Бренди" [level=2] [ref=e67]
          - button "Новий бренд" [ref=e68]
        - generic [ref=e69]:
          - generic [ref=e70]:
            - generic [ref=e71]:
              - generic [ref=e72]:
                - heading "Brand One" [level=3] [ref=e73]
                - paragraph [ref=e74]: brand_1
              - generic [ref=e75]:
                - button "Редагувати" [ref=e76]
                - button "Видалити" [active] [ref=e77]
            - generic [ref=e78]: Активний
            - generic [ref=e80]:
              - generic [ref=e81]:
                - heading "Канали бренду" [level=4] [ref=e82]
                - button "Додати канал" [ref=e83]
              - paragraph [ref=e84]: Каналів не налаштовано
          - generic [ref=e85]:
            - generic [ref=e86]:
              - generic [ref=e87]:
                - heading "Brand Two" [level=3] [ref=e88]
                - paragraph [ref=e89]: brand_2
              - generic [ref=e90]:
                - button "Редагувати" [ref=e91]
                - button "Видалити" [ref=e92]
            - generic [ref=e93]: Неактивний
            - generic [ref=e95]:
              - generic [ref=e96]:
                - heading "Канали бренду" [level=4] [ref=e97]
                - button "Додати канал" [ref=e98]
              - paragraph [ref=e99]: Каналів не налаштовано
  - generic:
    - status "Сповіщення"
  - dialog [ref=e100]:
    - generic [ref=e101]:
      - heading "Видалити бренд" [level=3] [ref=e102]
      - paragraph [ref=e103]: Ви впевнені, що хочете видалити бренд Brand One (brand_1)?
      - generic [ref=e104]:
        - button "Скасувати" [ref=e105]
        - button "Підтвердити" [ref=e106]
```

# Test source

```ts
  7   | 
  8   | test.describe('Brands CRUD', () => {
  9   |   let brandsState = [...mockBrands];
  10  | 
  11  |   async function setupRoutes(page: any) {
  12  |     await page.route('**/api/**', async route => {
  13  |       const request = route.request();
  14  |       const path = new URL(request.url()).pathname;
  15  |       const method = request.method();
  16  | 
  17  |       if (path.endsWith('/session')) {
  18  |         return route.fulfill({ json: { username: 'admin', role: 'admin' } });
  19  |       }
  20  |       if (path.endsWith('/status')) {
  21  |         return route.fulfill({ json: { system: { state: 'NORMAL' } } });
  22  |       }
  23  |       if (path === '/api/brands') {
  24  |         if (method === 'GET') {
  25  |           return route.fulfill({ json: brandsState });
  26  |         }
  27  |         if (method === 'POST') {
  28  |           const payload = request.postDataJSON();
  29  |           const newBrand = { ...payload, id: payload.id };
  30  |           brandsState.push(newBrand);
  31  |           return route.fulfill({ json: newBrand });
  32  |         }
  33  |       }
  34  |       if (path.match(/\/api\/brands\/[^/]+$/)) {
  35  |         const brandId = path.split('/').pop()!;
  36  |         if (method === 'PUT') {
  37  |           const payload = request.postDataJSON();
  38  |           const idx = brandsState.findIndex(b => b.id === brandId);
  39  |           if (idx >= 0) {
  40  |             brandsState[idx] = { ...brandsState[idx], ...payload };
  41  |             return route.fulfill({ json: brandsState[idx] });
  42  |           }
  43  |           return route.fulfill({ status: 404, json: { detail: 'Not found' } });
  44  |         }
  45  |         if (method === 'DELETE') {
  46  |           const idx = brandsState.findIndex(b => b.id === brandId);
  47  |           if (idx >= 0) {
  48  |             brandsState.splice(idx, 1);
  49  |             return route.fulfill({ json: { deleted: brandId } });
  50  |           }
  51  |           return route.fulfill({ status: 404, json: { detail: 'Not found' } });
  52  |         }
  53  |       }
  54  |       return route.fulfill({ json: [] });
  55  |     });
  56  |   }
  57  | 
  58  |   test('Список брендів відображається після завантаження', async ({ page }) => {
  59  |     await setupRoutes(page);
  60  |     await page.goto('/brands');
  61  |     await expect(page.getByTestId('brands-page')).toBeVisible();
  62  |     await expect(page.getByText('Brand One')).toBeVisible();
  63  |     await expect(page.getByText('Brand Two')).toBeVisible();
  64  |   });
  65  | 
  66  |   test('Створення нового бренду', async ({ page }) => {
  67  |     await setupRoutes(page);
  68  |     await page.goto('/brands');
  69  |     await page.getByTestId('create-brand-button').click();
  70  |     await expect(page.getByTestId('brand-editor-modal')).toBeVisible();
  71  |     await page.locator('input[placeholder="наприклад, my_brand"]').fill('brand_3');
  72  |     await page.locator('input[placeholder="Назва бренду"]').fill('Brand Three');
  73  |     await page.locator('textarea').first().fill('{"key": "value"}');
  74  |     await page.getByRole('button', { name: 'Зберегти', exact: true }).click();
  75  |     await expect(page.getByTestId('brand-editor-modal')).toBeHidden();
  76  |     await expect(page.getByText('Brand Three')).toBeVisible();
  77  |     expect(brandsState.find(b => b.id === 'brand_3')).toBeTruthy();
  78  |   });
  79  | 
  80  |   test('Редагування існуючого бренду', async ({ page }) => {
  81  |     await setupRoutes(page);
  82  |     await page.goto('/brands');
  83  |     await page.getByRole('button', { name: 'Редагувати', exact: true }).first().click();
  84  |     await expect(page.getByTestId('brand-editor-modal')).toBeVisible();
  85  |     await page.locator('input[placeholder="Назва бренду"]').fill('Brand One Updated');
  86  |     await page.getByRole('button', { name: 'Зберегти', exact: true }).click();
  87  |     await expect(page.getByTestId('brand-editor-modal')).toBeHidden();
  88  |     await expect(page.getByText('Brand One Updated')).toBeVisible();
  89  |     expect(brandsState.find(b => b.id === 'brand_1')?.name).toBe('Brand One Updated');
  90  |   });
  91  | 
  92  |   test('Видалення бренду викликає DELETE API', async ({ page }) => {
  93  |     let deleteCalled = false;
  94  |     await page.route('**/api/brands/brand_1', async route => {
  95  |       if (route.request().method() === 'DELETE') {
  96  |         deleteCalled = true;
  97  |         brandsState = brandsState.filter(b => b.id !== 'brand_1');
  98  |         return route.fulfill({ json: { deleted: 'brand_1' } });
  99  |       }
  100 |       return route.continue();
  101 |     });
  102 |     await setupRoutes(page);
  103 |     await page.goto('/brands');
  104 |     page.on('dialog', dialog => dialog.accept());
  105 |     await page.getByRole('button', { name: 'Видалити', exact: true }).first().click();
  106 |     await page.waitForTimeout(500);
> 107 |     expect(deleteCalled).toBe(true);
      |                          ^ Error: expect(received).toBe(expected) // Object.is equality
  108 |     expect(brandsState.find(b => b.id === 'brand_1')).toBeFalsy();
  109 |   });
  110 | 
  111 |   test('Відображення стану бренду (активний/неактивний)', async ({ page }) => {
  112 |     await setupRoutes(page);
  113 |     await page.goto('/brands');
  114 |     await expect(page.getByText('Активний').first()).toBeVisible();
  115 |     await expect(page.getByText('Неактивний')).toBeVisible();
  116 |   });
  117 | });
```