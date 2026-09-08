import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { Worker, NodeDetail, RegistrationTokenResponse, NodeActionPayload, NodeRegisterResponse } from '../models';

@Injectable()
export class WorkersApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}

  list(): Observable<Worker[]> {
    return this.http.get<Worker[]>(`${this.base.url}/workers`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  health(): Observable<Record<string, unknown>[]> {
    return this.http.get<Record<string, unknown>[]>(`${this.base.url}/workers/health`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  heartbeat(payload: { node_name: string; vram_mb?: number; [k: string]: unknown }): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base.url}/workers/heartbeat`, payload, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  getNode(nodeId: string): Observable<NodeDetail> {
    return this.http.get<NodeDetail>(`${this.base.url}/nodes/${this.base.enc(nodeId)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  nodeStatus(nodeName: string): Observable<Record<string, unknown>> {
    return this.http.get<Record<string, unknown>>(`${this.base.url}/node/status/${this.base.enc(nodeName)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  generateToken(role: string): Observable<RegistrationTokenResponse> {
    return this.http.post<RegistrationTokenResponse>(`${this.base.url}/nodes/registration-tokens`, { role }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  action(nodeId: string, payload: NodeActionPayload): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base.url}/nodes/${this.base.enc(nodeId)}/actions`, payload, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  revoke(nodeId: string): Observable<Record<string, unknown>> {
    return this.http.post<Record<string, unknown>>(`${this.base.url}/nodes/${this.base.enc(nodeId)}/revoke`, {}, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
  renew(nodeId: string, csr: string): Observable<NodeRegisterResponse> {
    return this.http.post<NodeRegisterResponse>(`${this.base.url}/nodes/${this.base.enc(nodeId)}/renew`, { csr }, { headers: this.h() }).pipe(catchError(this.base.handleError));
  }
}
