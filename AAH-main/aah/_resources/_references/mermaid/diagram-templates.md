# Mermaid Diagram Template Library

Comprehensive template patterns for all supported diagram types. Adapt structure,
detail level, and styling to match the actual project context.

---

## 1. Architecture Diagram (C4-Style)

Use `graph TB` with layered subgraphs for system boundaries.

```mermaid
graph TB
    subgraph "External Systems"
        EXT1[External API<br/>REST]
        EXT2[Identity Provider<br/>OAuth2/OIDC]
        EXT3[Third-party SaaS]
    end

    subgraph "Application Boundary"
        subgraph "Presentation Layer"
            API[REST API<br/>FastAPI/Express]
            WEB[Web UI<br/>React/Next.js]
            GW[API Gateway<br/>Kong/APIGW]
        end

        subgraph "Business Logic"
            SVC1[Core Service<br/>Domain Logic]
            SVC2[Processing Service<br/>Workflows]
            WORKER[Background Worker<br/>Celery/Bull]
            ORCH[Orchestrator<br/>LangGraph/Step Functions]
        end

        subgraph "Data Layer"
            REPO[Repository<br/>SQLAlchemy/Prisma]
            CACHE[Cache Layer<br/>Redis]
            SEARCH[Search<br/>Elasticsearch]
        end
    end

    subgraph "Data Stores"
        DB[(PostgreSQL<br/>Primary)]
        VECTOR[(Vector DB<br/>PgVector/Pinecone)]
        QUEUE[[Message Queue<br/>Kafka/SQS]]
        STORE[(Object Store<br/>S3)]
    end

    WEB --> GW --> API
    API --> SVC1 & SVC2
    SVC1 --> REPO & CACHE
    SVC2 --> ORCH --> WORKER
    WORKER --> QUEUE
    REPO --> DB
    REPO --> VECTOR
    SVC1 --> EXT1
    API --> EXT2
    WORKER --> STORE
    SVC2 --> SEARCH
```

### Guidelines
- Subgraphs represent architectural boundaries (layers, services, external)
- Label nodes: `ID[Logical Name<br/>Technology]`
- Shapes: `[]` services, `[()]` databases, `[[]]` queues/streams, `{{}}` decisions, `([])` events
- Show primary data flow direction with arrows
- Max 4 subgraph nesting levels for readability

---

## 2. Sequence Diagram

For API interactions, service-to-service communication, and protocol flows.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant GW as API Gateway
    participant AUTH as Auth Service
    participant API as Core API
    participant DB as Database
    participant Q as Queue
    participant W as Worker

    C->>GW: POST /api/resource
    GW->>AUTH: Validate token
    AUTH-->>GW: Token valid (user context)

    GW->>API: Forward request + user context
    activate API

    API->>DB: Begin transaction
    API->>DB: INSERT resource
    DB-->>API: Resource created (id: xyz)

    API->>Q: Publish event: resource.created
    Q-->>API: ACK

    API->>DB: COMMIT
    API-->>GW: 201 Created {id: xyz}
    deactivate API

    GW-->>C: 201 Created {id: xyz}

    Note over Q,W: Async processing
    Q->>W: Consume: resource.created
    activate W
    W->>DB: UPDATE resource (enriched)
    W-->>Q: ACK
    deactivate W
```

### Guidelines
- Use `autonumber` for step tracking
- Show `activate`/`deactivate` for long-running operations
- `-->>` for responses, `->>` for requests
- `Note over` for cross-participant context
- Conditional flows:
  ```
  alt Success
      API-->>C: 200 OK
  else Failure
      API-->>C: 500 Error
  end
  ```
- Parallel operations:
  ```
  par Parallel enrichment
      W->>SVC1: Enrich data
  and
      W->>SVC2: Validate data
  end
  ```

---

## 3. Data Model (ERD)

Use `erDiagram` for relational models with entities, attributes, and relationships.

```mermaid
erDiagram
    USER ||--o{ PROJECT : owns
    USER {
        uuid id PK
        string email UK
        string name
        string password_hash
        enum role "admin | member | viewer"
        datetime created_at
        datetime updated_at
    }

    PROJECT ||--|{ FEATURE : contains
    PROJECT ||--o{ COLLABORATOR : has
    PROJECT {
        uuid id PK
        uuid owner_id FK
        string name
        string description
        enum status "draft | active | archived"
        jsonb settings
        datetime created_at
        datetime updated_at
    }

    COLLABORATOR {
        uuid id PK
        uuid project_id FK
        uuid user_id FK
        enum role "editor | viewer"
        datetime invited_at
    }

    FEATURE ||--o{ TASK : "broken into"
    FEATURE {
        uuid id PK
        uuid project_id FK
        string title
        text description
        int priority
        enum status "backlog | in_progress | done"
    }

    TASK ||--o{ COMMENT : has
    TASK {
        uuid id PK
        uuid feature_id FK
        uuid assignee_id FK "nullable"
        string title
        enum status "todo | doing | done"
    }

    COMMENT {
        uuid id PK
        uuid task_id FK
        uuid author_id FK
        text body
        datetime created_at
    }

    USER ||--o{ COMMENT : writes
    USER ||--o{ COLLABORATOR : "participates as"
```

### Guidelines
- Include PK, FK, UK annotations
- Cardinality: `||--o{` (one-to-many), `||--||` (one-to-one), `}o--o{` (many-to-many)
- Verb labels on relationships
- Field types: `string`, `uuid`, `int`, `decimal`, `enum`, `datetime`, `jsonb`, `text`
- Show enum values: `enum status "active | inactive"`
- For NoSQL: use `classDiagram` with embedded document notation

---

## 4. Data Flow Diagram

Use `flowchart LR` for data pipelines showing ingestion, processing, storage, and delivery.

```mermaid
flowchart LR
    subgraph "Ingestion"
        SRC1[API Request] --> VAL{Validate<br/>Schema}
        SRC2[Webhook] --> VAL
        SRC3[File Upload<br/>CSV/JSON] --> PARSE[Parse &<br/>Transform]
        SRC4[Event Stream] --> DEDUP[Deduplicate]
    end

    VAL -->|valid| ENR[Enrich &<br/>Normalize]
    VAL -->|invalid| DLQ[[Dead Letter<br/>Queue]]
    PARSE --> ENR
    DEDUP --> ENR

    subgraph "Processing"
        ENR --> ROUTE{Route by<br/>Type}
        ROUTE -->|type A| PROC_A[Process A<br/>Sync]
        ROUTE -->|type B| PROC_B[Process B<br/>Async]
        PROC_A --> AGG[Aggregate &<br/>Index]
        PROC_B --> QUEUE[[Task Queue]] --> WORKER[Worker<br/>Pool] --> AGG
    end

    subgraph "Storage"
        AGG --> DB[(Primary DB<br/>PostgreSQL)]
        AGG --> CACHE[(Cache<br/>Redis)]
        AGG --> IDX[(Search Index<br/>Elasticsearch)]
    end

    subgraph "Delivery"
        DB --> API_OUT[API Response]
        CACHE --> API_OUT
        IDX --> SEARCH_OUT[Search Results]
        DB --> EXPORT[Reports &<br/>Export]
        DB --> STREAM_OUT[[Event Stream<br/>Downstream]]
    end

    DLQ --> ALERT[Alert &<br/>Retry]
```

### Guidelines
- Left-to-right (LR) for pipelines; top-to-bottom (TB) for request/response
- Show transformation steps, not just connections
- Label edges: `-->|"JSON/REST"|`
- Include error paths (DLQ, retry, alerts)
- Use `[[]]` for queues/async boundaries
- Use `{}` diamonds for routing/decision points

---

## 5. State Machine Diagram

Use `stateDiagram-v2` for lifecycle models and status transitions.

```mermaid
stateDiagram-v2
    [*] --> Draft: Create

    Draft --> InReview: Submit for review
    Draft --> Draft: Edit

    InReview --> Approved: Approve
    InReview --> Draft: Request changes
    InReview --> Rejected: Reject

    Approved --> Active: Deploy
    Approved --> Draft: Revoke approval

    Active --> Paused: Pause
    Active --> Deprecated: Deprecate

    Paused --> Active: Resume
    Paused --> Deprecated: Deprecate

    Deprecated --> Archived: Archive (after 90 days)
    Rejected --> Archived: Archive

    Archived --> [*]

    state InReview {
        [*] --> PeerReview
        PeerReview --> SecurityReview: Peer approved
        SecurityReview --> FinalApproval: Security cleared
        FinalApproval --> [*]
    }

    note right of Active
        Monitored state:
        - Health checks every 30s
        - Auto-pause on 3 failures
    end note
```

### Guidelines
- Start with `[*] -->` for initial state
- End with `--> [*]` for terminal states
- Label transitions with triggering action/event
- Nested `state` blocks for composite states
- `note` blocks for constraints or behaviors
- `<<choice>>` for conditional transitions
- `<<fork>>` and `<<join>>` for parallel states

---

## 6. Dependency Graph

Use `graph TD` for module/package dependencies with architectural grouping.

```mermaid
graph TD
    subgraph "API Layer"
        REST[api/rest<br/>Controllers]
        GQL[api/graphql<br/>Resolvers]
        MW[api/middleware<br/>Auth, Logging]
    end

    subgraph "Domain Layer"
        CORE[domain/core<br/>Entities, Values]
        SVC_USER[domain/user<br/>User Service]
        SVC_PROJECT[domain/project<br/>Project Service]
        SVC_BILLING[domain/billing<br/>Billing Service]
    end

    subgraph "Infrastructure"
        DB_LAYER[infra/database<br/>Repositories]
        CACHE_LAYER[infra/cache<br/>Redis Client]
        QUEUE_LAYER[infra/queue<br/>Message Broker]
        HTTP_CLIENT[infra/http<br/>External Calls]
    end

    subgraph "Shared"
        CONFIG[shared/config<br/>Environment]
        LOGGER[shared/logging<br/>Structured Logs]
        ERRORS[shared/errors<br/>Error Types]
    end

    REST & GQL --> MW
    REST --> SVC_USER & SVC_PROJECT & SVC_BILLING
    GQL --> SVC_USER & SVC_PROJECT

    SVC_USER --> CORE & DB_LAYER & CACHE_LAYER
    SVC_PROJECT --> CORE & DB_LAYER & QUEUE_LAYER
    SVC_BILLING --> CORE & DB_LAYER & HTTP_CLIENT

    DB_LAYER & CACHE_LAYER & QUEUE_LAYER --> CONFIG & LOGGER & ERRORS
    CORE --> ERRORS

    linkStyle 0,1 stroke:#2196F3
```

### Guidelines
- Group by architectural layer/domain
- Show only direct dependencies (not transitive)
- `linkStyle` with colors for layer-to-layer connections
- Highlight circular dependencies: `linkStyle N stroke:red,stroke-width:3px`
- Package-level for large codebases, file-level for small ones
- High fan-in = high change impact (annotate these)

---

## 7. C4 Context Diagram

For formal system context documentation.

```mermaid
C4Context
    title System Context Diagram

    Person(user, "End User", "Uses the application via web browser")
    Person(admin, "Administrator", "Manages system configuration")

    System(system, "Our Application", "Core platform providing business value")

    System_Ext(idp, "Identity Provider", "OAuth2/OIDC authentication")
    System_Ext(payment, "Payment Gateway", "Processes financial transactions")
    System_Ext(email, "Email Service", "Sends transactional emails")

    Rel(user, system, "Uses", "HTTPS")
    Rel(admin, system, "Administers", "HTTPS")
    Rel(system, idp, "Authenticates via", "OAuth2")
    Rel(system, payment, "Processes payments", "REST API")
    Rel(system, email, "Sends notifications", "SMTP/API")
```

---

## 8. C4 Container Diagram

```mermaid
C4Container
    title Container Diagram

    Person(user, "User", "End user of the platform")

    System_Boundary(boundary, "Application") {
        Container(web, "Web App", "React/Next.js", "Single-page application")
        Container(api, "API Server", "Python/FastAPI", "REST API backend")
        Container(worker, "Worker", "Python/Celery", "Async task processing")

        ContainerDb(db, "Database", "PostgreSQL", "Primary data store")
        ContainerDb(cache, "Cache", "Redis", "Session and data cache")
        ContainerDb(queue, "Message Queue", "RabbitMQ", "Task queue")
    }

    System_Ext(llm, "LLM Provider", "Claude API for AI features")
    System_Ext(storage, "Object Storage", "S3 for file uploads")

    Rel(user, web, "Uses", "HTTPS")
    Rel(web, api, "Calls", "REST/JSON")
    Rel(api, db, "Reads/Writes", "SQL")
    Rel(api, cache, "Caches", "Redis Protocol")
    Rel(api, queue, "Publishes", "AMQP")
    Rel(worker, queue, "Consumes", "AMQP")
    Rel(worker, llm, "Calls", "HTTPS")
    Rel(api, storage, "Stores files", "S3 API")
```

---

## 9. Workflow / Decision Flowchart

For business processes, approval flows, and decision trees.

```mermaid
flowchart TD
    START([Start]) --> INPUT[Receive Request]
    INPUT --> VALIDATE{Valid<br/>Input?}

    VALIDATE -->|No| ERROR[Return 400<br/>Validation Error]
    VALIDATE -->|Yes| AUTH{Authorized?}

    AUTH -->|No| DENY[Return 403<br/>Forbidden]
    AUTH -->|Yes| CHECK{Resource<br/>Exists?}

    CHECK -->|No| CREATE[Create Resource]
    CHECK -->|Yes| UPDATE[Update Resource]

    CREATE --> ENRICH[Enrich Data]
    UPDATE --> ENRICH

    ENRICH --> PERSIST[Persist to DB]
    PERSIST --> EVENT[Emit Domain Event]
    EVENT --> RESPOND[Return Success]

    ERROR --> END([End])
    DENY --> END
    RESPOND --> END

    style ERROR fill:#ffcdd2
    style DENY fill:#ffcdd2
    style RESPOND fill:#c8e6c9
    style START fill:#e3f2fd
    style END fill:#e3f2fd
```

### Guidelines
- `([])` for start/end (stadium shape)
- `{}` diamonds for decisions
- `[]` rectangles for actions
- Color-code: green success, red errors, blue start/end
- Max 2-3 decision branches per diamond
- Happy path in center/left

---

## 10. Class Diagram

For domain models and object hierarchies.

```mermaid
classDiagram
    class BaseEntity {
        <<abstract>>
        +UUID id
        +DateTime created_at
        +DateTime updated_at
        +save() void
        +delete() void
    }

    class User {
        +String email
        +String name
        +Role role
        +authenticate(credentials) Token
        +authorize(permission) bool
    }

    class Project {
        +String name
        +ProjectStatus status
        +addCollaborator(user, role) void
        +archive() void
    }

    class Repository~T~ {
        <<interface>>
        +find(id) T
        +findAll(filter) List~T~
        +save(entity) T
        +delete(id) void
    }

    BaseEntity <|-- User
    BaseEntity <|-- Project
    Repository~T~ <|.. UserRepository
    User "1" --> "*" Project: owns
```

### Guidelines
- Stereotypes: `<<abstract>>`, `<<interface>>`, `<<enum>>`, `<<service>>`
- Visibility: `+` public, `-` private, `#` protected
- Include key methods only (not all)
- Generics: `Repository~T~`
- Relations: `<|--` inheritance, `<|..` implementation, `-->` association, `--o` aggregation, `--*` composition

---

## Styling Reference

### Theme Configuration

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
    'primaryColor': '#1976D2',
    'primaryTextColor': '#fff',
    'primaryBorderColor': '#0D47A1',
    'lineColor': '#546E7A',
    'secondaryColor': '#26A69A',
    'tertiaryColor': '#FFF3E0'
}}}%%
```

### Style Classes

```
classDef primary fill:#1976D2,stroke:#0D47A1,color:#fff
classDef secondary fill:#26A69A,stroke:#00897B,color:#fff
classDef danger fill:#E53935,stroke:#B71C1C,color:#fff
classDef success fill:#43A047,stroke:#1B5E20,color:#fff
classDef external fill:#78909C,stroke:#37474F,color:#fff

class API,SVC primary
class EXT1,EXT2 external
class ERROR danger
```

---

## Best Practices

1. **Readability over completeness** — 15-20 clear nodes beats 50 crowded ones
2. **One concept per diagram** — separate architecture from data flow
3. **Use project-specific names** — not generic placeholders
4. **Direction matters** — TB for hierarchies, LR for flows, TD for dependencies
5. **Label all edges** — unlabeled arrows are ambiguous
6. **Group logically** — subgraphs = real boundaries (network, team, domain)
7. **Keep text short** — use `<br/>` for line breaks within nodes

---

## Syntax Rules — Prevent Silent Failures

**One violation makes Mermaid drop the entire block silently** — the diagram vanishes in both the Markdown preview and the rendered HTML, with no error banner. These rules apply to every ```mermaid fence in every diagram type, not just sequenceDiagram. The AAH validator gate (`aah run core.gates.validate_mermaid`) enforces them deterministically — but this section exists so authors write clean fences the first time.

1. **Quote labels containing anything outside `[A-Za-z0-9_ ]`.** Any of `(` `)` `+` `.` `/` `:` `-` `,` `&` `<` `>` `"` `'` `[` `]` `{` `}` or non-ASCII in a `participant`/`actor`/node label → wrap it in double quotes.
   - ✅ `participant BE as "FastAPI+LangGraph (ALB)"`
   - ✅ `A["User (browser)"] --> B["API Gateway"]`
   - ❌ `participant BE as FastAPI+LangGraph (ALB)` — `+` and `(` break the lexer

2. **Never use `<placeholder>` or `<name>` in message/Note/edge text.** Mermaid parses `<` as an arrow, so `WHERE user_id=<id>` is read as a malformed arrow to a phantom participant.
   - ✅ `CLI->>RDS: DELETE audit_log WHERE user_id = {user_id}`
   - ✅ `CLI->>S3: DeleteObjects, prefix audit/user=user_id/`
   - ❌ `CLI->>S3: DeleteObjects, prefix audit/user=<user_id>/`

3. **No backticks inside message text.** They render as literal `` ` `` and are noise — keep identifiers plain: `intent_classifier` not `` `intent_classifier` ``.

4. **ASCII only in fence content.** Use `->` not `→`, `-` not `—`, straight quotes `"` `'`. Copy-paste from user prose is the usual culprit.

5. **No semicolons as statement separators in message text.** Mermaid can misread `BEGIN; UPDATE ...` — split into multiple arrows or join with commas.

6. **Reserved words as bare node/participant IDs are forbidden.** `end`, `subgraph`, `class`, `click`, `link` — always alias them (`participant END_NODE as "End"`).

7. **One diagram type per fence.** A `sequenceDiagram` block must not contain `flowchart`/`graph`/`erDiagram` directives, and vice versa. Split into separate fences.

### How this is validated

Any AAH phase that generates architecture docs (`aah-arch`, `aah-codebase-profile`) runs the validator gate before closing:

```bash
aah run core.gates.validate_mermaid --project-path "$PROJECT_DIR"
```

The gate scans every `.md` **and** `.html` under `.aah/architecture/` (recursive), extracts fences from ```mermaid blocks, `<script type="text/markdown">` tags, and `<div class="mermaid">` elements, and applies the 7 rules above. Non-zero exit lists each `<file> (block #N): line <L>: <reason>` — the phase agent then edits the offender, keeps any `.md`/`.html` pair in sync, and re-runs. Up to 3 attempts; still failing → the phase surfaces the remaining issues to the user.
