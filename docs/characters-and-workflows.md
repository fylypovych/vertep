# Персонажі та workflow

> Статус: чинний | Аудиторія: розробник, оператор | Канон: `AGENTS.md` §4.2, §4.3, §24

## Персонаж — це директорія

Новий персонаж додається директорією `characters/<id>/` без змін коду. Приклад — `characters/did_samogon/`.

| Файл | Зміст |
|---|---|
| `character.json` | `id`, `name`, `language`, `enabled`, базовий `workflow`. |
| `system_prompt.txt` | Системний промпт LLM для сценаріїв. |
| `voice.json` | Параметри голосу (voice/provider/model/language/speed). |
| `visual.json` | Візуальний опис для генерації зображень. |
| `generation.json` | Параметри генерації, retries. |
| `publishing.json` | Канали публікації персонажа. |

ID — системний, immutable; перейменування через UI створює конфлікти посилань і блокується (409 із залежностями). Видалені записи не відновлюються seed при update/recreate.

## ComfyUI-workflow — окремо від коду

Workflow лежать у `workflows/` (`image/`, `video/`, `character/`). CORE передає в task лише шлях-референс; зміна моделі/LoRA/sampler не вимагає зміни CORE. Референс резолвиться відносно persistent root однаково на CORE і Worker; demo-файли (`demo.json`) — стартові приклади, не production-контракт. Кастомні ноди (напр. VHS) мають бути встановлені в образі Worker і перевірені readiness.

## Масштабування без коду

- Нова роль — через bootstrap + wizard; нова capability — конфігом Worker.
- Новий персонаж — директорією; нова платформа — адаптером у `publishers/<platform>/`.
- Список ролей для Wizard — `config/node_roles.json`.
