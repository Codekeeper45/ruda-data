# Полный справочник схемы `app.db`

Документ описывает рабочую release-схему. Типы SQLite: `INTEGER` — числа и ID,
`REAL` — секунды/дробные оценки, `TEXT` — строки, `JSON` — сериализованные
структуры, `BLOB` — бинарный вектор, `BOOLEAN` — 0/1. Все времена видео —
секунды от его начала. `NULL` означает «не установлено/не применимо», а не
«факт опровергнут».

> Для визуальной карты откройте [диаграммы](diagrams.md). Служебные FTS5
> таблицы с суффиксами `_data`, `_idx`, `_docsize`, `_config` намеренно не
> описываются: их создаёт и обслуживает SQLite, вручную их не редактируют.

## Значения, общие для многих таблиц

| Поле | Допустимые/наблюдаемые значения |
| --- | --- |
| `review_status` | `confirmed`, `auto_verified`, `needs_review`, `unknown`. Первые два пригодны для подтверждённых ответов при отсутствии конфликта; последние два — нет. |
| `status` аудиторской правки | `candidate`, `approved`, `rejected`. |
| `status` технического задания | Обычно `completed` или `failed`. |
| `faction` | `civilians`, `mafia`, `unknown`. |
| `outcome` | `won`, `lost`, `unknown`. |
| `winning_faction` | `civilians`, `mafia`, `unknown`. |
| `confidence`, `role_confidence` | Оценка 0–1; не заменяет доказательство. |
| `created_at`, `updated_at`, `completed_at` | Время создания, изменения, завершения; `NULL`, если действие не наступило. |

## Источники, речь и профили

### `source_folders`

Каталоги исходников: `id` (PK), `path`, `recursive`, `auto_scan`, `enabled`,
`last_scanned_at`, `last_error`, `created_at`. `recursive`, `auto_scan`,
`enabled` — boolean; путь и ошибка служебные, не пользовательские факты.

### `videos`

Одна строка на медиафайл: `id` (PK), `source_folder_id` (FK), `title`,
`original_filename`, `source_path`, `source_signature`, `source_size_bytes`,
`source_modified_ns`, `duration_seconds`, `prepared_path`,
`prepared_size_bytes`, `status`, `language`, `model`, `remote_job_id`,
`raw_transcript_path`, `transcript_text`, `error_message`, `created_at`,
`started_at`, `completed_at`.

В выпуске `status=completed`, `language=ru`, `model=enhanced`. `source_*` и
`prepared_*` — происхождение файла; `transcript_text` — сырой полный текст,
а точные границы берутся из `utterances`/`words`.

### `transcription_jobs`

Журнал внешней обработки: `id` (PK), `kind`, `status`, `video_id` (FK),
`sample_id` (FK), `remote_job_id`, `config_json`, `prepared_path`,
`attempt_count`, `next_attempt_at`, `error_message`, `created_at`, `updated_at`,
`submitted_at`, `completed_at`.

`kind=video` — транскрибация видео; `kind=enrollment` — регистрация образца.

### `speaker_profiles`

Канонический голосовой профиль: `id` (PK), `name`, `api_label`, `notes`,
`active`, `created_at`, `updated_at`. `active=1` включает профиль в
сопоставление; это не утверждение о присутствии в конкретном видео.

### `speaker_samples`

Референс-файлы профиля: `id` (PK), `profile_id` (FK), `original_filename`,
`stored_path`, `duration_seconds`, `size_bytes`, `sha256`,
`speaker_identifier`, `enrollment_model`, `enrollment_language`, `status`,
`error_message`, `created_at`, `completed_at`.

`status` обычно `completed` или `pending_review`; `sha256` защищает от
дубликата; `speaker_identifier` возвращает внешний сервис.

### `sample_audits` и `profile_reviews`

`sample_audits`: `id` (PK), `sample_id` (FK), `source_path`, `pcm_sha256`,
`sample_rate`, `channels`, `bit_depth`, `codec`, `rms_dbfs`, `peak_dbfs`,
`silence_ratio`, `clipping_ratio`, `within_profile_similarity`,
`closest_other_profile`, `closest_other_similarity`, `quality_status`,
`quality_issues`, `manual_status`, `manual_notes`, `selected_for_enrollment`,
`reviewed_at`, `audited_at`.

`quality_status` в выпуске `good` или `warning`; `quality_issues` — JSON;
`selected_for_enrollment=1` означает включение образца в профиль.

`profile_reviews`: `id` (PK), `profile_id` (FK), `manual_status`, `notes`,
`reviewed_at`, `updated_at`. В текущем выпуске все 23 профиля `approved`.

### `video_speakers`, `utterances`, `words`

`video_speakers`: `id` (PK), `video_id` (FK), `label`, `display_name`,
`profile_id` (FK), `is_known`, `total_speech_seconds`, `utterance_count`.
`label` — локальная метка диаризации; один label в разных видео не является
глобальным персонажем.

`utterances`: `id` (PK), `video_id` (FK), `speaker_id` (FK), `sequence`,
`start_time`, `end_time`, `text`, `average_confidence`, `word_count`. Это
основная реплика с таймкодом; `text` может содержать ASR-ошибку.

`words`: `id` (PK), `video_id` (FK), `utterance_id` (FK), `speaker_id` (FK),
`sequence`, `token_type`, `content`, `start_time`, `end_time`, `confidence`,
`language`, `attaches_to`, `is_eos`, `raw_json`. `token_type=word` или
`punctuation`; у пунктуации `attaches_to=previous`.

## Игровая модель

### `video_enrichments` и `enrichment_runs`

`video_enrichments`: `video_id` (PK/FK), `content_type`, `has_mafia`,
`confidence`, `status`, `review_status`, `extractor_model`,
`extractor_version`, `source_hash`, `error_message`, `raw_result`,
`created_at`, `updated_at`, `completed_at`.

`content_type`: `talk_only`, `mafia_only`, `mixed`. `raw_result` — технический
JSON извлекателя, не источник финального пользовательского факта.

`enrichment_runs`: `id` (PK), `video_id` (FK), `stage`, `status`, `model`,
`pipeline_version`, `input_hash`, `attempt_count`, `prompt_tokens`,
`completion_tokens`, `estimated_cost_usd`, `raw_output_path`, `error_message`,
`created_at`, `started_at`, `completed_at`. В текущем выпуске `stage=extract`.

### `enrichment_evidence`

Связывает извлечённое поле с происхождением: `id` (PK), `entity_type`,
`entity_id`, `field_name`, `utterance_id` (FK), `start_time`, `end_time`,
`source_type`, `source_ref`, `excerpt`, `confidence`, `created_at`.

### `mafia_roles`

Справочник ролей: `id` (PK), `code`, `name`, `faction`, `aliases`,
`description`, `created_at`.

Допустимые `code`: `civilian`, `mafia`, `don`, `sheriff`. Других игровых ролей
в домене нет. `faction=civilians` у Мирного/Шерифа, `faction=mafia` у
Мафии/Дона.

### `mafia_rounds`

Партия в рамках видео: `id` (PK), `video_id` (FK), `round_number`,
`start_time`, `end_time`, `start_utterance_id` (FK), `end_utterance_id` (FK),
`is_partial`, `winning_faction`, `winner_summary`, `confidence`,
`review_status`, `extractor_version`, `created_at`, `updated_at`.

`is_partial=1` означает обрезанную/неполную игру. `winning_faction=unknown`
не должен превращаться в победу какой-либо стороны.

### `mafia_round_participants`

Состав конкретного раунда: `id` (PK), `round_id` (FK), `profile_id` (FK),
`video_speaker_id` (FK), `display_name`, `role_id` (FK), `faction`, `outcome`,
`confidence`, `role_confidence`, `review_status`, `notes`, `created_at`,
`updated_at`.

Это главный источник для вопроса «кто был мафией в игре». `role_id=NULL` не
запрещает известную фракцию: Мафия может быть установлена, а Дон/обычная Мафия
— нет.

### `mafia_phases`

Непрерывные части раунда: `id` (PK), `round_id` (FK), `phase_type`,
`phase_number`, `start_time`, `end_time`, `is_partial`, `confidence`,
`review_status`, `created_at`.

`phase_type`: `introduction`, `day`, `voting`, `night`, `last_words`,
`result`, `intermission`, `postgame`. `phase_number` нумерует дни/ночи и может
быть `NULL` для вступления или результата.

### `mafia_events`

Дискретные действия: `id` (PK), `round_id` (FK), `phase_id` (FK),
`event_type`, `actor_participant_id` (FK), `target_participant_id` (FK),
`start_time`, `end_time`, `summary`, `confidence`, `review_status`,
`created_at`.

`event_type`: `game_start`, `game_end`, `night_kill`, `vote_out`,
`sheriff_check`, `don_check`, `role_reveal`, `winner_announcement`, `other`.
`actor_participant_id` и `target_participant_id` могут быть `NULL`, если
участник не доказан. В одном раунде допускается несколько `vote_out`.

## Семантический поиск

### `semantic_documents` и FTS5

`semantic_documents`: `id` (PK), `document_type`, `video_id` (FK),
`round_id` (FK), `start_time`, `end_time`, `text`, `token_count`,
`content_hash`, `pipeline_version`, `created_at`, `updated_at`.

`document_type`: `timeline_chunk`, `round_summary`, `video_summary`.
`semantic_documents_fts` — виртуальная FTS5-таблица с полем `text`;
используется для буквального поиска.

`semantic_document_utterances`: `document_id` (PK/FK), `utterance_id`
(PK/FK), `sequence`. Это составная связь документа с исходными репликами.

### `embedding_vectors` и `embedding_jobs`

`embedding_vectors`: `id` (PK), `document_id` (FK), `model`, `dimensions`,
`dtype`, `vector`, `content_hash`, `created_at`. В текущем выпуске модель
`voyageai/voyage-4`; `vector` — BLOB, его не редактируют вручную.

`embedding_jobs`: `id` (PK), `document_id` (FK), `model`, `dimensions`,
`input_hash`, `status`, `attempt_count`, `error_message`, `created_at`,
`updated_at`, `completed_at`. Это очередь/история пересчёта векторов.

## Канонические имена, маски и effective views

`canonical_characters`: `id` (PK), `profile_id` (FK), `canonical_name`,
`english_name`, `status`, `source_ref`, `created_at`, `updated_at`.
`status=active` у актуальных 23 персонажей.

`character_aliases`: `id` (PK), `canonical_character_id` (FK), `alias`,
`alias_key`, `alias_type`, `review_status`, `confidence`, `rationale`,
`valid_from_video_id` (FK), `valid_to_video_id` (FK), `created_at`,
`updated_at`. `alias_type=canonical` или `asr_error`.

`character_alias_evidence`: `alias_id` (PK/FK), `evidence_id` (PK/FK).

`masked_identity_episodes`: `id` (PK), `video_id` (FK), `round_id` (FK),
`participant_id` (FK), `mask_name`, `start_time`, `end_time`,
`revealed_profile_id` (FK), `revealed_name`, `confidence`, `review_status`,
`evidence_summary`, `notes`, `created_at`, `updated_at`.

`masked_identity_episode_evidence`: `episode_id` (PK/FK), `evidence_id`
(PK/FK). Маску нельзя глобально заменить одним профилем: раскрытие относится
к эпизоду видео/раунда.

`effective_video_speakers` содержит исходные поля `video_speakers` плюс
`effective_profile_id`, `effective_display_name`, `applied_correction_id`.

`effective_utterances` содержит поля `utterances` плюс `effective_speaker_id`,
`effective_start_time`, `effective_end_time`, `effective_text`,
`effective_profile_id`, `effective_display_name`, `applied_correction_id`.

`effective_mafia_round_participants` содержит поля участников плюс
`effective_profile_id`, `effective_display_name`, `character_name`,
`effective_faction`, `effective_role_id`, `role_code`, `role_name`,
`applied_correction_id`. В runtime нужно предпочитать эти представления
сырым таблицам.

## Аудиторский контур

`audit_evidence`: `id` (PK), `source_type`, `source_ref`, `video_id` (FK),
`utterance_id` (FK), `start_time`, `end_time`, `excerpt`, `sha256`,
`created_at`.

`audit_corrections`: `id` (PK), `entity_type`, `entity_id`, `field_name`,
`old_value`, `proposed_value`, `value_type`, `status`, `confidence`,
`evidence_required`, `rationale`, `reviewer`, `reviewed_at`, `created_at`,
`updated_at`. В выпуске `entity_type`: `mafia_round_participant`,
`mafia_phase`, `mafia_event`, `mafia_round`, `video_speaker`,
`video_enrichment`; `value_type`: `text`, `integer`, `real`, `null`.

`audit_correction_evidence`: `correction_id` (PK/FK), `evidence_id` (PK/FK).

`audit_phase_inserts`: `id` (PK), `round_id` (FK), `phase_type`,
`phase_number`, `start_time`, `end_time`, `is_partial`, `confidence`,
`review_status`, `status`, `rationale`, `materialized_phase_id`, `created_at`,
`updated_at`; `audit_phase_insert_evidence`: `phase_insert_id` (PK/FK),
`evidence_id` (PK/FK).

`audit_participant_inserts`: `id` (PK), `participant_id`, `round_id` (FK),
`profile_id` (FK), `video_speaker_id` (FK), `display_name`, `role_id` (FK),
`faction`, `outcome`, `confidence`, `role_confidence`, `review_status`,
`notes`, `status`, `rationale`, `materialized_participant_id`, `created_at`,
`updated_at`; `audit_participant_insert_evidence`: `participant_insert_id`
(PK/FK), `evidence_id` (PK/FK).

`audit_event_inserts`: `id` (PK), `event_id`, `round_id` (FK), `phase_id`
(FK), `event_type`, `actor_participant_id` (FK), `target_participant_id` (FK),
`start_time`, `end_time`, `summary`, `confidence`, `review_status`, `status`,
`rationale`, `materialized_event_id`, `created_at`, `updated_at`;
`audit_event_insert_evidence`: `event_insert_id` (PK/FK), `evidence_id`
(PK/FK).

`audit_event_deletions`: `id` (PK), `event_id` (FK), `expected_json`,
`status`, `rationale`, `created_at`, `updated_at`; связь с источниками в
`audit_event_deletion_evidence(event_deletion_id, evidence_id)`.

`audit_phase_deletions`: `id` (PK), `phase_id` (FK), `expected_json`,
`status`, `rationale`, `created_at`, `updated_at`; связь с источниками в
`audit_phase_deletion_evidence(phase_deletion_id, evidence_id)`.

`audit_event_timing_classifications`: `event_id` (PK/FK), `round_id` (FK),
`classification`, `evidence_id` (FK), `rationale`, `created_at`.

`audit_ledger`: `id` (PK), `event_type`, `entity_type`, `entity_key`,
`old_value`, `new_value`, `actor`, `reason`, `evidence_json`, `created_at`.
Это append-only история действий.

`audit_rebuild_queue`: `id` (PK), `artifact_type`, `entity_id`, `reason`,
`status`, `created_at`, `completed_at`. В выпуске `artifact_type` —
`semantic_documents`; очередь требует пересобрать производные данные.

`archive_data_versions`: `id` (PK), `schema_version`, `data_version`,
`source_sha256`, `source_size_bytes`, `source_wal_sha256`,
`source_wal_size_bytes`, `source_snapshot_sha256`,
`source_foreign_key_errors`, `source_foreign_key_errors_json`, `built_at`,
`builder_version`, `materialized_approved`. `materialized_approved=1` означает,
что одобренные правки были наложены на release-копию.

`schema_migrations`: `version` (PK), `applied_at`.
`app_settings`: `key` (PK), `value`, `encrypted`, `updated_at`.

## Отдельная БД чатов

В release `app.db` чатов нет: приложение хранит их в отдельной `chats.db`.
`ruda_chats`: `id` (PK), `owner`, `title`, `mode`, `created_at`, `updated_at`.
`ruda_messages`: `id` (PK), `chat_id` (FK), `role`, `content`, `sources`,
`steps`, `ts`. Это защищает архив от записи во время работы ассистента.
