# Release Key Ceremony

Цей документ описує процедуру генерації, ротації та відновлення
offline-root metadata для Vertep Update Agent.

## Ролі

- **Root Key Holder** — володіє приватним root-ключем, генерує нову кореневу
  версію metadata під час ротації.
- **Release Key Issuer** — володіє online release-ключами, підписує manifests.
- **Bootstrap Distributor** — постачає `root-keys/` та `root-metadata.json`
  разом із bootstrap image.
- **Update Operator** — запускає оновлення, моніторить стан.

## Каталог `installer/root-keys`

```
installer/root-keys/
├── root-metadata.json        # threshold-signed коренева метадата
├── 0001.pem                  # release key 1 (public)
├── 0002.pem                  # release key 2 (public)
└── ...
```

- `root-metadata.json` містить `version`, `expires_at`, `release_keys`,
  `signatures`.
- Кожен `.pem` — це відкритий ключ для `authorize_release_key`.
- Приватні ключі зберігаються **лише** у Root Key Holder і Release Key Issuer,
  ніколи не комітяться.

## Церемонія ротації (>=2 осіб)

1. Root Key Holder генерує нову пару ключів (або використовує існуючу) для
   кожного release key.
2. Кореневі публічні ключі копіюються до `installer/root-keys/*.pem`.
3. Генерується `root-metadata.json` з полями:
   - `version` — monotonic збільшення
   - `expires_at` — ISO-8601, зазвичай +365 днів
   - `release_keys` — mapping `key_id` → `{sha256, channels, revoked}`
   - `signatures` — >= `threshold` підписів від release keys
4. Метадата перевіряється `validate_root_metadata`.
5. `installer/root-keys/` підписується та архівується в release artifact.
6. Bootstrap image оновлюється новим `installer/root-keys/`.
7. Старий `root-metadata.json` зберігається для rollback.

## Ceremony check-list

- [ ] Згенеровано нові root-ключі (RSA 4096 або EC P-384)
- [ ] `root-metadata.json` перевірено локально
- [ ] Підписи перевірено: `len(verified) >= threshold`
- [ ] `expires_at` встановлено на майбутнє
- [ ] `release_sequence` скинуто/перевірено
- [ ] Bootstrap image перезібрано з новим `root-keys/`
- [ ] Збережено old `root-metadata.json` для emergency rollback
- [ ] Документовано `key_id` учасників церемонії

## Compromised key recovery

1. Видалити/відзначити `revoked: true` у новому `root-metadata.json`.
2. Згенерувати новий ключ для пошкодженого `key_id`.
3. Оновити `sha256` у `release_keys`.
4. Провести церемонію ротації з новим ключем.
5. Bootstrap distributors отримують новий `root-keys/` під час наступного
   release.

## Rotation drill

Щоквартально виконувати:
1. Згенерувати новий `root-metadata.json` з новим `version`.
2. Переконатися, що старий metadata відкидається (`version < trusted_version`).
3. Переконатися, що новий metadata приймається.
4. Відкотити зміни, якщо drill не пройшов.
