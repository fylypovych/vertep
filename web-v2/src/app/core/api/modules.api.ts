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
