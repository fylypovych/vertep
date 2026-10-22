Workflow DAG Contract
====================

1. Format opisiu workflow (YAML/JSON)
-----------------------
- Workflow описується у форматі YAML або JSON.
- Кореневий об'єкт містить: `name`, `version`, `description`, `tasks`.
- Завдання описуються масивом `tasks`, кожне завдання має унікальний `id`.

2. Структура task definition (input, output, dependencies, timeout, retry)
---------------------------------
```yaml
tasks:
  - id: "task_1"
    name: "Generate script"
    type: "llm_generation"
    input:
      prompt: "Generate a short video script about..."
      character_config: "..."
    output:
      type: "script"
      format: "markdown"
    dependencies: []  # IDs of tasks that must complete first
    timeout_minutes: 30
    retry:
      max_attempts: 3
      delay_seconds: 5
```

3. Контракт стану workflow (job status, task progress, approval state)
---------------------------------
- `job_status`: "NEW", "IN_PROGRESS", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"
- `task_progress`: mapping task_id → {status: "PENDING"|"RUNNING"|"COMPLETED"|"FAILED", progress_percent: 0-100, output: any}
- `approval_state`: "PENDING", "APPROVED", "REJECTED" — для завдань, що вимагають אישії

4. Контракт для workflow engine API (create, start, pause, resume, cancel, query)
---------------------------------
- `create_job(definition_id, params)` → job_id
- `start_job(job_id)` → status
- `pause_job(job_id)` → status
- `resume_job(job_id)` → status
- `cancel_job(job_id)` → status
- `get_job_status(job_id)` → {status, tasks, progress, artifacts, error}

5. Edge cases
-----------
- Cyclic dependencies: workflow creation must fail with clear error.
- Orphaned dependencies: task references non-existent dependency ID.
- Infinite retry: max_attempts limit enforced.
- Approval required: workflow cannot start without approval if `approval_required: true`.