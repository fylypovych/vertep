# Підписаний контракт runtime-релізу Vertep

Контракт версії 2 є єдиним джерелом правди для Bootstrap Installer, Update Agent і перевірки релізу. Він криптографічно зв’язує номер та послідовність релізу з файлами, каталогом ролей, контейнерними образами, сумісністю API/бази даних і CycloneDX SBOM.

## Обов’язкові гарантії

- кожен файл має SHA-256 і точний розмір;
- кожен сервіс, указаний хоча б в одній ролі, має незмінний container digest;
- список сервісів, можливостей і модулів кожної ролі збігається з підписаним каталогом;
- SBOM входить до файлового inventory та перевіряється за SHA-256;
- `release_sequence` захищає від повторного відтворення старого релізу;
- контракт містить версії Core API, Worker API, схеми бази даних і стратегію міграції;
- Bootstrap передає Compose лише образи у формі `reference@sha256:digest`.

## Створення SBOM

```bash
python scripts/generate-sbom.py \
  --image-lock images.json \
  --version 0.0.0.12 \
  --output bundle/sbom.cdx.json
```

`images.json` формується release pipeline після побудови й публікації образів. У репозиторій не можна записувати вигадані digest.

## Створення контракту

```bash
python scripts/runtime-contract.py build \
  --artifact-root bundle \
  --version 0.0.0.12 \
  --sequence 12 \
  --image-lock images.json \
  --private-key release-private.pem \
  --rollback-safe \
  --output manifest.json
```

Приватний ключ зберігається поза репозиторієм. Публічний ключ постачається через довірений bootstrap і процедуру ротації offline-root metadata.

## Перевірка

```bash
python scripts/runtime-contract.py validate manifest.json \
  --artifact-root bundle \
  --public-key release-public.pem
```

Перевірка завершується помилкою, якщо файл змінено, роль посилається на образ без digest, SBOM відсутній, контракт прострочений або RSA-підпис неправильний.
