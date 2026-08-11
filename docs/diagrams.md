# Диаграммы RUDA Data

Эти диаграммы объясняют не код приложения, а устройство release-снимка данных:
от исходного видео до таблиц, RAG и аудиторского решения.

Диаграммы исполнения Speechmatics — enrollment, очередь, нормализация,
восстановление и экспорт — находятся в
[`Speechmatics-пайплайн: полное руководство`](speechmatics-pipeline.md).

## От исходного файла до проверенного release

```mermaid
flowchart LR
  MEDIA[Видео и аудио] --> ASR[Транскрипция]
  ASR --> DIAR[Диаризационные дорожки]
  DIAR --> RAW[raw-снимок]
  RAW --> ENRICH[Раунды, составы, роли, фазы, события]
  ENRICH --> AUDIT[Доказательства и решения аудита]
  AUDIT --> EFFECTIVE[effective-представления]
  EFFECTIVE --> RAG[FTS5, документы, эмбеддинги]
  RAG --> VERIFY[Целостность, FK и инварианты]
  VERIFY --> RELEASE[Версионный app.db]
```

## Проверка release по манифесту

```mermaid
flowchart LR
  TAG[GitHub Release] --> M[database-manifest.json]
  M --> EXPECTED[asset, размер, SHA-256, data_version, counts]
  TAG --> DB[app.db]
  DB --> ACTUAL[хэш, PRAGMA, версия, counts]
  EXPECTED --> MATCH{совпало?}
  ACTUAL --> MATCH
  MATCH -- да --> OK[снимок воспроизводим]
  MATCH -- нет --> BAD[остановиться и сообщить об ошибке]
```

## Граница содержимого `app.db`

```mermaid
flowchart LR
  subgraph DB[Внутри app.db]
    TRANSCRIPT[реплики, слова, таймкоды]
    GAME[игровое обогащение]
    PROFILES[метаданные профилей]
    SEARCH[поисковые документы и векторы]
    AUDIT[доказательства и журнал]
  end
  subgraph EXTERNAL[Отдельные артефакты]
    MEDIA[исходные WAV и видео]
    SAMPLES[референсные WAV]
    RAW[prepared FLAC и raw JSON]
    CHATS[chats.db]
    CODE[код приложения]
  end
  EXTERNAL -. пути происхождения .-> DB
```

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
flowchart LR
  I[introduction] --> D1[day 1]
  D1 --> V1[voting 1]
  V1 --> L1[last_words]
  L1 --> N1[night 1]
  N1 --> D2[day 2]
  D2 --> V2[voting 2]
  V2 --> MORE[следующие циклы]
  MORE --> R[result]
  R --> P[postgame]
```

`vote_out` привязан к дневному голосованию. `night_kill` — к ночи. Фаза
`last_words` объясняет, почему реплика после выбытия не является вторым
событием устранения.

## Фаза и событие — разные уровни

```mermaid
flowchart TB
  V[video] --> R[mafia_round]
  R --> P1[phase: voting 1]
  R --> P2[phase: last_words 1]
  R --> P3[phase: night 1]
  P1 --> E1[event: vote_out A]
  P2 -. подтверждение результата .-> E1
  P3 --> E2[event: night_kill B]
  E1 --> T1[target: participant A]
  E2 --> T2[target: participant B]
```

`mafia_phase` — протяжённый интервал. `mafia_event` — единичный факт.
Событие всегда принадлежит раунду, но его `phase_id` может быть `NULL`, если
точная фаза не доказана.

## Решающее дерево `event_type`

```mermaid
flowchart TD
  F[Факт из контекста] --> Q{Что произошло?}
  Q -- начало партии --> GS[game_start]
  Q -- конец партии --> GE[game_end]
  Q -- ночное устранение мафией --> NK[night_kill]
  Q -- дневное выбытие голосованием --> VO[vote_out]
  Q -- проверка шерифа --> SC[sheriff_check]
  Q -- проверка дона --> DC[don_check]
  Q -- раскрытие роли --> RR[role_reveal]
  Q -- объявление победы --> WA[winner_announcement]
  Q -- иной значимый факт --> O[other]
  GS --> MAP[раунд, время, фаза]
  GE --> MAP
  NK --> MAP
  VO --> MAP
  SC --> MAP
  DC --> MAP
  RR --> MAP
  WA --> MAP
  O --> MAP
  MAP --> WHO[actor и target, только если доказаны]
  WHO --> EVID[реплика и таймкод]
  EVID --> STATUS[review_status]
```

## Полнота `vote_out` в v1.6.0

```mermaid
flowchart LR
  ALL[113 раундов] --> ZERO[0 событий<br/>3 раунда]
  ALL --> ONE[1 событие<br/>29 раундов]
  ALL --> TWO[2 события<br/>60 раундов]
  ALL --> THREE[3 события<br/>21 раунд]
  PH[Есть фаза voting] --> OK[Раундов без vote_out: 0]
```

Ограничения «один `vote_out` на раунд» нет. Если видео доказывает вторую
дневную казнь, а строка отсутствует, нужна аудиторская вставка.

## ER-диаграмма речи и RAG

```mermaid
erDiagram
  SOURCE_FOLDERS ||--o{ VIDEOS : discovers
  VIDEOS ||--o{ VIDEO_SPEAKERS : diarizes
  VIDEO_SPEAKERS ||--o{ UTTERANCES : speaks
  UTTERANCES ||--o{ WORDS : tokenizes
  SPEAKER_PROFILES ||--o{ VIDEO_SPEAKERS : recognizes
  SPEAKER_PROFILES ||--o{ SPEAKER_SAMPLES : owns
  SPEAKER_SAMPLES ||--|| SAMPLE_AUDITS : evaluated_by
  UTTERANCES ||--o{ SEMANTIC_DOCUMENT_UTTERANCES : contributes
  SEMANTIC_DOCUMENTS ||--o{ SEMANTIC_DOCUMENT_UTTERANCES : cites
  SEMANTIC_DOCUMENTS ||--o{ EMBEDDING_VECTORS : embeds
  SEMANTIC_DOCUMENTS ||--o{ EMBEDDING_JOBS : queues
```

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

## Защитные механизмы схемы

```mermaid
flowchart TD
  WRITE[Изменение] --> FK[Foreign keys]
  WRITE --> TRIGGER{Триггеры}
  TRIGGER --> PROOF[Одобрение требует evidence]
  TRIGGER --> LEDGER[audit_ledger append-only]
  TRIGGER --> FTS[Автосинхронизация FTS5]
  FK --> C[CASCADE: дочерние технические строки]
  FK --> R[RESTRICT: доказательства и факты]
  FK --> N[SET NULL: связь стала неизвестной]
```
