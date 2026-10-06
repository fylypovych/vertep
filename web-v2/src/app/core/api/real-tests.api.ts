import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, throwError } from 'rxjs';

export interface RealTestScenario {
  id: string;
  name: string;
  rt_id: string;
  rt_issue: number | null;
  description: string;
  checks: string[];
}

export interface RealTestScenariosResponse {
  scenarios: RealTestScenario[];
  available_checks: string[];
  version: string;
}

export interface RealTestPrerequisites {
  rt_id: string;
  name: string;
  description: string;
  checks: string[];
  mandatory_checks: string[];
  destructive_checks: string[];
  requires_confirmation: boolean;
}

export interface RealTestRunSummary {
  test_run_id: string;
  rt_id: string;
  rt_issue_number: number | null;
  version: string;
  commit_sha: string;
  status: string;
  final_result: string;
  started_at: string;
  finished_at: string | null;
  initiator: string;
}

export interface RealTestRunsResponse {
  runs: RealTestRunSummary[];
}

export interface RealTestReport {
  test_run_id: string;
  rt_id: string;
  status: string;
  final_result: string;
  initiator: string;
  version: string;
  commit_sha: string;
  checks: Array<{ name: string; status: string; detail: string; mandatory: boolean }>;
  github_report: Record<string, unknown> | null;
  error_details: string | null;
}

export interface RealTestRunResponse {
  test_run_id: string;
  status: string;
  result: string;
  rt_id: string;
}

export interface RealTestRetryResponse {
  test_run_id: string;
  status: string;
  github_report: Record<string, unknown> | null;
}

@Injectable({ providedIn: 'root' })
export class RealTestsApiService {
  private baseUrl = '/api';

  constructor(private http: HttpClient) {}

  private getHeaders(): Record<string, string> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const csrf = document.cookie
      .split('; ')
      .find(x => x.startsWith('vertep_csrf='))
      ?.split('=')[1];
    if (csrf) {
      headers['X-CSRF-Token'] = csrf;
    }
    return headers;
  }

  private handleError(err: unknown) {
    return throwError(() => err);
  }

  scenarios(): Observable<RealTestScenariosResponse> {
    return this.http.get<RealTestScenariosResponse>(
      `${this.baseUrl}/real-tests/scenarios`,
      { headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }

  prerequisites(rtId: string): Observable<RealTestPrerequisites> {
    return this.http.get<RealTestPrerequisites>(
      `${this.baseUrl}/real-tests/scenarios/${encodeURIComponent(rtId)}/prerequisites`,
      { headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }

  run(rtId: string, checkNames: string[] | null, confirmDestructive: boolean): Observable<RealTestRunResponse> {
    return this.http.post<RealTestRunResponse>(
      `${this.baseUrl}/real-tests/run`,
      { rt_id: rtId, check_names: checkNames, confirm_destructive: confirmDestructive },
      { headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }

  listRuns(limit = 50): Observable<RealTestRunsResponse> {
    return this.http.get<RealTestRunsResponse>(
      `${this.baseUrl}/real-tests/runs`,
      { params: { limit }, headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }

  getRun(testRunId: string): Observable<RealTestReport> {
    return this.http.get<RealTestReport>(
      `${this.baseUrl}/real-tests/runs/${encodeURIComponent(testRunId)}`,
      { headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }

  retryReport(testRunId: string): Observable<RealTestRetryResponse> {
    return this.http.post<RealTestRetryResponse>(
      `${this.baseUrl}/real-tests/runs/${encodeURIComponent(testRunId)}/retry-report`,
      {},
      { headers: this.getHeaders() },
    ).pipe(catchError(this.handleError));
  }
}