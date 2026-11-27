import { Injectable, signal } from '@angular/core';

export type UiVersion = 'v2' | 'v3';
const STORAGE_KEY = 'vertep_ui_version';

export interface MappedRoute {
  path: string;
  queryParams?: Record<string, string>;
  fragment?: string;
}

/**
 * Stores the user's preferred panel version (v2 vs v3) and maps routes bidirectionally
 * so switching versions preserves the current workspace and deep links.
 */
@Injectable({ providedIn: 'root' })
export class UiVersionService {
  readonly version = signal<UiVersion>(this.readStored());

  setVersion(version: UiVersion): void {
    this.version.set(version);
    try {
      localStorage.setItem(STORAGE_KEY, version);
    } catch {
      /* private mode */
    }
  }

  private readStored(): UiVersion {
    try {
      return localStorage.getItem(STORAGE_KEY) === 'v3' ? 'v3' : 'v2';
    } catch {
      return 'v2';
    }
  }

  parseUrl(rawUrl: string): { path: string; params: Record<string, string>; fragment?: string } {
    const [withoutHash, fragment] = rawUrl.split('#');
    const [rawPath, rawQuery] = withoutHash.split('?');
    const path = (rawPath || '/').replace(/\/+$/, '') || '/';
    const params: Record<string, string> = {};
    if (rawQuery) {
      const sp = new URLSearchParams(rawQuery);
      sp.forEach((value, key) => { params[key] = value; });
    }
    return { path, params, fragment: fragment || undefined };
  }

  /** Maps any v2 URL (or already-v3 URL) to the canonical v3 route. */
  toV3Route(rawUrl: string): MappedRoute {
    const { path, params, fragment } = this.parseUrl(rawUrl);
    if (path === '/v3' || path.startsWith('/v3/')) {
      return { path, queryParams: Object.keys(params).length ? params : undefined, fragment };
    }
    if (path === '/' || path === '') return { path: '/v3', fragment };
    if (path === '/queue') return { path: '/v3/jobs', queryParams: { ...params, tab: 'queue' }, fragment };
    if (path === '/health') return { path: '/v3/monitor', queryParams: { tab: 'health' }, fragment };
    if (path === '/alerts' || path === '/operations') return { path: '/v3/monitor', queryParams: { tab: 'alerts' }, fragment };
    if (path === '/logs') return { path: '/v3/monitor', queryParams: { tab: 'logs' }, fragment };
    if (path === '/real-tests') return { path: '/v3/monitor', queryParams: { tab: 'real-tests' }, fragment };
    if (path === '/workflows') return { path: '/v3/pipeline', queryParams: { tab: 'workflows' }, fragment };
    if (path === '/modules') return { path: '/v3/modules', queryParams: { tab: 'catalog' }, fragment };

    if (path === '/settings') {
      const tab = params['tab'] || 'system';
      if (tab === 'integrations') return { path: '/v3/monitor', queryParams: { tab: 'integrations' }, fragment };
      if (tab === 'update') return { path: '/v3/modules', queryParams: { tab: 'update' }, fragment };
      if (tab === 'backup') return { path: '/v3/modules', queryParams: { tab: 'backup' }, fragment };
      if (tab === 'models' || tab === 'video-engine' || tab === 'workflow-mode') {
        return { path: '/v3/pipeline', queryParams: { tab }, fragment };
      }
      if (tab === 'users') return { path: '/v3/settings', queryParams: { tab: 'users' }, fragment };
      if (tab === 'security') return { path: '/v3/settings', queryParams: { tab: 'security' }, fragment };
      if (tab === 'publishing') {
        return { path: '/v3/settings', queryParams: { tab: 'publishing' }, fragment };
      }
      if (tab === 'telegram' || tab === 'secrets') {
        return { path: '/v3/settings', queryParams: { tab: 'integrations', section: tab }, fragment };
      }
      return { path: '/v3/settings', queryParams: { tab: 'general' }, fragment };
    }

    return {
      path: `/v3${path}`,
      queryParams: Object.keys(params).length ? params : undefined,
      fragment,
    };
  }

  /** Maps any v3 URL back to the equivalent v2 route. */
  toV2Route(rawUrl: string): MappedRoute {
    const { path, params, fragment } = this.parseUrl(rawUrl);
    if (path !== '/v3' && !path.startsWith('/v3/')) {
      return { path, queryParams: Object.keys(params).length ? params : undefined, fragment };
    }
    const sub = path.slice(3) || '/';
    if (sub === '/') return { path: '/', fragment };
    if (sub === '/jobs' && params['tab'] === 'queue') return { path: '/queue', fragment };

    if (sub === '/monitor') {
      const tab = params['tab'] || 'health';
      if (tab === 'alerts') return { path: '/alerts', fragment };
      if (tab === 'logs') return { path: '/logs', fragment };
      if (tab === 'real-tests') return { path: '/real-tests', fragment };
      if (tab === 'integrations') return { path: '/settings', queryParams: { tab: 'integrations' }, fragment };
      return { path: '/health', fragment };
    }

    if (sub === '/modules') {
      const tab = params['tab'] || 'catalog';
      if (tab === 'update') return { path: '/settings', queryParams: { tab: 'update' }, fragment };
      if (tab === 'backup') return { path: '/settings', queryParams: { tab: 'backup' }, fragment };
      return { path: '/modules', fragment };
    }

    if (sub === '/pipeline') {
      const tab = params['tab'] || 'workflows';
      if (tab === 'models' || tab === 'video-engine' || tab === 'workflow-mode') {
        return { path: '/settings', queryParams: { tab }, fragment };
      }
      return { path: '/workflows', fragment };
    }

    if (sub === '/settings') {
      const tab = params['tab'] || 'general';
      if (tab === 'users') return { path: '/settings', queryParams: { tab: 'users' }, fragment };
      if (tab === 'security') return { path: '/settings', queryParams: { tab: 'security' }, fragment };
      if (tab === 'integrations') {
        const section = params['section'] || 'telegram';
        return { path: '/settings', queryParams: { tab: section }, fragment };
      }
      return { path: '/settings', queryParams: { tab: 'system' }, fragment };
    }

    return {
      path: sub,
      queryParams: Object.keys(params).length ? params : undefined,
      fragment,
    };
  }
}
