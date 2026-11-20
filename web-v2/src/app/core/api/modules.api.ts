import { Injectable } from '@angular/core';
import { HttpClient, HttpContext } from '@angular/common/http';
import { Observable, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import { BACKGROUND_REQUEST } from '../interceptors/auth.interceptor';
import { BaseApiService } from './base-api.service';

export interface ModuleUiExtension {
  module_id: string;
  version: string;
  url: string;
  navigation: { id: string; label?: string }[];
  /** The module's own Gateway API; the only place the panel relays its UI's requests to. */
  api_base?: string | null;
}

/** Signed module UI extensions (#493). A failure yields no extensions, never a broken panel. */
@Injectable({ providedIn: 'root' })
export class ModulesApiService {
  constructor(private http: HttpClient, private base: BaseApiService) {}

  uiExtensions = (): Observable<ModuleUiExtension[]> =>
    this.http.get<{ extensions: ModuleUiExtension[] }>(`${this.base.url}/modules/ui-extensions`,
      { headers: this.base.headers(), context: new HttpContext().set(BACKGROUND_REQUEST, true) })
      .pipe(map(response => Array.isArray(response?.extensions) ? response.extensions : []), catchError(() => of([])));

  /** Status of the extension entry; the bytes are digest-checked by CORE before serving. */
  entryAvailable = (url: string): Observable<boolean> =>
    this.http.get(url, { responseType: 'text', context: new HttpContext().set(BACKGROUND_REQUEST, true) })
      .pipe(map(() => true), catchError(() => of(false)));
}

export interface CatalogNodeEntry {
  node_id: string;
  compatible_reasons: string[];
  runtime_missing: boolean;
  instance: { state: string; reasons: string[]; version: string } | null;
}

export interface CatalogModule {
  module_id: string; label: string; version: string; kind: string; bundled: boolean; state: string; runtime_role: string | null;
  requires_gpu: boolean; target: string; scaling: string; resources: Record<string, number>;
  dependencies: unknown[]; permissions: string[]; approved_permissions: string[]; placement: string[];
  nodes: CatalogNodeEntry[];
}

export interface CatalogNode { node_id: string; role?: string; gpu_available?: boolean; vram_mb?: number; environment: string; isolation: string; }

/** Administrator module management (#496): one choice — module and node. */
@Injectable({ providedIn: 'root' })
export class ModuleManagementApiService {
  constructor(private http: HttpClient, private base: BaseApiService) {}
  private h = () => this.base.headers();
  catalog = (): Observable<{ modules: CatalogModule[]; nodes: CatalogNode[] }> =>
    this.http.get<{ modules: CatalogModule[]; nodes: CatalogNode[] }>(`${this.base.url}/nodes/module-catalog`, { headers: this.h() })
      .pipe(catchError(this.base.handleError));
  lifecycle = (action: string, body: Record<string, unknown>): Observable<unknown> =>
    this.http.post(`${this.base.url}/nodes/module-registry/lifecycle/${action}`, body, { headers: this.h() })
      .pipe(catchError(this.base.handleError));
  placement = (moduleId: string, nodeIds: string[]): Observable<unknown> =>
    this.http.put(`${this.base.url}/nodes/module-registry/placement/${this.base.enc(moduleId)}`, { node_ids: nodeIds }, { headers: this.h() })
      .pipe(catchError(this.base.handleError));
  approve = (moduleId: string, permissions: string[]): Observable<unknown> =>
    this.http.put(`${this.base.url}/nodes/module-registry/approval/${this.base.enc(moduleId)}`, { permissions }, { headers: this.h() })
      .pipe(catchError(this.base.handleError));
}

export interface CoreRuntimeState { active_roles: string[]; queued: boolean; requested_roles: string[] | null;
  deployment: { state?: string | null; phase?: string | null; error?: string | null; stalled?: boolean; message?: string | null } | null; }

/** Runtime of the central machine, prepared from the module choice (legacy role executor underneath). */
@Injectable({ providedIn: 'root' })
export class CoreRuntimeApiService {
  constructor(private http: HttpClient, private base: BaseApiService) {}
  state = (): Observable<CoreRuntimeState> =>
    this.http.get<CoreRuntimeState>(`${this.base.url}/system/roles`, { headers: this.base.headers() }).pipe(catchError(this.base.handleError));
  prepare = (roles: string[]): Observable<unknown> =>
    this.http.post(`${this.base.url}/system/roles`, { roles }, { headers: this.base.headers() }).pipe(catchError(this.base.handleError));
}
