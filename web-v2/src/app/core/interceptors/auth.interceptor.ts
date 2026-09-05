import { HttpInterceptorFn } from '@angular/common/http';

export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const csrf = document.cookie
    .split('; ')
    .find(x => x.startsWith('vertep_csrf='))
    ?.split('=')[1];
  
  let headers = req.headers;
  if (csrf) {
    headers = headers.set('X-CSRF-Token', csrf);
  }
  
  const cloned = req.clone({ headers });
  return next(cloned);
};
