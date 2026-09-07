import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app.config';
import { AppComponent } from './app/app.component';

console.log('[DIAG][main] bootstrapping Vertep V2');
bootstrapApplication(AppComponent, appConfig).then((ref) => {
  console.log('[DIAG][main] bootstrap success', ref);
}).catch((err) => {
  console.error('[DIAG][main] bootstrap failed', err);
});
