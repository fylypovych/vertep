# Vertep — стан відповідності ТЗ

Оновлено для версії 0.6.0.

## Реалізовано та перевіряється локальними тестами

- CORE/WORKER архітектура, heartbeat, визначення GPU та диспетчеризація за VRAM і типом задачі.
- Персистентна модель Job, унікальні річні номери, історія подій і каталог артефактів.
- DAG етапів SCRIPT, ASSETS, TTS, ASSEMBLY і PUBLISH; сцени, спроби та контроль переходів.
- Redis/local priority queue, leases, renew, watchdog, відновлення після рестарту та скасування.
- Fan-out сцен на незалежні GPU tasks, fan-in перед монтажем і retry лише невдалої сцени.
- Відновлення перерваних stage/scene attempts після crash та повторний монтаж із готових артефактів.
- Валідація LLM JSON, image/video prompts і metadata для YouTube, TikTok, Facebook, Instagram та Threads.
- Ollama/ComfyUI/FFmpeg/TTS адаптери без прив'язки оркестратора до конкретної моделі.
- Artifact manifest із SHA-256, MIME, розміром і provenance; перевірена видача файлів.
- Lease-bound Worker results, content-signature validation і серіалізований fan-in без подвійного монтажу.
- Raw uploads, ZIP import/export, планування запуску і dead-letter queue.
- Web UI, Telegram workflow, керування Job, Characters, Brands і Workflows.
- Авторизація, ролі, CSRF, rate limit, worker tokens і локальне зберігання секретів.
- Інсталятор Ubuntu 24.04 для CORE/WORKER, systemd, Docker restart policies та CLI `vertep`.
- Окремий WORKER Compose без серверних залежностей; role-aware update, rollback і status.
- Web UI містить Delete, повтор публікації та керування Brands і Workflows.
- Worker явно переходить в ERROR, якщо GPU обов'язковий, але недоступний.
- Відеозадача має окремий worker artifact contract; CORE перевіряє відеоконтейнер і склеює кліпи сцен через FFmpeg.
- Ролі інсталятора розширюються через manifest profiles та необов'язкові role hooks.
- CI перевіряє Python, тести, shell-синтаксис і Compose-конфігурації без запуску серверів.
- GTX 1660 6 GB визначається як Turing 7.5; інсталятор обирає перевірюваний PyTorch/CUDA-профіль і режим ComfyUI low-VRAM.
- Web UI може перевірити та встановити fast-forward оновлення з довіреного GitHub origin через окремий systemd-агент зі статусом і журналом.

## Потребує перевірки на реальній інфраструктурі

- Фактичне проходження міграцій PostgreSQL 16 і відновлення Redis після reboot.
- Інсталяція NVIDIA driver/toolkit та ComfyUI на GTX 1050/1060, Tesla P40 і сучасних RTX.
- Ollama з обраною локальною моделлю та якість JSON-сценаріїв.
- Telegram webhook через реальну HTTPS-адресу.
- Реальні API Publisher-модулів і сторонній TTS — вони навмисно не імітують успіх без credentials.
- Повний distributed soak test з кількома фізичними WORKER-вузлами та network failure.

Ці пункти не можна чесно підтвердити лише локальними mock-тестами: для них потрібні встановлені сервіси, GPU або зовнішні облікові дані.
