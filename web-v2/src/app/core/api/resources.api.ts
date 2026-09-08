import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { BaseApiService } from './base-api.service';
import { Character, Brand, Channel, Workflow } from '../models';

@Injectable()
export class ResourcesApiService {
  private h = () => this.base.headers();
  constructor(private http: HttpClient, private base: BaseApiService) {}
  characters = (): Observable<Character[]> => this.http.get<Character[]>(`${this.base.url}/characters`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  character = (id: string): Observable<Character> => this.http.get<Character>(`${this.base.url}/characters/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  createCharacter = (p: Character): Observable<Character> => this.http.post<Character>(`${this.base.url}/characters`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateCharacter = (id: string, p: Character): Observable<Character> => this.http.put<Character>(`${this.base.url}/characters/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteCharacter = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/characters/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  brands = (): Observable<Brand[]> => this.http.get<Brand[]>(`${this.base.url}/brands`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  createBrand = (p: { id: string; name: string; metadata?: Record<string, unknown>; publishing?: Record<string, unknown> }): Observable<Brand> => this.http.post<Brand>(`${this.base.url}/brands`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateBrand = (id: string, p: { name?: string; enabled?: boolean; metadata?: Record<string, unknown>; publishing?: Record<string, unknown> }): Observable<Brand> => this.http.put<Brand>(`${this.base.url}/brands/${this.base.enc(id)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteBrand = (id: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/brands/${this.base.enc(id)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  channels = (brandId: string): Observable<Channel[]> => this.http.get<Channel[]>(`${this.base.url}/brands/${this.base.enc(brandId)}/channels`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  channelTypes = (): Observable<string[]> => this.http.get<string[]>(`${this.base.url}/channels/types`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  createChannel = (brandId: string, p: { brand_id: string; channel_type: string; target: string; enabled?: boolean; metadata?: Record<string, unknown> }): Observable<Channel> => this.http.post<Channel>(`${this.base.url}/brands/${this.base.enc(brandId)}/channels`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  updateChannel = (channelId: string, p: { target?: string; enabled?: boolean; metadata?: Record<string, unknown> }): Observable<Channel> => this.http.put<Channel>(`${this.base.url}/channels/${this.base.enc(channelId)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteChannel = (channelId: string): Observable<{ deleted: string }> => this.http.delete<{ deleted: string }>(`${this.base.url}/channels/${this.base.enc(channelId)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflows = (): Observable<Workflow[]> => this.http.get<Workflow[]>(`${this.base.url}/workflows`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  workflow = (kind: string, name: string): Observable<Record<string, unknown>> => this.http.get<Record<string, unknown>>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  saveWorkflow = (kind: string, name: string, p: Record<string, unknown>): Observable<Record<string, unknown>> => this.http.put<Record<string, unknown>>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}`, p, { headers: this.h() }).pipe(catchError(this.base.handleError));
  deleteWorkflow = (kind: string, name: string): Observable<Record<string, unknown>> => this.http.delete<Record<string, unknown>>(`${this.base.url}/workflows/${this.base.enc(kind)}/${this.base.enc(name)}`, { headers: this.h() }).pipe(catchError(this.base.handleError));
  logo = (): Observable<Blob> => this.http.get(`${this.base.url}/settings/logo`, { responseType: 'blob' }).pipe(catchError(this.base.handleError));
  uploadLogo = (file: File): Observable<{ saved: boolean }> => { const f = new FormData(); f.append('file', file); return this.http.post<{ saved: boolean }>(`${this.base.url}/settings/logo`, f, { headers: this.h() }).pipe(catchError(this.base.handleError)); };
  deleteLogo = (): Observable<{ deleted: boolean }> => this.http.delete<{ deleted: boolean }>(`${this.base.url}/settings/logo`, { headers: this.h() }).pipe(catchError(this.base.handleError));
}
