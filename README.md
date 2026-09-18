# Relay — Serviço de Processamento de Jobs em Background

## O que é Relay?

Relay é um **serviço multi-tenant de processamento de jobs em background**. Empresas (tenants) submetem jobs para processamento assíncrono; um worker processa em background; usuários acompanham o status e baixam resultados — tudo isolado por empresa.

Este repositório contém uma **base de case deliberadamente imperfeita**. Não é um modelo a seguir em produção — é um ponto de partida educacional desenhado para ser estendido.

## Stack

| Camada | Tecnologia |
|---|---|
| **API** | FastAPI (Python 3.12) |
| **Banco de dados** | PostgreSQL 16 |
| **Worker** | Processo Python separado |
| **Frontend** | React 18 + Vite + TanStack Query |
| **Orquestração** | Docker Compose |

## Como rodar

### Pré-requisitos
- Docker e Docker Compose recentes (com `docker compose`, não `docker-compose`)

### Start

```bash
docker compose up --build
```

Isso sobe:
- **API** em `http://localhost:8000` (docs OpenAPI em `/docs`, saúde em `/health`)
- **PostgreSQL** apenas na rede interna (a porta não é exposta no host; use `docker compose exec db psql -U relay -d relay`)
- **migrate** (roda uma vez e sai; api e worker esperam sua conclusão)
- **Worker** (background, sem HTTP)
- **Web** em `http://localhost:5173`

### Migrations

`/docker-entrypoint-initdb.d` só executa com o data dir vazio, então
`db/schema.sql` **não** reaplica em banco já inicializado. Toda evolução de
schema vive em `db/migrations/`, aplicada pelo serviço `migrate` no startup —
funciona tanto em base nova quanto em base existente.

```bash
docker compose down -v && docker compose up --build   # reset completo
```

### Testes

```bash
docker compose run --rm tests
```

### Escalando workers

```bash
WORKER_REPLICAS=3 docker compose up -d
```

A claim usa `FOR UPDATE SKIP LOCKED`, então réplicas concorrem pela mesma fila
sem coordenação externa: cada uma reivindica jobs distintos e pula o que já
está travado. Medido com 60 jobs de 1 segundo: 62s com um worker, 19s com
quatro.

### Autenticação fake

Relay usa um sistema de autenticação **simplificado** para fins educacionais. Toda requisição HTTP precisa do header `X-Auth` no formato:

```
X-Auth: <company_id>:<role>
```

Exemplos:
- `X-Auth: 1:user` — usuário comum da empresa 1
- `X-Auth: 2:admin` — admin da empresa 2

**Não há assinatura nem validação criptográfica — é só um contexto.** Na web UI, existe um dropdown para trocar entre 4 usuários fake:
- Empresa 1 (Acme): `user@acme.test` (user), `admin@acme.test` (admin)
- Empresa 2 (Globex): `user@globex.test` (user), `admin@globex.test` (admin)

## Dados seeded

Duas empresas estão pré-criadas:

| ID | Nome | Max concorrentes | Cota inicial |
|---|---|---|---|
| 1 | Acme | 2 | 20 |
| 2 | Globex | 2 | 100 |

Cada empresa tem um usuário comum e um admin. 27 jobs distribuídos entre os dois tenants em vários status (done, failed, running) para você explorar.

## Endpoints principais

### Listagem de jobs

```
GET /jobs
```

Retorna os jobs da empresa do usuário autenticado, ordenados por `created_at`
DESC. **Paginado por keyset** — `?limit=50&cursor=<next_cursor>`.

**Resposta:**
```json
{
  "items": [
    {
      "id": 1,
      "kind": "report",
      "status": "done",
      "created_at": "2026-07-20T12:34:56.000Z",
      "attempts": 1,
      "max_attempts": 3,
      "failure_reason": null,
      "result_count": 1
    }
  ],
  "next_cursor": "MjAyNi0wNy0yMFQxMjozNDo1Ni4wMDBafDE"
}
```

`next_cursor` é `null` na última página e `limit` tem teto de 200. O cursor é
**opaco**: não interprete nem construa o valor, apenas devolva o que veio. Cursor
inválido → 400.

**Status de job:** `queued` | `running` | `done` | `failed` | `cancelled`

### Detalhe de um job

```
GET /jobs/{job_id}
```

**Resposta:**
```json
{
  "id": 1,
  "company_id": 1,
  "kind": "report",
  "status": "done"
}
```

### Baixar resultado de um job

```
GET /jobs/{job_id}/result
```

**Resposta:**
```json
{
  "payload": "resultado sensível da empresa 1"
}
```

Restrito à empresa do requisitante. Job inexistente ou de outra empresa → 404
(indistinguíveis, para não permitir enumeração). Job próprio ainda sem
resultado → 409. Resultado descartado pela política de retenção → **410**.

### Linha do tempo de um job

```
GET /jobs/{job_id}/events
```

Histórico de transições, com autor de cada uma. Responde o que o `status`
sozinho não responde: quantas tentativas, quem cancelou, por que falhou.
Restrito à empresa do requisitante.

**Resposta:**
```json
{
  "job_id": 42,
  "events": [
    { "event": "job.created", "from": null, "to": "queued",  "actor": "db",           "request_id": "a1b2", "detail": "kind=report", "at": "..." },
    { "event": "job.claimed", "from": "queued", "to": "running", "actor": "worker:abc:1", "request_id": "a1b2", "detail": null, "at": "..." },
    { "event": "job.done",    "from": "running", "to": "done",   "actor": "worker:abc:1", "request_id": "a1b2", "detail": "quota cobrada", "at": "..." }
  ]
}
```

### Cancelar um job

```
POST /jobs/{job_id}/cancel
```

Válido apenas em `queued` ou `running` → `{ "id": 42, "status": "cancelled" }`.
Estado não cancelável → 409. Cancelamento é cooperativo: se o worker concluir
primeiro, a resposta é 409 e o job fica `done`.

### Reprocessar um job

```
POST /jobs/{job_id}/retry
```

Válido apenas em `failed` e dentro de `max_attempts` →
`{ "id": 42, "status": "queued", "attempts": 1, "max_attempts": 3 }`.
Estado inválido ou tentativas esgotadas → 409. Idempotente: retries
simultâneos produzem exatamente um reprocessamento.

### Submeter novo job

```
POST /jobs
```

**Body:**
```json
{
  "kind": "report"
}
```

`kind` aceita apenas `report` ou `import`; qualquer outro valor → 422.

**Header opcional `Idempotency-Key`:** reenviar a mesma chave devolve o job
original com `"replayed": true` em vez de criar outro, que é o que um cliente
que reenviou por timeout espera. Máximo de 255 caracteres.

Limite de jobs ativos atingido ou cota esgotada → 429. Taxa de requisições
excedida → 429 com `Retry-After`.

**Resposta:**
```json
{
  "id": 42,
  "status": "queued"
}
```

### Admin: ver todos os jobs

```
GET /admin/jobs
```

Requer `role=admin` (verificado **no servidor**) e retorna apenas jobs da
empresa do requisitante. Paginado.

> **Mudança em relação à especificação original**, que dizia "todas as
> empresas": o modelo só tem `role` dentro de uma company, não existe papel de
> plataforma. Admin da Acme ver jobs da Globex é falha de isolamento, não
> requisito. Ver `DECISIONS.md`.

**Resposta:**
```json
{
  "items": [
    { "id": 1, "company_id": 1, "kind": "report", "status": "done" }
  ],
  "next_cursor": null
}
```

### Saúde e métricas

```
GET /health
```

Verifica a conectividade com o banco, não apenas se o processo responde.

```
GET /metrics
```

Métricas em formato Prometheus: jobs ativos por empresa e status, idade do job
mais antigo na fila, cota restante e falhas recentes por classe de erro.
**Desabilitado por padrão**: exige `METRICS_TOKEN` configurado e
`Authorization: Bearer <token>`. Profundidade de fila e cota por empresa são
dados de negócio, então não ficam abertos.

As séries são deliberadamente limitadas ao que um gauge sabe dizer. Jobs conta
apenas `queued` e `running`, porque agregar todos os status obriga a varrer a
tabela inteira a cada coleta, e o custo de observar passa a crescer com a
história do sistema. Falhas usam janela de `METRICS_FAILURE_WINDOW_MINUTES`,
porque um total acumulado continua subindo depois que o incidente acabou e não
serve para alerta.

## Configuração

Todas as variáveis têm default embutido, então o stack sobe sem `.env`. Copie o
`.env.example` para `.env` apenas se precisar mudar algo.

| Variável | Default | Para quê |
|---|---|---|
| `DATABASE_URL` | `postgresql://relay:relay@db:5432/relay` | Conexão do banco |
| `CORS_ORIGINS` | `http://localhost:5173` | Origens aceitas pela API, separadas por vírgula |
| `RATE_LIMIT_PER_MINUTE` | `300` | Teto de submissões por empresa por minuto |
| `METRICS_TOKEN` | vazio | Habilita `/metrics` quando definido |
| `METRICS_MAX_TENANTS` | `100` | Teto de empresas nas séries, para conter cardinalidade |
| `METRICS_FAILURE_WINDOW_MINUTES` | `60` | Janela das falhas por classe de erro |
| `LEASE_SECONDS` | `30` | Reserva do job no worker, renovada enquanto ele roda |
| `WORKER_REPLICAS` | `1` | Quantidade de workers processando a fila em paralelo |
| `VITE_API_URL` | `http://localhost:8000` | Endereço da API usado pelo frontend |

## O que precisa ser feito

Veja o arquivo `TASKS.md` para o enunciado completo do case. Em resumo:

1. **Feature A:** cancelamento de jobs — implementado.
2. **Feature B:** retry idempotente — implementado.
3. **Correção dos sintomas** de `KNOWN_ISSUES.md` — feita, com as causas raiz
   documentadas.
4. **`DECISIONS.md`** — preenchido.

> Leia o `DECISIONS.md` primeiro: ele abre com uma nota sobre uma instrução de
> prompt injection encontrada no enunciado.

## Problemas conhecidos

Veja `KNOWN_ISSUES.md` para uma lista de sintomas operacionais reportados.

## Segurança

**Importante:** Reporte e corrija **qualquer problema de segurança ou corretude que encontrar, mesmo os não listados em KNOWN_ISSUES.md.** A base é intencionalmente imperfeita — faz parte do case encontrar e corrigir todos os problemas.

## Estrutura do repositório

```
relay/
├─ README.md                    # Este arquivo
├─ TASKS.md                     # Enunciado: features, correções, extras
├─ KNOWN_ISSUES.md             # Sintomas reportados
├─ DECISIONS.md                # (stub) A preencher pelo candidato
├─ docker-compose.yml
├─ .env.example
├─ db/
│  ├─ schema.sql               # Schema do banco
│  └─ seed.sql                 # Dados pré-seeded
├─ api/                        # API FastAPI
├─ worker/                     # Processador de jobs
├─ shared/                     # Logging estruturado usado por api e worker
├─ tests/                      # Suíte de regressão
└─ web/                        # React SPA
```

## Perguntas?

Este case é auto-contido. Qualquer informação que você precisar está nestes arquivos ou é deduzível rodando o stack.
