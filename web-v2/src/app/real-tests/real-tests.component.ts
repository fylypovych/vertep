import { Component, signal, computed } from '@angular/core';
import { finalize } from 'rxjs';

import { RealTestsApiService, RealTestReport, RealTestScenario, RealTestRunSummary } from '../core/api/real-tests.api';

type Tab = 'scenarios' | 'history';

@Component({
  selector: 'app-real-tests',
  standalone: true,
  imports: [],
  templateUrl: './real-tests.component.html',
  styleUrl: './real-tests.component.css',
})
export class RealTestsComponent {
  tab = signal<Tab>('scenarios');
  scenarios = signal<RealTestScenario[]>([]);
  runs = signal<RealTestRunSummary[]>([]);
  loading = signal(true);
  error = signal<string | null>(null);
  running = signal<Record<string, boolean>>({});
  confirmDialog = signal<{ rtId: string; name: string; destructive: boolean } | null>(null);
  report = signal<RealTestReport | null>(null);
  selectedRunId = signal<string | null>(null);

  constructor(private api: RealTestsApiService) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.scenarios().subscribe({
      next: (scenariosResponse) => {
        this.scenarios.set(scenariosResponse.scenarios);
        this.api.listRuns(50).subscribe({
          next: (runsResponse) => {
            this.runs.set(runsResponse.runs);
            this.loading.set(false);
          },
          error: (err) => {
            this.error.set(err?.error?.detail || err?.message || 'Помилка завантаження історії');
            this.loading.set(false);
          },
        });
      },
      error: (err) => {
        this.error.set(err?.error?.detail || err?.message || 'Помилка завантаження сценаріїв');
        this.loading.set(false);
      },
    });
  }

  destructiveFor(rtId: string): boolean {
    return this.scenarios().some((s) => s.id === rtId && s.checks.length > 0);
  }

  requestRun(scenario: RealTestScenario): void {
    this.confirmDialog.set({
      rtId: scenario.rt_id || scenario.id,
      name: scenario.name,
      destructive: this.destructiveFor(scenario.rt_id || scenario.id),
    });
  }

  confirmRun(): void {
    const dialog = this.confirmDialog();
    this.confirmDialog.set(null);
    if (!dialog) {
      return;
    }
    this.running.update((map) => ({ ...map, [dialog.rtId]: true }));
    this.error.set(null);
    this.api
      .prerequisites(dialog.rtId)
      .pipe(finalize(() => this.running.update((map) => ({ ...map, [dialog.rtId]: false }))))
      .subscribe({
        next: (prereq) => {
          this.api
            .run(dialog.rtId, null, true)
            .subscribe({
              next: () => this.load(),
              error: (err) => this.error.set(err?.error?.detail || err?.message || 'Помилка запуску'),
            });
        },
        error: (err) => this.error.set(err?.error?.detail || err?.message || 'Помилка перевірки передумов'),
      });
  }

  cancelDialog(): void {
    this.confirmDialog.set(null);
  }

  viewReport(run: RealTestRunSummary): void {
    this.selectedRunId.set(run.test_run_id);
    this.report.set(null);
    this.api.getRun(run.test_run_id).subscribe({
      next: (report) => this.report.set(report),
      error: (err) => this.error.set(err?.error?.detail || err?.message || 'Помилка завантаження звіту'),
    });
  }

  retryReport(run: RealTestRunSummary): void {
    this.api.retryReport(run.test_run_id).subscribe({
      next: () => this.viewReport(run),
      error: (err) => this.error.set(err?.error?.detail || err?.message || 'Помилка повторної відправки'),
    });
  }

  closeReport(): void {
    this.selectedRunId.set(null);
    this.report.set(null);
  }

  statusClass(status: string): string {
    switch (status) {
      case 'PASS':
      case 'REPORTED':
        return 'text-emerald-600';
      case 'FAIL':
      case 'REPORT_PENDING':
        return 'text-amber-600';
      default:
        return 'text-slate-500';
    }
  }

  resultLabel(result: string): string {
    return {
      PASS: 'Пройдено',
      FAIL: 'Невдалося',
      PROCEDURE: 'Не переведено',
    }[result] ?? result;
  }

  tabCount = computed(() => this.scenarios().length + this.runs().length);
}