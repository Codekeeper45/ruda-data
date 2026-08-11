# Диаграммы RUDA Data

Эти диаграммы объясняют не код приложения, а устройство release-снимка данных:
от исходного видео до таблиц, RAG и аудиторского решения.

## Карта доменов

```mermaid
flowchart LR
  subgraph MEDIA[Источники и речь]
    SF[source_folders] --> V[videos]
    V --> VS[video_speakers]
    VS --> U[utterances]
    U --> W[words]
    SP[speaker_profiles] --> SS[speaker_samples]
    SS --> SA[sample_audits]
    VS -. известный голос .-> SP
  end

  subgraph GAME[Игры]
    V --> R[mafia_rounds]
    R --> P[mafia_round_participants]
    R --> PH[mafia_phases]
    R --> E[mafia_events]
    P --> ROLE[mafia_roles]
    E -. актёр или цель .-> P
    E -. фаза .-> PH
  end

  subgraph SEARCH[Поиск]
    U --> LINK[semantic_document_utterances]
    LINK --> DOC[semantic_documents]
    DOC --> FTS[FTS5]
    DOC --> VEC[embedding_vectors]
    DOC --> JOB[embedding_jobs]
  end

  subgraph AUDIT[Аудит]
    EVID[audit_evidence] --> COR[audit_corrections]
    EVID --> INS[вставки и удаления]
    EVID --> MASK[masked_identity_episodes]
    COR --> EFF[effective_* views]
    INS --> EFF
  end
```

## Игровая ER-диаграмма

```mermaid
erDiagram
  VIDEOS ||--o{ MAFIA_ROUNDS : contains
  MAFIA_ROUNDS ||--o{ MAFIA_ROUND_PARTICIPANTS : has
  MAFIA_ROUNDS ||--o{ MAFIA_PHASES : splits_into
  MAFIA_ROUNDS ||--o{ MAFIA_EVENTS : records
  MAFIA_ROLES ||--o{ MAFIA_ROUND_PARTICIPANTS : assigns
  VIDEO_SPEAKERS ||--o{ MAFIA_ROUND_PARTICIPANTS : links
  SPEAKER_PROFILES ||--o{ MAFIA_ROUND_PARTICIPANTS : identifies
  MAFIA_PHASES ||--o{ MAFIA_EVENTS : contains
  MAFIA_ROUND_PARTICIPANTS ||--o{ MAFIA_EVENTS : acts_or_targets

  MAFIA_ROUNDS {
    int id PK
    int video_id FK
    int round_number
    float start_time
    float end_time
    string winning_faction
    string review_status
  }
  MAFIA_ROUND_PARTICIPANTS {
    int id PK
    int round_id FK
    int profile_id FK
    int role_id FK
    string faction
    string outcome
  }
  MAFIA_PHASES {
    int id PK
    int round_id FK
    string phase_type
    int phase_number
  }
  MAFIA_EVENTS {
    int id PK
    int round_id FK
    int phase_id FK
    int actor_participant_id FK
    int target_participant_id FK
    string event_type
  }
```

## Таймлайн раунда

```mermaid
timeline
  title Типичный порядок фаз одной игры
  Вступление : introduction
  День 1 : day
  Голосование 1 : voting
  Последние слова : last_words, если игрок выбыл
  Ночь 1 : night
  День 2 : day
  Результат : result или winner_announcement
  Послеигровое обсуждение : postgame, вне игровых фактов
```

`vote_out` привязан к дневному голосованию. `night_kill` — к ночи. Фаза
`last_words` объясняет, почему реплика после выбытия не является вторым
событием устранения.

## Семантический поиск

```mermaid
flowchart TD
  Q[Вопрос] --> N[Нормализация имени и ASR-вариантов]
  N --> A[Несколько аспектов запроса]
  A --> F[FTS5: буквальные совпадения]
  A --> E[Voyage 4: query embedding]
  E --> S[Сходство с документами]
  F --> M[Кандидаты]
  S --> M
  M --> FIL[Фильтры: видео, раунд, роль, время]
  FIL --> DIV[Диверсификация по видео]
  DIV --> RR[rerank]
  RR --> H[Фрагменты с таймкодами]
  H --> P[Проверка составом, событиями и контекстом]
  P --> OUT[Ответ со ссылками]
  E -. сервис недоступен .-> F
```

## Аудит и выпуск

```mermaid
stateDiagram-v2
  [*] --> Наблюдение
  Наблюдение --> Доказательство: таймкод и источник
  Доказательство --> Кандидатная_правка
  Кандидатная_правка --> Отклонена: противоречие или недостаточно данных
  Кандидатная_правка --> Одобрена: ручное решение
  Отклонена --> Журнал_аудита
  Одобрена --> Новая_копия_БД
  Новая_копия_БД --> Материализация
  Материализация --> Пересборка_RAG
  Пересборка_RAG --> Валидация
  Валидация --> Release: integrity и FK успешны
  Release --> [*]
```

## Исторические маски и голоса

```mermaid
flowchart LR
  PROFILE[speaker_profile] --> TRACK[video_speaker]
  TRACK --> UTTERANCE[реплики]
  TRACK --> PARTICIPANT[участник раунда]
  PARTICIPANT --> Q{Это маска?}
  Q -- нет --> CANON[каноническое имя]
  Q -- да --> EP[masked_identity_episode]
  EP --> R[revealed_profile_id]
  R --> PROOF[реплики и таймкоды раскрытия]
```

Маска не переписывает профиль глобально. Одно и то же отображаемое имя может
иметь разные раскрытия в разных видео, поэтому исторические эпизоды хранятся
отдельно.
