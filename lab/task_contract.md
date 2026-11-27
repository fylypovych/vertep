# Шаблон задачі лабораторії (Task Contract)

> **Обов’язкове правило LAB:** перед запуском будь-якої нової задачі зі зміною коду агент **спершу** заповнює всі 4 пункти цього контракту й проходить перевірку (`lab.cli contract validate <файл>` або `POST /api/lab/task-contract/validate`).
> **Тільки після заповнення та валідації всіх 4 пунктів дозволено починати писати код.**
> Оркестратор LAB (`lab/orchestrator.py`), API (`POST /api/lab/tasks`) і CLI (`lab.cli enqueue`) блокують запуск задач зміни коду без валідного `Task Contract` (fail-closed).

---

## 1. Що змінюємо (файли)
<!-- Перелік відносних шляхів файлів у репозиторії (без абсолютних шляхів і `..`). -->
- `<path/to/file.py>`

## 2. Які інваріанти не можна зламати
<!-- Конкретні архітектурні, безпекові або контрактні інваріанти, що мають лишитися незмінними. -->
- `<інваріант 1>`

## 3. Як перевіряємо без GPU
<!-- Конкретні команди/тести, які виконуються без фізичного GPU (compileall, unit/integration/contract pytest тощо). -->
- `<команда перевірки без GPU>`

## 4. Який артефакт буде доказом
<!-- Конкретний артефакт (звіт, JSON/лог запуску тестів, SHA256, див. lab/evidence.py або lab/reports.py), який підтверджує виконання. -->
- `<шлях або формат артефакту-доказу>`

---

## JSON-формат (для API / CLI `payload.task_contract`)

```json
{
  "files_to_change": ["lab/example.py", "tests/test_example.py"],
  "invariants": ["Ізоляція LAB (#491) і PolicyGate збережені без послаблень"],
  "no_gpu_verification": [".venv/bin/python -m pytest -q tests/test_example.py"],
  "proof_artifact": "Звіт pytest + запис результату у state_dir/results.jsonl"
}
```
