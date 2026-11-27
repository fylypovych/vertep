import { Injectable, computed, signal } from '@angular/core';
import { HttpClient, HttpContext } from '@angular/common/http';
import { Observable, of } from 'rxjs';
import { tap } from 'rxjs/operators';
import { BACKGROUND_REQUEST } from '../interceptors/auth.interceptor';
import { ModuleNavItem, ModulesApiService } from '../api/modules.api';
import { PolicyService } from './policy.service';

/**
 * Single reactive source of truth for dynamic navigation state across v2 and v3:
 * active READY module menu entries, Backup availability, and LAB panel entry.
 * Any lifecycle mutation (activate/deactivate/uninstall module, connect/disconnect LAB)
 * triggers an immediate refresh instead of waiting for the 30s background poll (#554).
 */
@Injectable({ providedIn: 'root' })
export class NavigationStateService {
  readonly moduleItems = signal<ModuleNavItem[]>([]);
  readonly modulesLoaded = signal(false);
  readonly backupAvailable = signal(false);
  readonly labConfigured = signal(false);

  readonly brandsActive = computed(() =>
    this.moduleItems().some(item => item.path === '/brands'),
  );

  constructor(
    private modulesApi: ModulesApiService,
    private policy: PolicyService,
    private http: HttpClient,
  ) {}

  refreshModules(): Observable<ModuleNavItem[]> {
    return this.modulesApi.navigation().pipe(
      tap(items => {
        this.moduleItems.set(items);
        this.modulesLoaded.set(true);
      }),
    );
  }

  ensureModulesLoaded(): Observable<ModuleNavItem[]> {
    if (this.modulesLoaded()) {
      return of(this.moduleItems());
    }
    return this.refreshModules();
  }

  refreshBackup(): void {
    if (this.policy.userRole() !== 'admin') {
      this.backupAvailable.set(false);
      return;
    }
    this.http.get<{ available: boolean }>('/api/system/backups/available', {
      context: new HttpContext().set(BACKGROUND_REQUEST, true),
    }).subscribe({
      next: ({ available }) => this.backupAvailable.set(!!available),
      error: () => this.backupAvailable.set(false),
    });
  }

  refreshLab(): void {
    this.http.get<{ url: string | null }>('/api/lab/panel', {
      context: new HttpContext().set(BACKGROUND_REQUEST, true),
    }).subscribe({
      next: ({ url }) => this.labConfigured.set(!!url),
      error: () => this.labConfigured.set(false),
    });
  }

  refreshAll(): void {
    this.refreshModules().subscribe();
    this.refreshBackup();
    this.refreshLab();
  }
}
