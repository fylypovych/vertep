import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { OwnerTokenService } from './owner-token.service';

const BASE = '/api/lab';

/** Thin client of the one laboratory backend — no permission logic here. */
@Injectable({ providedIn: 'root' })
export class LabApiService {
  private readonly http = inject(HttpClient);
  private readonly owner = inject(OwnerTokenService);

  status(): Observable<Record<string, any>> {
    return this.http.get<Record<string, any>>(`${BASE}/status`);
  }

  auditRecords(limit = 200): Observable<{ records: Record<string, any>[] }> {
    return this.http.get<{ records: Record<string, any>[] }>(
      `${BASE}/audit/records`, { params: { limit } });
  }

  issues(): Observable<{ created: Record<string, any>[]; closed: Record<string, any> }> {
    return this.http.get<{ created: Record<string, any>[]; closed: Record<string, any> }>(
      `${BASE}/issues`);
  }

  evidence(): Observable<{ evidence: Record<string, any>[] }> {
    return this.http.get<{ evidence: Record<string, any>[] }>(`${BASE}/evidence`);
  }

  grants(): Observable<{ grants: Record<string, any>[] }> {
    return this.http.get<{ grants: Record<string, any>[] }>(`${BASE}/grants`);
  }

  rightsRequests(): Observable<{ requests: Record<string, any>[] }> {
    return this.http.get<{ requests: Record<string, any>[] }>(`${BASE}/rights-requests`);
  }

  transfers(): Observable<Record<string, any>> {
    return this.http.get<Record<string, any>>(`${BASE}/transfers`);
  }

  gpu(): Observable<Record<string, any>> {
    return this.http.get<Record<string, any>>(`${BASE}/gpu`);
  }

  notifications(): Observable<Record<string, Record<string, boolean>>> {
    return this.http.get<Record<string, Record<string, boolean>>>(
      `${BASE}/notifications`);
  }

  saveNotifications(settings: Record<string, Record<string, boolean>>):
      Observable<Record<string, any>> {
    return this.http.put<Record<string, any>>(`${BASE}/notifications`,
      { settings, reason: 'оновлено з Web-панелі лабораторії' },
      { headers: this.owner.headers() });
  }

  reports(sections: string[]): Observable<Record<string, any>> {
    return this.http.get<Record<string, any>>(`${BASE}/reports`,
      { params: { sections: sections.join(',') } });
  }

  revokeGrant(grantId: string): Observable<Record<string, any>> {
    return this.http.post<Record<string, any>>(
      `${BASE}/grants/${grantId}/revoke`, {}, { headers: this.owner.headers() });
  }

  submitEvidence(identifier: string, evidence: Record<string, any>):
      Observable<Record<string, any>> {
    return this.http.post<Record<string, any>>(`${BASE}/ir/evidence`,
      { identifier, evidence });
  }

  closeIr(identifier: string, evidence: Record<string, any>):
      Observable<Record<string, any>> {
    return this.http.post<Record<string, any>>(`${BASE}/ir/close`,
      { identifier, evidence }, { headers: this.owner.headers() });
  }

  consolidate(source: Record<string, any>, successors: Record<string, any>[],
              reason: string): Observable<Record<string, any>> {
    return this.http.post<Record<string, any>>(`${BASE}/ir/consolidate`,
      { source, successors, reason }, { headers: this.owner.headers() });
  }

  probe(kind: string, subject = ''): Observable<Record<string, any>> {
    return this.http.get<Record<string, any>>(`${BASE}/issues/${kind}`,
      { params: { subject } });
  }
}
