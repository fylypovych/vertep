# План виправлення версій та комітів

## Поточна проблема
1. `VERSION` файл = `0.0.0.5`
2. Останній git tag = `0.0.0.17`
3. Неузгодженість між файлом версії, тегами та комітами
4. Не завжди дотримано семантичного версіонування

## Дії для виправлення поточного стану

### Варіант A: Синхронізувати з останнім тегом (рекомендовано)
```bash
# Оновити VERSION файл
echo "0.0.0.17" > VERSION

# Перевірити, що код відповідає версії
git diff 0.0.0.17..HEAD -- VERSION
```

### Варіант B: Відкатити тег до версії в коді
```bash
# Видалити несинхронізовані теги
git tag -d 0.0.0.6 0.0.0.7 0.0.0.8 0.0.0.9 0.0.0.10 0.0.0.11 0.0.0.12 0.0.0.13 0.0.0.14 0.0.0.15 0.0.0.16 0.0.0.17

# Створити тег відповідно до VERSION файлу
echo "0.0.0.5" > VERSION
git add VERSION
git commit -m "chore: синхронізувати VERSION з тегом 0.0.0.5"
git tag 0.0.0.5
```

## Правила для майбутнього

### 1. Єдина джерело правди — `VERSION` файл
- Всі зміни версії виконуються лише через `VERSION` файл
- Git tag створюється АВТОМАТИЧНО при релізі
- Ніколи не створювати теги вручну

### 2. Git workflow
```
main ────●───●───●───●───●
          ↑   ↑   ↑   ↑   ↑
          │   │   │   │   └─── patch (bugfix)
          │   │   │   └─────── minor (feature)
          │   │   └─────────── major (breaking)
          │   └─────────────── версія в VERSION файлі
          └─────────────────── git tag
```

### 3. Commit message convention
```
type(scope): description

[optional body]

[optional footer]
```

**Types:**
- `feat:` — нова функціональність (minor)
- `fix:` — виправлення багу (patch)
- `docs:` — зміни в документації
- `style:` — форматування, пропущені крапки
- `refactor:` — рефакторинг
- `test:` — додавання тестів
- `chore:` — зміни в build/dev tooling

### 4. Версіонування
- **Major** (X.0.0): breaking changes
- **Minor** (0.X.0): нові функції, backward compatible
- **Patch** (0.0.X): bugfixes, backward compatible

### 5. Процес релізу
1. Оновити `VERSION` файл
2. `git add VERSION`
3. `git commit -m "chore: bump version to X.Y.Z"`
4. `git tag -a X.Y.Z -m "Release X.Y.Z"`
5. `git push --follow-tags`

### 6. Автоматизація
Додати GitHub Actions workflow:
- При push тегу `v*` → створити GitHub Release
- При push у `main` → автоматично збільшити patch версію
- Перевірити, що `VERSION` файл == git tag

## План дій для цього репозиторію

### Крок 1: Визначити поточну версію
```bash
# Перевірити останній зміни в main
git log --oneline -5

# Визначити, чи потрібно bump версії
# Якщо останній тег 0.0.0.17 і всі зміни зроблені після нього:
echo "0.0.0.18" > VERSION
```

### Крок 2: Створити структурований коміт
```bash
# Додати всі зміни
git add -A

# Створити коміт з усіма змінами
git commit -m "feat(release): Deployment Wizard, Bootstrap Installer, Safe Update System

- Розширено до 7 ролей вузлів (core, gpu, text, voice, publisher, backup, monitoring)
- Додано Add Worker Wizard з role-specific командами
- Реалізовано push-based сценарій enrollment
- Додано domain підтримку (WEB_DOMAIN) + SSL SAN
- Завершено Worker self-update mechanism
- Додано rolling update order (workers-first/core-first/custom)
- Реалізовано host-level watchdog + Core API integration
- Додано startup recovery service
- Інтегровано універсальні health checks
- Додано server-side фільтрація для Worker'ів
- Покрито E2E тестами (123 passing, 9 skipped)"
```

### Крок 3: Створити тег
```bash
git tag -a 0.0.0.18 -m "Release 0.0.0.18: Full Deployment Wizard + Safe Update"
```

### Крок 4: Перевірити та push
```bash
# Перевірити статус
git status
git log --oneline -3

# Push з тегами
git push origin main --follow-tags
```

## План запобігання майбутнім проблемам

### 1. Додати pre-commit hook
```bash
# .git/hooks/pre-commit
#!/bin/bash
VERSION=$(cat VERSION)
TAG=$(git describe --abbrev=0 --tags 2>/dev/null || echo "none")
if [ "$TAG" != "v$VERSION" ] && [ "$TAG" != "$VERSION" ]; then
    echo "WARNING: VERSION file ($VERSION) does not match latest tag ($TAG)"
    echo "Run: git tag -a $VERSION -m 'Release $VERSION'"
fi
```

### 2. Додати CI/CD перевірку
```yaml
# .github/workflows/version-check.yml
- name: Verify version consistency
  run: |
    VERSION=$(cat VERSION)
    TAG=$(git describe --abbrev=0 --tags)
    if [ "$TAG" != "$VERSION" ]; then
      echo "::error::VERSION ($VERSION) != TAG ($TAG)"
      exit 1
    fi
```

### 3. Додати CHANGELOG.md
```markdown
# Changelog

## [0.0.0.18] - 2026-08-29
### Added
- Deployment Wizard з 7 ролями
- Add Worker Wizard
- Safe Update System з rolling updates
- Domain підтримка

### Changed
- Уніфіковано deployment plan module
- Покращено health checks

### Fixed
- Worker self-update mechanism
```

### 4. Правило "один коміт — одна зміна"
- Не комітити всі зміни разом
- Розбити на логічні одиниці:
  1. `feat: add domain support`
  2. `feat: add worker self-update`
  3. `feat: add rolling update order`
  4. `test: add E2E tests`
  5. `chore: bump version to 0.0.0.18`

### 5. Використовувати `bump2version` або `semantic-release`
```bash
# bump2version
pip install bump2version

# Bump patch
bump2version patch

# Bump minor
bump2version minor

# Bump major
bump2version major
```

## Негайні дії (сьогодні)

1. **Визначити версію**: оскільки останній тег `0.0.0.17`, наступна версія = `0.0.0.18`
2. **Оновити VERSION файл**: `echo "0.0.0.18" > VERSION`
3. **Створити коміт**: `git commit -am "chore: bump version to 0.0.0.18"`
4. **Створити тег**: `git tag -a 0.0.0.18 -m "Release 0.0.0.18"`
5. **Push**: `git push origin main --follow-tags`
6. **Створити GitHub Release** з описом змін

## Довгостроккові рішення

1. **Автоматизація версіонування** через GitHub Actions
2. **Conventional Commits** для автоматичного bump версії
3. **Semantic Release** для автоматичного створення тегів та releases
4. **Monorepo tooling** (Changesets, Lerna) якщо проект розростається
