# Публікація

> Статус: чинний | Аудиторія: розробник, оператор | Канон: [AGENTS.md](../AGENTS.md) §3.4–§3.5.

## Архітектура

Кожна платформа — окремий адаптер у `publishers/<platform>/`: `youtube`, `tiktok`, `facebook`, `instagram`, `threads` (+ Telegram). CORE передає готовий ролик і метадані; Publisher Worker виконує публікацію і повертає валідований receipt (платформа/канал, remote ID, URL, час, статус/помилка).

## Правила

- Публікується лише затверджена поточна video-версія; publish до approval заборонено на всіх шляхах (Web, Telegram, retry).
- Idempotency на рівні intent job/version/channel: повтор після часткового успіху не дублює успішні канали; в кожного каналу свій retry-budget.
- Transient/permanent/`NOT_CONFIGURED` помилки обробляються окремо; receipts перевіряються за схемою і прив'язкою до каналу/версії.
- Відео на окремий Worker потрапляє явним artifact-transfer контрактом з integrity-контролем, а не спільним випадковим filesystem.
- Секрети й токени не потрапляють у logs/DOM/receipts/metadata (перевіряється синтетичними маркерами).
- OAuth lifecycle (expiry/refresh/revoke/reconnect/scopes) — для кожної підтримуваної платформи; readiness видно в UI.

## Режими

- **Sandbox/test** — погоджені тестові акаунти або приватні режими; receipt перевіряється (remote ID, URL, видиме медіа).
- **Live** — лише за явним дозволом власника акаунта; жоден Issue чи документ такого дозволу не надає.

## Керування

Канали: readiness/config/test publish/result у Web UI; системні операції (див. `telegram.md`); публикація зі script approval — через штатні Job-дії Approve/Publish.

## Передача відео й посилання на пост

Звірено з офіційною документацією API 2026-11-06: [YouTube `videos.insert`](https://developers.google.com/youtube/v3/docs/videos/insert), [Facebook Video API](https://developers.facebook.com/docs/video-api/guides/publishing), [Instagram Content Publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/), [Threads Posts](https://developers.facebook.com/docs/threads/posts), [Telegram `sendVideo`](https://core.telegram.org/bots/api#sendvideo) і [посилання на повідомлення Telegram](https://core.telegram.org/api/links#message-links). Live-перевірка належить #47.

| Канал | Передача відео | Публічний URL відео | Очікування готовності | Посилання на пост |
|---|---|---|---|---|
| YouTube | Resumable upload файла | Не потрібен | Ні | Формується з `id` відео, який повертає `videos.insert` |
| Facebook | Multipart-завантаження файла | Не потрібен | Ні | Формується: `facebook.com/<page>/videos/<id>`; у довіднику Video-вузла поля `permalink_url` немає |
| Instagram | Resumable: `upload_type=resumable`, файл на `rupload.facebook.com` | Не потрібен | `status_code` до `FINISHED`, далі `media_publish` | Поле `permalink` |
| Threads | Платформа сама завантажує за `video_url` | **Потрібен** | Поле `status` до `FINISHED`; адреса API `graph.threads.com` | Поле `permalink`, може бути відсутнє через авторські права |
| Telegram | `sendVideo` із файлом | Не потрібен | Ні | `t.me/<username>/<message_id>` лише для публічного каналу; Bot API надсилає файли до 50 МБ |

Якщо API не дав посилання, receipt містить `permalink_available: false`, а пост лишається опублікованим: збій читання посилання не запускає повторну публікацію.

### Налаштування

Усе вводиться в адмін-панелі:

- **Налаштування → Публікація → Облікові дані платформ:** client ID/secret, access і refresh tokens YouTube, Facebook, Instagram, Threads, TikTok, згруповані за платформою. Значення зберігаються в захищеному сховищі секретів, а не в налаштуваннях публікації, і після збереження не показуються.
- **Налаштування → Ключі інтеграцій → Telegram-бот:** єдиний токен бота. Його використовують і керування Vertep, і публікація в канал; у «Публікації» показано лише стан цього токена з посиланням на місце зміни. Там же — MoneyPrinterTurbo, зовнішній AI і SMTP. Старе посилання `settings?tab=secrets` відкриває цю вкладку.
- **Налаштування → Стан підключень:** готовність платформ і дія «Перепідключити» (OAuth).
- **Налаштування → Публікація → Канал Telegram:** `@назва` або числовий ідентифікатор. Бот має бути адміністратором каналу.
- **Налаштування → Публікація → Публічна адреса Vertep:** потрібна для Threads, має відкриватися з інтернету. CORE видає посилання лише на активну версію відео, з підписом і строком дії 6 годин.
