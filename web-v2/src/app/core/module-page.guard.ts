import { inject } from '@angular/core';
import { ActivatedRouteSnapshot, CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs/operators';
import { ModulesApiService } from './api/modules.api';
import { ToastService } from './services/toast.service';
import { I18nService } from './i18n/i18n.service';

/** A page owned by a module opens only while that module is READY and declares it in its menu. */
export const modulePageGuard: CanActivateFn = (route: ActivatedRouteSnapshot) => {
  const router = inject(Router);
  const toast = inject(ToastService);
  const i18n = inject(I18nService);
  const page = '/' + (route.url[0]?.path ?? '');
  return inject(ModulesApiService).navigation().pipe(map(items => {
    if (items.some(item => item.path === page)) return true;
    toast.show(i18n.t('Модуль вимкнено: ця сторінка працює лише з активним модулем'), 'error');
    return router.createUrlTree(['/']);
  }));
};
