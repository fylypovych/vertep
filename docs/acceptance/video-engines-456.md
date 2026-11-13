# Відеодвижки — #456 і рішення щодо ShortGPT (#462)

> Статус: рішення власника та межі доказів | Аудиторія: власник, розробник, ревізор | Канон: [AGENTS.md](../../AGENTS.md).

## Рішення власника

Під час виконання #456 агент повідомив власнику, що ShortGPT у Vertep ніколи не пакувався: був лише клієнт `ShortGPTEngine` до зовнішнього `SHORTGPT_URL` з контрактом MoneyPrinterTurbo, а upstream ShortGPT — Python-бібліотека з Gradio UI без такого HTTP API, залежна від зовнішніх OpenAI/Pexels/озвучки. Локальне пакування означало б новий образ і власний bridge, тобто новий backend.

Відповідь власника в робочій сесії 2026-11-12, дослівно: «shortgpt - видалити повністю. MoneyPrinterTurbo - довести до повноти використання».

Отже, вимоги #456 щодо **локального ShortGPT** (AC1 у частині ShortGPT, AC3, ShortGPT-частини AC4/AC5/AC7) змінено рішенням власника: ShortGPT не інтегрується, а видаляється. Видалення не є виконанням локальної інтеграції і так не подається.

## Що реалізовано (0.0.3.67)

- ShortGPT видалено з коду, конфігурації, `.env.example`, документації й UI разом із generic-гілкою `/render`, яку використовував лише він. Збережений вибір `shortgpt` показується непідтримуваним і відхиляється без переходу на Native (`UnsupportedVideoEngine`).
- MoneyPrinterTurbo: локальний runtime через `engine_runtimes` плану розгортання, локальний Worker із `video_assembly`, автоматична адреса `http://moneyprinter:8098`, без ручного токена; зовнішній runtime лишився окремим варіантом. Деталі — [moneyprinter-runtime.md](../moneyprinter-runtime.md).
- Готовність Native — наявність FFmpeg у CORE, без вимоги локальної GPU.

## Докази та межі

Автоматизовані: `tests/test_issue_456_local_engine.py` (зокрема `test_shortgpt_is_gone_from_code_configuration_and_ui`), `tests/test_video_engines.py`, `tests/test_provider_matrix.py`, `web-v2/tests/issue-456.spec.ts`. Реальний render локальним MoneyPrinterTurbo та installation evidence лишаються в #441/#442. Рішення записано в цьому документі; зміна описів чи статусів #456/#462 у GitHub — лише за окремим дорученням власника.
