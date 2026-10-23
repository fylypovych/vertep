import { Component, signal, computed } from '@angular/core';

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
  runProgress = signal<Record<string, string>>({});
  confirmDialog = signal<{ rtId: string; name: string; destructive: boolean; checks?: string[] } | null>(null);
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

  requestRun(scenario: RealTestScenario): void {
    const rtId = scenario.rt_id || scenario.id;
    this.running.update((map) => ({ ...map, [rtId]: true }));
    this.api.prerequisites(rtId).subscribe({
      next: (prereq) => {
        this.running.update((map) => ({ ...map, [rtId]: false }));
        this.confirmDialog.set({
          rtId,
          name: scenario.name,
          destructive: prereq.requires_confirmation,
          checks: prereq.destructive_checks,
        });
      },
      error: (err) => {
        this.running.update((map) => ({ ...map, [rtId]: false }));
        this.error.set(err?.error?.detail || err?.message || 'Помилка перевірки передумов');
      },
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
      .run(dialog.rtId, null, true)
      .subscribe({
        next: (runResponse) => {
          this.runProgress.update((progress) => ({
            ...progress,
            [dialog.rtId]: runResponse.progress || 'Запуск перевірок…',
          }));
          this.pollRunCompletion(dialog.rtId, runResponse.test_run_id);
        },
        error: (err) => {
          this.error.set(err?.error?.detail || err?.message || 'Помилка запуску');
          this.running.update((map) => ({ ...map, [dialog.rtId]: false }));
        },
      });
  }

  private pollRunCompletion(rtId: string, testRunId: string): void {
    let attempts = 0;
    const maxAttempts = 240;
    const poll = () => {
      attempts += 1;
      this.api.getRunProgress(testRunId).subscribe({
        next: (progress) => {
          this.runProgress.update((current) => ({
            ...current,
            [rtId]: progress.progress || progress.status,
          }));
          const terminal = progress.status !== 'RUNNING';
          if (terminal || attempts >= maxAttempts) {
            this.running.update((map) => ({ ...map, [rtId]: false }));
            this.load();
            return;
          }
          setTimeout(poll, 250);
        },
        error: () => {
          if (attempts >= maxAttempts) {
            this.running.update((map) => ({ ...map, [rtId]: false }));
            return;
          }
          setTimeout(poll, 250);
        },
      });
    };
    setTimeout(poll, 250);
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

  retryReport(): void {
    const runId = this.selectedRunId();
    if (!runId) {
      return;
    }
    this.api.retryReport(runId).subscribe({
      next: () => {
        const run = this.runs().find((r) => r.test_run_id === runId);
        if (run) {
          this.viewReport(run);
        }
      },
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