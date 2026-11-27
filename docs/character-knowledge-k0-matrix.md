# Character Knowledge & Memory (#506) — K0 Матриця «Контракт → Код → Тест»

Контрольна точка **K0** фіксує відповідність погоджених контрактів архітектури **Character Knowledge & Memory** (`#506`) фактичним схемам даних PostgreSQL, API, доменній логіці, CORE-гейтам, Worker-виконанню та автоматизованим тестам.

## 1. Схема даних (модуль `characters`)

Усі таблиці живуть у власній схемі модуля Characters (`modules/characters/migrations/m002_knowledge_memory.sql`), прив’язані до `characters(id) ON DELETE CASCADE` і включаються в модульний backup/restore:

| Таблиця | Призначення | Етап |
|---|---|---|
| `character_knowledge_sources` | Ізольована бібліотека джерел персонажа: версія, `sha256`, `origin`, `license_info`, `rights_status`, `quarantine_reason` | K1 |
| `character_knowledge_chunks` | Витягнуті Worker-ом фрагменти з `source_version`, `page_number`, `content_sha256`, `ocr_used`, `injection_flagged` | K2 |
| `character_knowledge_items` | Багаторівневі елементи пам’яті (`layer`: `canon`/`memory`/`knowledge`/`belief`/`working`), `truth_status`, `risk_level`, `status`, `provenance`, `legal_hold` | K3, K5 |
| `character_knowledge_policies` | Версіоновані політики погодження (`allow_auto_approve_low/medium`, `blocked_topics`, `shared_with_characters`) | K3 |
| `character_knowledge_revocations` | Журнал негайного відкликання (`action`, `reason`, `legal_block`) | K3 |
| `character_knowledge_incidents` | Реєстр інцидентів для вже опублікованого контенту після відкликання джерела | K3, K4 |
| `character_knowledge_snapshots` | Незмінні snapshot-фіксації контексту для відтворюваності Jobs (`snapshot_id`, `canon_version`, `item_ids`) | K4 |
| `character_timelines` | Основна хронологія (`main`) та ізольовані альтернативні гілки сюжетів | K5 |
| `character_canon_versions` | Версіоновані snapshot-и затвердженого канону з `snapshot_sha256` та `approved_by` | K5 |

## 2. Матриця «Контракт → Код → Тест»

| Контракт / вимога (#506) | Реалізація в коді | Автоматизовані тести (`tests/test_character_knowledge_506.py`) |
|---|---|---|
| **K1 — Бібліотека джерел, права, карантин, backup/restore** | `modules/characters/migrations/m002_knowledge_memory.sql`, `modules/characters/knowledge_logic.py` (`CharacterKnowledgeStore`), `modules/characters/characters_module.py` | `test_k1_source_registration_isolation_quarantine_and_backup_restore` |
| **K2 — Worker extraction, OCR (uk), захист від XXE/zip-bomb/prompt-injection** | `worker/knowledge_extractor.py`, `worker/role_executor.py::execute_knowledge_extract`, `modules/characters/knowledge_logic.py` (ingest chunks) | `test_k2_worker_extraction_formats_sanitization_injection_and_ukrainian_ocr` |
| **K3 — Ризик-орієнтована матриця погодження, версіоновані політики, відкликання** | `modules/characters/knowledge_logic.py`, `modules/characters/characters_module.py` (`PUT /characters/{id}/knowledge/policies`, `POST .../revoke`) | `test_k3_risk_approval_matrix_policies_and_immediate_revocation` |
| **Контракт 1 / K4 — Context API через Gateway (2-рівнева авторизація, профілі `script_agent`/`storyboard`/`voice`, гібридний пошук, бюджет, snapshots)** | `core/knowledge_context.py` (`fetch_job_context`, `format_context_block`, `prepare_voice_knowledge`), `core/module/gateway.py`, `core/api/job_helpers/script.py`, `core/api/job_helpers/tts.py`, `modules/characters/knowledge_logic.py` | `test_k4_context_api_gateway_profiles_hybrid_search_budget_and_snapshot_pinning`, `test_voice_dispatch_pins_context_and_blocks_retry_after_revocation` |
| **Контракт 4 — Повторний запуск після відкликання, утримання публікації (`legal_hold`), інциденти, fail-closed при недоступності перевірки прав** | `core/knowledge_context.py` (`KnowledgeVerificationUnavailable`, `KnowledgeRightsBlocked`), `core/api/jobs.py` (rerun/publish gates), `core/api/tasks.py`, `modules/characters/characters_module.py` (`verify-rights`, `incidents`) | `test_voice_dispatch_pins_context_and_blocks_retry_after_revocation`, `test_did_samohonshchyk_e2e_provenance_clarifications_and_fail_closed` |
| **K5 — Багаторівнева пам’ять, `truth_status` (факт/переконання/чутка), ізоляція таймлайнів, перенесення подій, міграція, відкат канону** | `modules/characters/knowledge_logic.py`, `modules/characters/characters_module.py` (`/timelines`, `/canon`) | `test_k5_versioned_canon_isolated_timelines_event_transfer_and_migration` |
| **Старі Jobs без snapshot** | `core/api/jobs.py` перевіряє `if job.knowledge_snapshot or job.voice_knowledge_snapshot`; для legacy Jobs без snapshot повторна перевірка прав не блокує існуючу поведінку, а `fetch_job_context()` повертає `None`, якщо модуль Characters не розгорнуто | `test_k4_context_api_gateway_profiles_hybrid_search_budget_and_snapshot_pinning`, `test_did_samohonshchyk_e2e_provenance_clarifications_and_fail_closed` |

## 3. Умови використання джерел і залишок приймання

- Джерела без підтверджених прав (`rights_status = 'unverified'`) автоматично потрапляють у карантин і не допускаються до Context API для генерації або публікації.
- Несинтетичний приймальний сценарій на реальних книжках «Дід Самогонщик» (включно з підтвердженням прав використання та перевіркою українського OCR на реальному скані) залишається залежним від підтвердження прав власником.
