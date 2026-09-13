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

## Автоматизована ротація та recovery

Команда генерації підтримує відтворюваний drill без доступу до production ceremony:

```bash
python scripts/generate-root-metadata.py \
  --keys-dir installer/root-keys \
  --output installer/root-keys/root-metadata.json \
  --key-ids root-1 root-2 \
  --threshold 2 \
  --version 1 \
  --expiry-days 365 \
  --channels stable beta
```

Для ротації створюється новий metadata-документ із більшим `version`; старий ключ
позначається `revoked: true`, а новий ключ додається до `release_keys`.
Приклад:

```bash
python scripts/generate-root-metadata.py \
  --keys-dir installer/root-keys \
  --output installer/root-keys/root-metadata-v2.json \
  --key-ids root-2 root-3 \
  --threshold 2 \
  --version 2 \
  --existing-metadata installer/root-keys/root-metadata-v1.json \
  --revoked root-1 \
  --expiry-days 365 \
  --channels stable beta
```

`validate_root_metadata()` відхиляє downgrade, equivocation, прострочені metadata,
несправжній threshold, tampered digest, неправильний channel і revoked key.
`authorize_release_key()` повторно перевіряє key ID, channel, digest і revoke state.
Recovery після компрометації: позначити старий key revoked, згенерувати replacement,
підписати новий metadata потрібним threshold і перевірити, що старий ключ більше не
авторизується. Старий metadata зберігається лише для аудиту та rollback drill.
