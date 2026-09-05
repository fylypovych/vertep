import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterModule } from '@angular/router';
import { VertepApiService } from '../core/api.service';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, RouterModule],
  template: `
    <div class="min-h-screen flex items-center justify-center bg-slate-50">
      <div class="w-full max-w-sm bg-white rounded-xl border border-slate-200 p-6">
        <div class="flex items-center gap-2 mb-6">
          <div class="w-8 h-8 bg-emerald-500 rounded-lg flex items-center justify-center text-white font-bold">V</div>
          <span class="text-lg font-semibold text-slate-800">Vertep</span>
        </div>
        <h2 class="text-xl font-semibold text-slate-900 mb-1">Вхід</h2>
        <p class="text-sm text-slate-500 mb-6">Увійдіть до панелі адміністратора</p>

        <form [formGroup]="form" (ngSubmit)="submit()" class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Логін</label>
            <input type="text" formControlName="login" class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="form.controls['login'].touched && form.controls['login'].invalid">Обов'язкове поле</p>
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 mb-1">Пароль</label>
            <input type="password" formControlName="password" class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500" />
            <p class="text-xs text-red-600 mt-1" *ngIf="form.controls['password'].touched && form.controls['password'].invalid">Обов'язкове поле</p>
          </div>
          <div class="text-sm text-red-600" *ngIf="error">{{ error }}</div>
          <button type="submit" [disabled]="loading || form.invalid" class="w-full bg-emerald-600 text-white rounded-lg py-2 text-sm font-medium hover:bg-emerald-700 disabled:opacity-50">
            {{ loading ? 'Завантаження...' : 'Увійти' }}
          </button>
        </form>
      </div>
    </div>
  `,
})
export class LoginComponent implements OnInit {
  form: FormGroup;
  loading = false;
  error: string | null = null;

  constructor(private fb: FormBuilder, private api: VertepApiService, private router: Router) {
    this.form = this.fb.group({
      login: ['', Validators.required],
      password: ['', Validators.required],
    });
  }

  ngOnInit(): void {
    this.api.getSession().subscribe({
      next: () => this.router.navigate(['/']),
      error: () => {},
    });
  }

  submit(): void {
    if (this.form.invalid) return;
    this.loading = true;
    this.error = null;
    this.api.createSession(this.form.value).subscribe({
      next: () => this.router.navigate(['/']),
      error: (err) => {
        this.error = err.message || 'Не вдалося увійти';
        this.loading = false;
      },
    });
  }
}
