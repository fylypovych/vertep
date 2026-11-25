/** Menu entries of active official modules: their pages exist only while the module is READY. */
export const ACTIVE_MODULE_MENU = [
  { module_id: 'brands', id: 'brands', label: 'Бренди', path: '/brands', group: 'content', admin_only: false },
  { module_id: 'characters', id: 'characters', label: 'Персонажі', path: '/characters', group: 'content', admin_only: false },
  { module_id: 'publishing', id: 'publishing', label: 'Опубліковане', path: '/published', group: 'content', admin_only: false },
];

/** Registered after a catch-all `**\/api/**` route, so it takes precedence for the menu. */
export const activeModules = async (page: any, items = ACTIVE_MODULE_MENU, catalog = ['backup']) => {
  await page.route('**/api/modules/navigation', (route: any) => route.fulfill({ json: { items } }));
  // Built-in pages without a module menu entry (Бекапи) follow the module's state in the catalog.
  await page.route('**/api/nodes/module-catalog', (route: any) => route.fulfill({ json: {
    modules: catalog.map(module_id => ({ module_id, state: 'ACTIVE' })), nodes: [] } }));
};
