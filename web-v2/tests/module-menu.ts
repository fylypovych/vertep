/** Menu entries of active official modules: their pages exist only while the module is READY. */
export const ACTIVE_MODULE_MENU = [
  { module_id: 'brands', id: 'brands', label: 'Бренди', path: '/brands', group: 'content', admin_only: false },
  { module_id: 'characters', id: 'characters', label: 'Персонажі', path: '/characters', group: 'content', admin_only: false },
  { module_id: 'publishing', id: 'publishing', label: 'Опубліковане', path: '/published', group: 'content', admin_only: false },
];

/** Registered after a catch-all `**\/api/**` route, so it takes precedence for the menu. */
export const activeModules = async (page: any, items = ACTIVE_MODULE_MENU, catalog = ['backup']) => {
  await page.route('**/api/modules/navigation', (route: any) => route.fulfill({ json: { items } }));
  // «Бекапи» follows the backup service: it answers when 'backup' is listed, otherwise 502.
  await page.route('**/api/system/backups/available', (route: any) =>
    route.fulfill({ json: { available: catalog.includes('backup') } }));
  if (!catalog.includes('backup')) {
    await page.route('**/api/system/backups', (route: any) =>
      route.fulfill({ status: 502, json: { detail: 'Сервіс BACKUP_URL недоступний. Перевірте, чи запущений відповідний сервіс.' } }));
  }
};
