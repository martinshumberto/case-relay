"""Testes de regressão dos defeitos corrigidos.

Critério de seleção: cada teste aqui FALHA na base original. Não há testes de
cobertura decorativa — o que importa é travar as classes de bug que voltariam
sem ninguém perceber, sobretudo concorrência e isolamento, que não se
verificam por inspeção de código.

Executar com o stack no ar:
    docker compose run --rm tests
"""

import os
import threading
import uuid

import psycopg
import pytest
import requests

API = os.environ.get("API_URL", "http://api:8000")
DSN = os.environ["DATABASE_URL"]

ACME, GLOBEX = "1:user", "2:user"
ACME_ADMIN = "1:admin"


def sql(query, params=None, fetch=True):
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        result = cur.fetchall() if fetch and cur.description else None
        conn.commit()
        return result


@pytest.fixture
def job_da_acme():
    """Job pertencente à company 1, com resultado gravado.

    Usa apenas colunas do schema ORIGINAL de propósito. A fixture chegou a
    referenciar `quota_charged`, e o efeito foi que os seis testes de
    isolamento — os mais importantes da suíte — davam *erro* em vez de *falha*
    quando executados contra a base sem correção. Um teste que não roda contra
    o código defeituoso não prova que pega o defeito.
    """
    rows = sql("INSERT INTO jobs (company_id, kind, status) VALUES (1,'report','done') RETURNING id")
    job_id = rows[0][0]
    sql(
        "INSERT INTO job_results (job_id, payload) VALUES (%s,%s)",
        (job_id, "resultado sensível da empresa 1"),
        fetch=False,
    )
    return job_id


@pytest.fixture
def folga_de_quota():
    """Remove limites para os testes que não são sobre limites."""
    sql("UPDATE companies SET job_quota=10000, max_concurrent_jobs=500", fetch=False)
    yield
    sql("UPDATE jobs SET status='done' WHERE status IN ('queued','running')", fetch=False)


# --------------------------------------------------------------------------
# Isolamento entre tenants — nenhum destes estava reportado no KNOWN_ISSUES.md
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rota", ["/jobs/{id}", "/jobs/{id}/result"])
def test_tenant_nao_le_recurso_alheio(job_da_acme, rota):
    r = requests.get(API + rota.format(id=job_da_acme), headers={"X-Auth": GLOBEX})
    assert r.status_code == 404, "IDOR: outro tenant acessou o recurso"


@pytest.mark.parametrize("rota", ["/jobs/{id}/cancel", "/jobs/{id}/retry"])
def test_tenant_nao_muda_estado_alheio(job_da_acme, rota):
    """Isolamento em cancel e retry.

    A asserção de 404 sozinha passaria pelo motivo errado numa base onde a rota
    simplesmente não existe — 404 de rota ausente é indistinguível de 404 de
    recurso protegido. Por isso o teste exige também que o DONO receba 409
    (o job está 'done', logo não é cancelável nem reprocessável): isso prova
    que a rota existe, responde, e discrimina por tenant.
    """
    url = API + rota.format(id=job_da_acme)

    assert requests.post(url, headers={"X-Auth": GLOBEX}).status_code == 404

    resposta_do_dono = requests.post(url, headers={"X-Auth": ACME})
    assert resposta_do_dono.status_code == 409, (
        "a rota precisa existir e responder ao dono — senão o 404 acima não "
        f"prova isolamento (veio {resposta_do_dono.status_code})"
    )


def test_dono_acessa_o_proprio_recurso(job_da_acme):
    r = requests.get(f"{API}/jobs/{job_da_acme}/result", headers={"X-Auth": ACME})
    assert r.status_code == 200
    assert "empresa 1" in r.json()["payload"]


def test_usuario_comum_nao_acessa_endpoint_admin():
    assert requests.get(f"{API}/admin/jobs", headers={"X-Auth": ACME}).status_code == 403


def test_admin_ve_apenas_o_proprio_tenant():
    r = requests.get(f"{API}/admin/jobs?limit=200", headers={"X-Auth": ACME_ADMIN})
    assert r.status_code == 200
    items = r.json()["items"]
    # Sem isto, uma resposta vazia satisfaria a checagem de subconjunto.
    assert items, "resposta vazia não prova isolamento"
    assert {j["company_id"] for j in items} <= {1}


@pytest.mark.parametrize(
    "header", ["abc:user", "1:superadmin", "1:user:extra", "1", "", "0:user"]
)
def test_auth_malformado_nunca_vira_5xx(header):
    r = requests.get(f"{API}/jobs", headers={"X-Auth": header})
    assert r.status_code == 401, f"esperado 401, veio {r.status_code}"


# --------------------------------------------------------------------------
# Concorrência — não reproduzível sem disparo genuinamente simultâneo
# --------------------------------------------------------------------------

def _post_concorrente(path, n, auth=ACME, body=None):
    """Dispara n POSTs no mesmo instante.

    Duas precauções que decidem se o teste realmente pega a corrida:

    1. Sessão por thread, com uma requisição de aquecimento ANTES da barreira.
       Sem isso, o handshake TCP acontece dentro da janela e serializa as
       threads o bastante para a corrida não abrir. Medido contra o código
       defeituoso: sem aquecimento o teste pegava a falha em 3 de 5 execuções
       (ou seja, passaria em CI e deixaria a regressão vazar); com aquecimento,
       falha de forma consistente.

    2. A barreira solta todas as threads já conectadas, no mesmo instante.
    """
    barreira = threading.Barrier(n)
    codigos = []
    trava = threading.Lock()

    def worker():
        sessao = requests.Session()
        try:
            sessao.get(f"{API}/health", timeout=5)  # aquece a conexão
        except Exception:
            pass
        barreira.wait()
        try:
            codigo = sessao.post(API + path, headers={"X-Auth": auth}, json=body, timeout=30).status_code
        except Exception:
            codigo = 0
        with trava:
            codigos.append(codigo)
        sessao.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return codigos


@pytest.mark.parametrize("rodada", range(3))
def test_limite_de_concorrencia_resiste_a_submissoes_simultaneas(rodada):
    """Original: 11 aceitos com limite 2 (check-then-act sem lock).

    O roteiro do KNOWN_ISSUES.md (dois curl &) não reproduz — o overhead de
    processo serializa o bastante para fechar a janela.

    Repetido em rodadas de propósito: corrida que passa uma vez não foi testada.
    """
    sql("UPDATE jobs SET status='done' WHERE company_id=1 AND status IN ('queued','running')", fetch=False)
    sql("UPDATE companies SET max_concurrent_jobs=2, job_quota=1000 WHERE id=1", fetch=False)
    try:
        codigos = _post_concorrente("/jobs", 30, body={"kind": "report"})
        ativos = sql(
            "SELECT count(*) FROM jobs WHERE company_id=1 AND status IN ('queued','running')"
        )[0][0]
    finally:
        # Sem isto a company 1 fica com limite 2 para os testes seguintes que
        # não usam a fixture de folga.
        sql("UPDATE companies SET max_concurrent_jobs=5000, job_quota=100000 WHERE id=1",
            fetch=False)
    # A asserção que importa é a invariante no banco. O 429 pode vir do limite
    # de concorrência OU do rate limiter — as duas são recusas legítimas, e
    # acoplar o teste a qual delas disparou o tornaria frágil.
    assert codigos.count(200) <= 2, f"limite furado: {codigos.count(200)} aceitos"
    assert ativos <= 2, f"{ativos} jobs ativos com limite 2"


def test_paginacao_percorre_sem_duplicata_nem_lacuna(folga_de_quota):
    """Keyset sobre (created_at, id).

    OFFSET seria instável aqui: a ordenação é created_at DESC e jobs novos
    entram no topo, então páginas subsequentes veriam itens repetidos.
    """
    sql("INSERT INTO jobs (company_id,kind,status) SELECT 1,'report','done'"
        " FROM generate_series(1,45)", fetch=False)

    esperados = {r[0] for r in sql(
        "SELECT id FROM jobs WHERE company_id=1 ORDER BY created_at DESC, id DESC LIMIT 100"
    )}

    vistos, cursor, paginas = [], None, 0
    while paginas < 10:
        url = f"{API}/jobs?limit=10" + (f"&cursor={cursor}" if cursor else "")
        d = requests.get(url, headers={"X-Auth": ACME}).json()
        vistos += [(j["created_at"], j["id"]) for j in d["items"]]
        paginas += 1
        cursor = d["next_cursor"]
        if not cursor:
            break

    ids = [i for _, i in vistos]
    assert len(ids) == len(set(ids)), "id repetido entre páginas"
    # A chave de ordenação é (created_at, id), não id sozinho: created_at vem de
    # now(), que é o início da transação, então uma transação iniciada antes e
    # commitada depois recebe timestamp menor com id maior.
    assert vistos == sorted(vistos, reverse=True), "ordenação instável entre páginas"
    assert paginas >= 4, f"esperava várias páginas, percorreu {paginas}"
    # Sem esta asserção, uma paginação que pulasse itens passaria: "sem
    # duplicata" e "ordenada" não implicam "completa".
    faltando = esperados - set(ids)
    assert not faltando, f"paginação pulou {len(faltando)} jobs"


def test_cursor_malformado_retorna_400():
    """Cursor inválido é erro do cliente. Antes de ser opaco, o timestamp
    continha '+' — que numa query string vira espaço — e o cast no Postgres
    estourava como 500."""
    r = requests.get(f"{API}/jobs?cursor=isto-nao-e-um-cursor", headers={"X-Auth": ACME})
    assert r.status_code == 400, f"esperado 400, veio {r.status_code}"


def test_cursor_respeita_fronteira_do_tenant(folga_de_quota):
    """O cursor não pode virar um jeito de alcançar jobs de outra empresa."""
    d = requests.get(f"{API}/jobs?limit=5", headers={"X-Auth": ACME}).json()
    if not d["next_cursor"]:
        pytest.skip("volume insuficiente para paginar")
    outra = requests.get(f"{API}/jobs?limit=5&cursor={d['next_cursor']}",
                         headers={"X-Auth": GLOBEX}).json()
    ids_globex = {j["id"] for j in outra["items"]}
    donos = sql("SELECT DISTINCT company_id FROM jobs WHERE id = ANY(%s)",
                (list(ids_globex),)) if ids_globex else []
    assert all(d[0] == 2 for d in donos), "cursor vazou jobs de outro tenant"


def test_quota_esgotada_recusa_na_submissao(folga_de_quota):
    """Original: quota nunca era verificada e chegava a -32."""
    sql("UPDATE companies SET job_quota=0 WHERE id=1", fetch=False)
    try:
        r = requests.post(f"{API}/jobs", headers={"X-Auth": ACME}, json={"kind": "report"})
        assert r.status_code == 429
    finally:
        sql("UPDATE companies SET job_quota=100000 WHERE id=1", fetch=False)


def test_rate_limit_barra_laco_descontrolado(folga_de_quota):
    """SEC-12: não havia throttle algum — um cliente em laço consumia conexões
    e CPU indefinidamente em requisições que seriam recusadas.

    Não presume o valor do limite: dispara sequencialmente até ser barrado, com
    teto. Presumir o número acoplaria o teste à configuração, e foi assim que eu
    quase caí na armadilha de ajustar o limite de produção para o teste passar.

    Usa a empresa 2 para não gastar o orçamento das outras baterias — a janela
    é por tenant.
    """
    sessao = requests.Session()
    TETO = 500
    for i in range(TETO):
        r = sessao.post(f"{API}/jobs", headers={"X-Auth": "2:user"}, json={"kind": "report"})
        if r.status_code == 429 and "cadência" in r.json().get("detail", ""):
            assert r.headers.get("Retry-After"), "429 de rate limit sem Retry-After"
            return
    pytest.fail(f"{TETO} requisições sequenciais não foram barradas pelo rate limiter")


def test_quota_nunca_fica_negativa():
    assert sql("SELECT count(*) FROM companies WHERE job_quota < 0")[0][0] == 0


# --------------------------------------------------------------------------
# Idempotência
# --------------------------------------------------------------------------

def test_retry_concorrente_aceita_exatamente_um(folga_de_quota):
    """Original: sem trava alguma; N cliques produziriam N reprocessamentos."""
    job_id = sql(
        "INSERT INTO jobs (company_id, kind, status, failure_reason, failed_at)"
        " VALUES (1,'report','failed','falha simulada',now()) RETURNING id"
    )[0][0]

    codigos = _post_concorrente(f"/jobs/{job_id}/retry", 10)

    assert codigos.count(200) == 1, f"esperado 1 sucesso, veio {codigos.count(200)}"
    # Conta relativa, não absoluta: _post_concorrente registra 0 em erro de rede,
    # e uma falha de transporte não deveria reprovar a idempotência.
    respondidos = [c for c in codigos if c in (200, 409)]
    assert codigos.count(409) == len(respondidos) - 1


def test_chave_de_idempotencia_sob_criacao_concorrente(folga_de_quota):
    """Chave NOVA disparada em paralelo, não replay de chave já existente.

    A distinção importa: com a chave já commitada, toda requisição pega o
    caminho de replay e a corrida nunca abre. Testando assim, 12 requisições
    simultâneas produziam 2 a 7 respostas 500, porque a consulta de replay roda
    antes do lock da empresa e várias passavam pelo INSERT.
    """
    chave = f"corrida-{uuid.uuid4().hex}"
    barreira = threading.Barrier(12)
    respostas, trava = [], threading.Lock()

    def submeter():
        sessao = requests.Session()
        try:
            sessao.get(f"{API}/health", timeout=5)
        except Exception:
            pass
        barreira.wait()
        try:
            r = sessao.post(f"{API}/jobs", headers={"X-Auth": ACME, "Idempotency-Key": chave},
                            json={"kind": "report"}, timeout=30)
            item = (r.status_code, r.json().get("id") if r.ok else None)
        except Exception:
            item = (0, None)
        with trava:
            respostas.append(item)

    threads = [threading.Thread(target=submeter) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not [c for c, _ in respostas if c >= 500], f"erro de servidor na corrida: {respostas}"
    assert len({i for _, i in respostas if i}) == 1, "a mesma chave gerou jobs distintos"
    assert sql("SELECT count(*) FROM jobs WHERE company_id=1 AND idempotency_key=%s",
               (chave,))[0][0] == 1


def test_resultado_unico_por_job_e_garantido_pelo_banco():
    """A garantia é a constraint, não a lógica da aplicação."""
    job_id = sql("INSERT INTO jobs (company_id,kind,status) VALUES (1,'report','done') RETURNING id")[0][0]
    sql("INSERT INTO job_results (job_id,payload) VALUES (%s,'a')", (job_id,), fetch=False)

    with pytest.raises(psycopg.errors.UniqueViolation):
        sql("INSERT INTO job_results (job_id,payload) VALUES (%s,'b')", (job_id,), fetch=False)


def test_nenhum_job_tem_resultado_duplicado():
    duplicados = sql(
        "SELECT count(*) FROM (SELECT job_id FROM job_results GROUP BY job_id HAVING count(*)>1) t"
    )[0][0]
    assert duplicados == 0


# --------------------------------------------------------------------------
# Máquina de estados
# --------------------------------------------------------------------------

def test_cancelar_job_nao_cancelavel_retorna_409(job_da_acme):
    r = requests.post(f"{API}/jobs/{job_da_acme}/cancel", headers={"X-Auth": ACME})
    assert r.status_code == 409


def test_cancel_e_retry_devolvem_o_corpo_documentado():
    """O TASKS.md especifica o shape da resposta das duas features, não só o
    status code."""
    job_id = sql("INSERT INTO jobs (company_id,kind,status) VALUES (1,'report','queued')"
                 " RETURNING id")[0][0]
    corpo = requests.post(f"{API}/jobs/{job_id}/cancel", headers={"X-Auth": ACME}).json()
    assert corpo == {"id": job_id, "status": "cancelled"}

    falhado = sql("INSERT INTO jobs (company_id,kind,status,attempts,failure_reason,failed_at)"
                  " VALUES (1,'report','failed',1,'simulada',now()) RETURNING id")[0][0]
    corpo = requests.post(f"{API}/jobs/{falhado}/retry", headers={"X-Auth": ACME}).json()
    assert corpo["id"] == falhado and corpo["status"] == "queued"
    assert corpo["attempts"] == 1 and corpo["max_attempts"] == 3


def test_retry_respeita_teto_de_tentativas():
    job_id = sql(
        "INSERT INTO jobs (company_id,kind,status,attempts,max_attempts)"
        " VALUES (1,'report','failed',3,3) RETURNING id"
    )[0][0]
    r = requests.post(f"{API}/jobs/{job_id}/retry", headers={"X-Auth": ACME})
    assert r.status_code == 409
    assert "tentativas" in r.json()["detail"]


# --------------------------------------------------------------------------
# Decisões de política — as três que exigiam julgamento, não só correção
# --------------------------------------------------------------------------

def test_remover_tenant_com_historico_e_bloqueado():
    """DAT-08: RESTRICT. Offboarding é desativação, não DELETE — e se alguém
    tentar, o banco precisa recusar antes que o histórico de faturamento suma."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sql("DELETE FROM companies WHERE id=1", fetch=False)


def test_remover_usuario_preserva_jobs():
    """DAT-08: SET NULL. Uma pessoa sai da empresa; o trabalho dela permanece.
    CASCADE apagaria histórico por causa de um desligamento."""
    antes = sql("SELECT count(*) FROM jobs WHERE company_id=1")[0][0]
    sql("INSERT INTO users (id, company_id, email, role) VALUES (999,1,'temp@acme.test','user')"
        " ON CONFLICT (id) DO NOTHING", fetch=False)
    sql("INSERT INTO jobs (company_id, created_by, kind, status) VALUES (1,999,'report','done')",
        fetch=False)
    sql("DELETE FROM users WHERE id=999", fetch=False)
    depois = sql("SELECT count(*) FROM jobs WHERE company_id=1")[0][0]
    assert depois == antes + 1, "job foi removido junto com o usuário"


def test_remover_job_leva_resultado_e_trilha():
    """DAT-08: CASCADE. Resultado e eventos não têm existência sem o job."""
    job_id = sql("INSERT INTO jobs (company_id,kind,status) VALUES (1,'report','done') RETURNING id")[0][0]
    sql("INSERT INTO job_results (job_id,payload) VALUES (%s,'x')", (job_id,), fetch=False)
    sql("INSERT INTO job_events (job_id,company_id,event) VALUES (%s,1,'teste')", (job_id,), fetch=False)
    sql("DELETE FROM jobs WHERE id=%s", (job_id,), fetch=False)
    assert sql("SELECT count(*) FROM job_results WHERE job_id=%s", (job_id,))[0][0] == 0
    assert sql("SELECT count(*) FROM job_events WHERE job_id=%s", (job_id,))[0][0] == 0


def test_resultado_expurgado_e_distinguivel_de_inexistente():
    """SEC-11: a decisão foi retenção em vez de cifra de aplicação. Expurgar a
    linha inteira apagaria a diferença entre 'nunca produziu' e 'produziu e foi
    descartado', que importa no suporte."""
    job_id = sql("INSERT INTO jobs (company_id,kind,status) VALUES (1,'report','done') RETURNING id")[0][0]
    sql("INSERT INTO job_results (job_id,payload,purged_at) VALUES (%s,'',now())",
        (job_id,), fetch=False)
    r = requests.get(f"{API}/jobs/{job_id}/result", headers={"X-Auth": ACME})
    assert r.status_code == 410, "expurgado deve ser 410, não 404/409"
    assert "retenção" in r.json()["detail"]


def test_metricas_exigem_token():
    """OBS-11: profundidade de fila e quota por empresa são dados de negócio.
    Expor sem autenticação reintroduziria pela porta dos fundos o vazamento
    entre tenants que acabou de ser fechado nas outras rotas."""
    r = requests.get(f"{API}/metrics")
    assert r.status_code in (401, 404), "métricas acessíveis sem token"


def test_status_invalido_e_rejeitado_pelo_banco():
    """Cria o job em vez de assumir que id=1 existe.

    A versão anterior fazia UPDATE ... WHERE id=1 e passava por acidente: com o
    seed intacto o job existia, mas num banco onde os ids fossem outros o UPDATE
    afetaria zero linhas, nenhuma constraint seria exercitada e o teste passaria
    sem testar nada — falso positivo silencioso. Descoberto ao recriar o dataset
    para validar a UI."""
    job_id = sql("INSERT INTO jobs (company_id,kind,status) VALUES (1,'report','queued') RETURNING id")[0][0]
    with pytest.raises(psycopg.errors.CheckViolation):
        sql("UPDATE jobs SET status='donee' WHERE id=%s", (job_id,), fetch=False)
    sql("UPDATE jobs SET status='done' WHERE id=%s", (job_id,), fetch=False)


def test_kind_invalido_e_rejeitado_pela_api(folga_de_quota):
    r = requests.post(f"{API}/jobs", headers={"X-Auth": ACME}, json={"kind": "xyz"})
    assert r.status_code == 422


# --------------------------------------------------------------------------
# Rastreabilidade
# --------------------------------------------------------------------------

def test_request_id_e_devolvido_e_persistido(folga_de_quota):
    rid = uuid.uuid4().hex
    r = requests.post(
        f"{API}/jobs",
        headers={"X-Auth": ACME, "X-Request-Id": rid},
        json={"kind": "report"},
    )
    assert r.status_code == 200
    assert r.headers.get("X-Request-Id") == rid, "cadeia de correlação quebrada"

    persistido = sql("SELECT request_id FROM jobs WHERE id=%s", (r.json()["id"],))[0][0]
    assert persistido == rid


def test_health_verifica_o_banco():
    r = requests.get(f"{API}/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
