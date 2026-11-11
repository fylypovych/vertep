# LAB: операції та діагностика

> Статус: чинна процедура | Аудиторія: власник, оператор лабораторії | Канон: [AGENTS.md](../../AGENTS.md) §4.6–§4.7.

## Розгортання

LAB розгортається на окремій Ubuntu VM; інструкція панелі й Compose — [Lab Web](../../deploy/lab/web/README.md). Bootstrap — `bootstrap-lab.sh`. Наявність запущеної панелі не активує автономний executor, policy gate або GPU sharing. Повне приймання стенду — [кваліфікація](lab-qualification.md).

## Перевірка стану

Команди виконуються в наявному ізольованому середовищі LAB. Вони не є інструкцією змінювати production.

```bash
python -m lab.cli --help
python -m lab.cli status
python -m lab.cli audit
python -m lab.cli check create_lab_issue --subject lab.0.0.0.1
```

`check` перевіряє рішення policy, не створює Issue і не надає права. GPU-команди CLI вимагають глобального `--gpu` перед підкомандою та налаштованого `LAB_RESOURCE_URL`; у його відсутність CLI може використовувати fake provider, тому його результат не є доказом physical GPU. Live GPU-операції виконуються лише в погодженому scope.

## Погодження і конфігурація

Owner-операції CLI вимагають `LAB_OWNER_TOKEN`, API — `X-Lab-Owner-Token`; identity береться з `LAB_OWNER_IDENTITY`. `--approved-by` не є підтвердженням прав. Не копіювати справжній токен у звіт, Git або журнал команд. Policy gate, grants, GPU sharing і таймер активуються лише за відповідним дорученням власника.

`lab/budget.py` зберігає ліміти в `<state-dir>/budget/budget.json`, запуски — у `runs.jsonl`. Зміна бюджету або скид history не є способом обійти відмову policy. Показники читати через CLI/API; не використовувати історичні приклади з викликом приватних методів для керування робочим стендом.

## Кваліфікація

[Колектор R1–R6](lab-qualification.md) описує `scripts/qualify_lab.py --plan`, збір і незалежну валідацію доказів. План, mock, сухий запуск та підготовлений Compose не є PASS реального стенду. Кожен доказ містить SHA, середовище, очікуваний і фактичний результат, артефакти та незалежну перевірку. Критерії 24×7 не підтверджуються лише наявністю timer або UI.
