# Схема базы данных

Приложение использует нормализованную SQLite-базу. Все временные значения времени речи хранятся в секундах от начала исходного файла.

## Основные таблицы

| Таблица | Назначение | Важные поля |
|---|---|---|
| `speaker_profiles` | Постоянные профили людей | `name`, `api_label`, `active`, `notes` |
| `speaker_samples` | Образцы голоса и идентификаторы Speechmatics | `profile_id`, `stored_path`, `sha256`, `speaker_identifier`, `enrollment_model`, `enrollment_language`, `status` |
| `source_folders` | Локальные папки, которые сканирует приложение | `path`, `recursive`, `auto_scan`, `enabled` |
| `videos` | Один исходный WAV = одна запись видео | `source_path`, `duration_seconds`, `status`, `language`, `model`, `remote_job_id`, `transcript_text` |
| `transcription_jobs` | Устойчивая очередь enrollment/video | `kind`, `status`, `remote_job_id`, `config_json`, `attempt_count`, `error_message` |
| `video_speakers` | Спикеры, найденные в конкретном видео | `video_id`, `label`, `display_name`, `profile_id`, `is_known`, `total_speech_seconds` |
| `utterances` | Цельные реплики | `video_id`, `speaker_id`, `sequence`, `start_time`, `end_time`, `text`, `average_confidence` |
| `words` | Пословный результат Speechmatics | `utterance_id`, `speaker_id`, `sequence`, `content`, `start_time`, `end_time`, `confidence`, `language`, `raw_json` |
| `app_settings` | Настройки и зашифрованный API-ключ | `key`, `value`, `encrypted` |

## Обогащение играми

| Таблица | Назначение | Важные поля |
|---|---|---|
| `video_enrichments` | Тип записи и состояние анализа | `video_id`, `content_type`, `has_mafia`, `confidence`, `status`, `review_status`, `source_hash` |
| `mafia_roles` | Контролируемый словарь ролей | `code`, `name`, `faction`, `aliases` |
| `mafia_rounds` | Раунды мафии внутри видео | `video_id`, `round_number`, `start_time`, `end_time`, `winning_faction`, `confidence`, `review_status` |
| `mafia_round_participants` | Состав и результат участника в конкретном раунде | `round_id`, `profile_id`, `video_speaker_id`, `display_name`, `role_id`, `faction`, `outcome`, `role_confidence` |
| `mafia_phases` | Ночи, дни, голосования и другие фазы | `round_id`, `phase_type`, `phase_number`, `start_time`, `end_time`, `confidence` |
| `mafia_events` | Значимые события игры | `round_id`, `phase_id`, `event_type`, `actor_participant_id`, `target_participant_id`, `start_time`, `summary` |
| `enrichment_evidence` | Проверяемое основание для каждого факта | `entity_type`, `entity_id`, `field_name`, `utterance_id`, `source_type`, `source_ref`, `start_time`, `end_time`, `excerpt`, `confidence` |
| `enrichment_runs` | Резюмируемые попытки извлечения | `video_id`, `stage`, `status`, `model`, `pipeline_version`, `input_hash`, `attempt_count`, `error_message` |

Допустимые значения `content_type`: `talk_only`, `mixed`, `mafia_only`,
`unknown`. Автоматические выводы не уничтожают подтверждённые вручную записи.
Фактическая роль хранится только при наличии доказательства и совпадении с
контролируемым словарём ролей.

## Полнотекстовый и векторный поиск

| Таблица | Назначение | Важные поля |
|---|---|---|
| `semantic_documents` | Фрагменты таймлайна и сводки | `document_type`, `video_id`, `round_id`, `start_time`, `end_time`, `text`, `content_hash`, `pipeline_version` |
| `semantic_document_utterances` | Точная связь документа с исходными репликами | `document_id`, `utterance_id`, `sequence` |
| `embedding_vectors` | Векторы документов | `document_id`, `model`, `dimensions`, `dtype`, `vector`, `content_hash` |
| `embedding_jobs` | Резюмируемая очередь векторизации | `document_id`, `model`, `dimensions`, `status`, `attempt_count`, `error_message` |
| `semantic_documents_fts` | Внешняя FTS5-таблица для лексического поиска | `text`, синхронизируется триггерами |

Векторы намеренно вынесены из `utterances`: это не ломает фильтрацию. Сначала
SQL ограничивает кандидатов по видео, раунду, времени, спикеру, роли, победителю
и типу записи; затем внутри этого множества выполняются FTS5 и косинусное
ранжирование. Связи с исходными репликами и таймкодами сохраняются через
`semantic_document_utterances`.

## Связи

```text
speaker_profiles 1 ── N speaker_samples
speaker_profiles 1 ── N video_speakers
source_folders   1 ── N videos
videos           1 ── N transcription_jobs
videos           1 ── N video_speakers
videos           1 ── N utterances
videos           1 ── N words
video_speakers   1 ── N utterances
utterances       1 ── N words
videos           1 ── 1 video_enrichments
videos           1 ── N mafia_rounds
mafia_rounds     1 ── N mafia_round_participants
mafia_rounds     1 ── N mafia_phases
mafia_rounds     1 ── N mafia_events
videos           1 ── N semantic_documents
mafia_rounds     1 ── N semantic_documents
semantic_documents 1 ── N embedding_vectors
semantic_documents N ── N utterances
```

В `words.raw_json` сохраняется исходный JSON каждого токена, поэтому позднее можно добавить новые поля без повторной расшифровки всего архива.
