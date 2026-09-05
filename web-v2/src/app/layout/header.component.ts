import { Component } from '@angular/core';
import { RouterModule } from '@angular/router';

@Component({
  selector: 'app-header',
  standalone: true,
  imports: [RouterModule],
  template: `
    <header class="h-16 bg-white border-b border-slate-200 flex items-center justify-between px-6">
      <div>
        <h1 class="text-xl font-semibold text-slate-800">{{ title }}</h1>
        <p class="text-sm text-slate-500">{{ subtitle }}</p>
      </div>
      <div class="flex items-center gap-4">
        <div class="flex items-center gap-2 px-3 py-1.5 bg-emerald-50 text-emerald-700 rounded-full text-sm font-medium">
          <span class="w-2 h-2 bg-emerald-500 rounded-full"></span>
          Нормальний
        </div>
        <button class="p-2 text-slate-400 hover:text-slate-600">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/></svg>
        </button>
        <div class="flex items-center gap-2">
          <div class="w-8 h-8 bg-emerald-100 rounded-full flex items-center justify-center text-emerald-700 font-semibold text-sm">A</div>
          <span class="text-sm font-medium text-slate-700">Admin</span>
        </div>
      </div>
    </header>
  `,
})
export class HeaderComponent {
  title = 'Дашборд';
  subtitle = 'Огляд вашої системи Vertep';
}
