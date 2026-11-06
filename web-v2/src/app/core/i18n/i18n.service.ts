import { Injectable, signal } from '@angular/core';
import { EN, EN_PATTERNS } from './en';

export type Language = 'uk' | 'en';
const STORAGE_KEY = 'vertep_language';

/**
 * The Ukrainian text is its own key: a missing English entry shows Ukrainian, so a technical
 * key can never reach the page. The choice lives in this browser; the first visit is Ukrainian.
 */
@Injectable({ providedIn: 'root' })
export class I18nService {
  readonly language = signal<Language>(this.stored());

  constructor() { this.applyToDocument(); }

  set(language: Language): void {
    this.language.set(language);
    try { localStorage.setItem(STORAGE_KEY, language); } catch { /* private window: this visit only */ }
    this.applyToDocument();
  }

  t(text: string): string {
    if (this.language() !== 'en') return text;
    if (EN[text] !== undefined) return EN[text];
    const pattern = EN_PATTERNS.find(([from]) => from.test(text));
    return pattern ? text.replace(pattern[0], pattern[1]) : text;
  }

  get locale(): string { return this.language() === 'en' ? 'en-US' : 'uk-UA'; }

  private stored(): Language {
    try { return localStorage.getItem(STORAGE_KEY) === 'en' ? 'en' : 'uk'; } catch { return 'uk'; }
  }

  private applyToDocument(): void { document.documentElement.lang = this.language(); }
}
