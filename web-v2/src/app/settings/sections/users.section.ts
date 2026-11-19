import { TPipe } from '../../core/i18n/t.pipe';
import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { catchError } from 'rxjs';
import { BaseApiService } from '../../core/api/base-api.service';
import { ResourcesApiService } from '../../core/api/resources.api';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmService } from '../../core/services/confirm.service';

interface AccessUser {
  user: string;
  role: 'admin' | 'moderator';
  brands: string[] | null;
  display_name: string | null;
  email: string | null;
  editable: boolean;
}

/** Settings → Користувачі: the administrator manages moderators and their brands. */
@Component({
  selector: 'app-settings-users',
  standalone: true,
  imports: [TPipe, CommonModule, FormsModule],
  template: `<div class="bg-white rounded-xl border border-slate-200 p-5 space-y-4" data-testid="settings-users">
    <div>
      <h3 class="text-lg font-semibold mb-1">{{ 'Користувачі' | t }}</h3>
      <p class="text-sm text-slate-500">{{ 'Адміністратор має повний доступ. Модератор працює лише з обраними брендами.' | t }}</p>
    </div>
    <table class="w-full text-sm">
      <thead><tr class="text-left text-slate-500">
        <th class="py-1">{{ 'Логін' | t }}</th><th>{{ 'Роль' | t }}</th><th>{{ 'Бренди' | t }}</th><th></th>
      </tr></thead>
      <tbody>
        @for (item of users(); track item.user) {
          <tr class="border-t border-slate-100" [attr.data-testid]="'user-' + item.user">
            <td class="py-2 font-mono">{{ item.user }}</td>
            <td>
              <select class="border rounded px-1 py-0.5" [disabled]="!item.editable" [ngModel]="item.role"
                      (ngModelChange)="update(item, { role: $event })" [attr.data-testid]="'user-role-' + item.user">
                <option value="admin">{{ 'Адміністратор' | t }}</option>
                <option value="moderator">{{ 'Модератор' | t }}</option>
              </select>
            </td>
            <td>
              @if (item.role === 'moderator') {
                <div class="flex flex-wrap gap-2">
                  @for (brand of brandIds(); track brand) {
                    <label class="inline-flex items-center gap-1">
                      <input type="checkbox" [disabled]="!item.editable" [checked]="(item.brands || []).includes(brand)"
                             (change)="toggleBrand(item, brand)" [attr.data-testid]="'user-brand-' + item.user + '-' + brand">{{ brand }}
                    </label>
                  }
                </div>
              } @else { <span class="text-slate-400">{{ 'усі' | t }}</span> }
            </td>
            <td class="text-right whitespace-nowrap">
              @if (item.editable) {
                <button class="text-blue-600 text-xs mr-2" (click)="resetPassword(item)">{{ 'Новий пароль' | t }}</button>
                <button class="text-red-600 text-xs" (click)="remove(item)">{{ 'Видалити' | t }}</button>
              }
            </td>
          </tr>
        }
      </tbody>
    </table>
    <div class="border-t border-slate-100 pt-4 grid grid-cols-1 md:grid-cols-4 gap-2 items-end" data-testid="user-create">
      <input class="border rounded px-2 py-1" [(ngModel)]="form.user" placeholder="{{ 'Логін' | t }}" data-testid="user-create-login">
      <select class="border rounded px-2 py-1" [(ngModel)]="form.role">
        <option value="moderator">{{ 'Модератор' | t }}</option>
        <option value="admin">{{ 'Адміністратор' | t }}</option>
      </select>
      <input class="border rounded px-2 py-1" type="password" [(ngModel)]="form.password" placeholder="{{ 'Пароль (від 12 символів)' | t }}" data-testid="user-create-password">
      <button class="px-3 py-1.5 bg-emerald-600 text-white rounded" (click)="create()" data-testid="user-create-button">{{ 'Додати користувача' | t }}</button>
      @if (form.role === 'moderator') {
        <div class="md:col-span-4 flex flex-wrap gap-2 text-sm">
          <span class="text-slate-500">{{ 'Бренди' | t }}:</span>
          @for (brand of brandIds(); track brand) {
            <label class="inline-flex items-center gap-1">
              <input type="checkbox" [checked]="form.brands.includes(brand)" (change)="toggleFormBrand(brand)">{{ brand }}
            </label>
          }
        </div>
      }
    </div>
  </div>`,
})
export class UsersSectionComponent implements OnInit {
  users = signal<AccessUser[]>([]);
  brandIds = signal<string[]>([]);
  form = { user: '', role: 'moderator' as 'admin' | 'moderator', password: '', brands: [] as string[] };

  constructor(private http: HttpClient, private base: BaseApiService, private resources: ResourcesApiService,
              private toast: ToastService, private confirm: ConfirmService) {}

  ngOnInit(): void {
    this.load();
    this.resources.brands().subscribe({ next: (items) => this.brandIds.set(items.map((item) => item.id)), error: () => this.brandIds.set([]) });
  }

  private url(path = ''): string { return `${this.base.url}/settings/users${path}`; }

  load(): void {
    this.http.get<{ users: AccessUser[] }>(this.url(), { headers: this.base.headers() }).pipe(catchError(this.base.handleError))
      .subscribe({ next: (data) => this.users.set(data.users), error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
  }

  update(item: AccessUser, body: Record<string, unknown>): void {
    this.http.put<AccessUser>(this.url(`/${encodeURIComponent(item.user)}`), body, { headers: this.base.headers() })
      .pipe(catchError(this.base.handleError))
      .subscribe({ next: () => this.load(), error: (error) => { this.toast.show(error?.message || 'Помилка', 'error'); this.load(); } });
  }

  toggleBrand(item: AccessUser, brand: string): void {
    const current = new Set(item.brands || []);
    current.has(brand) ? current.delete(brand) : current.add(brand);
    this.update(item, { brands: [...current].sort() });
  }

  toggleFormBrand(brand: string): void {
    this.form.brands = this.form.brands.includes(brand) ? this.form.brands.filter((b) => b !== brand) : [...this.form.brands, brand];
  }

  resetPassword(item: AccessUser): void {
    const password = prompt('Новий пароль (від 12 символів)');
    if (password) this.update(item, { password });
  }

  remove(item: AccessUser): void {
    this.confirm.confirm({ title: 'Видалити користувача', message: `Видалити користувача ${item.user}?` }).subscribe((ok) => {
      if (!ok) return;
      this.http.delete(this.url(`/${encodeURIComponent(item.user)}`), { headers: this.base.headers() }).pipe(catchError(this.base.handleError))
        .subscribe({ next: () => this.load(), error: (error) => this.toast.show(error?.message || 'Помилка', 'error') });
    });
  }

  create(): void {
    const body = { user: this.form.user.trim(), role: this.form.role, password: this.form.password,
                   brands: this.form.role === 'moderator' ? this.form.brands : undefined };
    this.http.post<AccessUser>(this.url(), body, { headers: this.base.headers() }).pipe(catchError(this.base.handleError))
      .subscribe({
        next: () => { this.form = { user: '', role: 'moderator', password: '', brands: [] }; this.load(); },
        error: (error) => this.toast.show(error?.message || 'Помилка', 'error'),
      });
  }
}
