import { Injectable } from '@angular/core';
import { BehaviorSubject } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly STORAGE_KEY = 'vertep_theme';
  private darkSubject: BehaviorSubject<boolean>;

  get dark$() { return this.darkSubject.asObservable(); }
  get isDark(): boolean { return this.darkSubject.value; }

  constructor() {
    const stored = localStorage.getItem(this.STORAGE_KEY);
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const isDark = stored !== null ? stored === 'dark' : prefersDark;
    this.darkSubject = new BehaviorSubject<boolean>(isDark);
    this.apply(isDark);
  }

  toggle(): void {
    this.set(!this.darkSubject.value);
  }

  set(dark: boolean): void {
    this.darkSubject.next(dark);
    localStorage.setItem(this.STORAGE_KEY, dark ? 'dark' : 'light');
    this.apply(dark);
  }

  private apply(dark: boolean): void {
    const html = document.documentElement;
    if (dark) {
      html.classList.add('dark');
    } else {
      html.classList.remove('dark');
    }
  }
}
