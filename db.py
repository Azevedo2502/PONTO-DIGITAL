import sqlite3
import hmac
import hashlib
import os
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash

_caminho_banco = os.environ.get("PONTO_DB_PATH")
if os.environ.get("APP_ENV", "development").lower() == "production":
    if not _caminho_banco:
        raise RuntimeError("Defina PONTO_DB_PATH para o volume persistente de produção.")
    if not Path(_caminho_banco).is_absolute():
        raise RuntimeError("PONTO_DB_PATH precisa ser um caminho absoluto em produção.")
DB_PATH = Path(_caminho_banco or Path(__file__).parent / "ponto.db")

_chave_configurada = os.environ.get("CODIGO_SECRET_KEY")
if os.environ.get("APP_ENV", "development").lower() == "production" and not _chave_configurada:
    raise RuntimeError("Defina CODIGO_SECRET_KEY no ambiente de produção.")
# Mantém compatibilidade da base local antiga; produção exige a chave do ambiente.
CHAVE_SECRETA = (_chave_configurada or "troque-esta-chave-depois").encode()
CHAVE_SECRETA_ANTERIOR = os.environ.get("LEGACY_CODIGO_SECRET_KEY")
if CHAVE_SECRETA_ANTERIOR:
    CHAVE_SECRETA_ANTERIOR = CHAVE_SECRETA_ANTERIOR.encode()

TIPOS_COM_ALMOCO = ["entrada", "saida_almoco", "volta_almoco", "saida"]
TIPOS_SEM_ALMOCO = ["entrada", "saida"]
MAX_TENTATIVAS_LOGIN = 5
JANELA_TENTATIVAS_LOGIN = 15 * 60

ROTULOS = {
    "entrada": "Entrada",
    "saida_almoco": "Saída para o almoço",
    "volta_almoco": "Volta do almoço",
    "saida": "Saída",
}

# Evita batida duplicada por toque acidental (em segundos)
INTERVALO_MINIMO_SEGUNDOS = 60

CONFIG_PADRAO = {
    "tolerancia_entrada_min": "10",
    "tolerancia_almoco_min": "5",
    "tolerancia_saida_min": "5",
    "hora_limite_saida": "22:00",
}


@contextmanager
def conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            yield conn
    finally:
        conn.close()


def hash_codigo(codigo):
    """O mesmo código sempre gera o mesmo hash, então dá para procurar
    o funcionário sem guardar o código real."""
    return hmac.new(CHAVE_SECRETA, codigo.encode(), hashlib.sha256).hexdigest()


def criar_tabelas():
    with conectar() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS funcionarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                codigo_hash TEXT NOT NULL UNIQUE,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS registros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id),
                tipo TEXT NOT NULL CHECK (tipo IN
                    ('entrada', 'saida_almoco', 'volta_almoco', 'saida')),
                data_hora TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jornada_dias (
                dia_semana INTEGER PRIMARY KEY CHECK (dia_semana BETWEEN 0 AND 6),
                trabalha INTEGER NOT NULL DEFAULT 0,
                entrada TEXT,
                saida TEXT,
                almoco_min INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feriados_folgas (
                data TEXT PRIMARY KEY,
                descricao TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tentativas_login (
                chave TEXT PRIMARY KEY,
                quantidade INTEGER NOT NULL,
                primeira_tentativa REAL NOT NULL,
                bloqueado_ate REAL
            );
        """)
        _semear_padroes(conn)


def _semear_padroes(conn):
    """Cria a jornada e as configurações padrão na primeira execução.
    INSERT OR IGNORE não sobrescreve o que o admin já alterou."""
    jornada = [
        (0, 1, "08:00", "17:00", 60),
        (1, 1, "08:00", "17:00", 60),
        (2, 1, "08:00", "17:00", 60),
        (3, 1, "08:00", "17:00", 60),
        (4, 1, "08:00", "17:00", 60),
        (5, 1, "08:00", "14:00", 0),
        (6, 0, None, None, 0),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO jornada_dias "
        "(dia_semana, trabalha, entrada, saida, almoco_min) VALUES (?, ?, ?, ?, ?)",
        jornada,
    )
    for chave, valor in CONFIG_PADRAO.items():
        conn.execute(
            "INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES (?, ?)",
            (chave, valor),
        )


# ---------- Funcionários ----------

def cadastrar_funcionario(nome, codigo):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO funcionarios (nome, codigo_hash) VALUES (?, ?)",
            (nome, hash_codigo(codigo)),
        )


def buscar_funcionario_por_codigo(codigo):
    hash_atual = hash_codigo(codigo)
    hash_anterior = (
        hmac.new(CHAVE_SECRETA_ANTERIOR, codigo.encode(), hashlib.sha256).hexdigest()
        if CHAVE_SECRETA_ANTERIOR else None
    )
    with conectar() as conn:
        funcionario = conn.execute(
            "SELECT * FROM funcionarios WHERE codigo_hash IN (?, ?) AND ativo = 1",
            (hash_atual, hash_anterior or hash_atual),
        ).fetchone()
        if funcionario and funcionario["codigo_hash"] != hash_atual:
            conn.execute(
                "UPDATE funcionarios SET codigo_hash = ? WHERE id = ?",
                (hash_atual, funcionario["id"]),
            )
        return funcionario


def buscar_funcionario(funcionario_id):
    with conectar() as conn:
        return conn.execute(
            "SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,)
        ).fetchone()


def listar_funcionarios():
    with conectar() as conn:
        return conn.execute(
            "SELECT id, nome, ativo, criado_em FROM funcionarios "
            "ORDER BY ativo DESC, nome"
        ).fetchall()


def alternar_ativo(funcionario_id):
    # 1 - ativo inverte o valor: 1 vira 0 e 0 vira 1
    with conectar() as conn:
        conn.execute(
            "UPDATE funcionarios SET ativo = 1 - ativo WHERE id = ?",
            (funcionario_id,),
        )


def arquivar_funcionario(funcionario_id):
    with conectar() as conn:
        conn.execute(
            "UPDATE funcionarios SET ativo = 0 WHERE id = ?",
            (funcionario_id,),
        )


# ---------- Administradores ----------

def criar_admin(usuario, senha):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO admins (usuario, senha_hash) VALUES (?, ?)",
            (usuario, generate_password_hash(senha)),
        )


def verificar_admin(usuario, senha):
    with conectar() as conn:
        admin = conn.execute(
            "SELECT * FROM admins WHERE usuario = ?", (usuario,)
        ).fetchone()
    if admin and check_password_hash(admin["senha_hash"], senha):
        return admin
    return None


# ---------- Jornada, configurações e feriados ----------

def obter_jornada():
    with conectar() as conn:
        return conn.execute(
            "SELECT * FROM jornada_dias ORDER BY dia_semana"
        ).fetchall()


def salvar_jornada(dias):
    """dias = lista de (dia_semana, trabalha, entrada, saida, almoco_min).
    Tudo é gravado numa única transação: ou salva os 7 dias, ou nenhum."""
    with conectar() as conn:
        conn.executemany(
            "UPDATE jornada_dias SET trabalha = ?, entrada = ?, saida = ?, "
            "almoco_min = ? WHERE dia_semana = ?",
            [(t, e, s, a, d) for d, t, e, s, a in dias],
        )


def obter_configuracoes():
    with conectar() as conn:
        linhas = conn.execute("SELECT chave, valor FROM configuracoes").fetchall()
    valores = dict(CONFIG_PADRAO)
    valores.update({linha["chave"]: linha["valor"] for linha in linhas})
    return {
        "tol_entrada": int(valores["tolerancia_entrada_min"]),
        "tol_almoco": int(valores["tolerancia_almoco_min"]),
        "tol_saida": int(valores["tolerancia_saida_min"]),
        "hora_limite": valores["hora_limite_saida"],
    }


def salvar_configuracoes(tol_entrada, tol_almoco, tol_saida, hora_limite):
    novos = [
        ("tolerancia_entrada_min", str(tol_entrada)),
        ("tolerancia_almoco_min", str(tol_almoco)),
        ("tolerancia_saida_min", str(tol_saida)),
        ("hora_limite_saida", hora_limite),
    ]
    with conectar() as conn:
        conn.executemany(
            "INSERT INTO configuracoes (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            novos,
        )


def listar_feriados():
    with conectar() as conn:
        return conn.execute(
            "SELECT data, descricao FROM feriados_folgas ORDER BY data"
        ).fetchall()


def adicionar_feriado(data, descricao):
    with conectar() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO feriados_folgas (data, descricao) VALUES (?, ?)",
            (data, descricao),
        )


def remover_feriado(data):
    with conectar() as conn:
        conn.execute("DELETE FROM feriados_folgas WHERE data = ?", (data,))


def montar_calendario():
    """Carrega jornada e feriados UMA vez, para usar em vários dias seguidos."""
    return {
        "jornada": obter_jornada(),
        "feriados": {f["data"]: f["descricao"] for f in listar_feriados()},
    }


def jornada_do_dia(data, calendario):
    """data: objeto date. Devolve a jornada prevista (dict) ou None se o dia
    não é de trabalho (folga semanal ou feriado)."""
    if data.isoformat() in calendario["feriados"]:
        return None
    linha = calendario["jornada"][data.weekday()]
    if not linha["trabalha"]:
        return None
    return {
        "entrada": linha["entrada"],
        "saida": linha["saida"],
        "almoco_min": linha["almoco_min"],
    }


# ---------- Batidas ----------

def registrar_batida(funcionario_id):
    agora = datetime.now()
    jornada = jornada_do_dia(agora.date(), montar_calendario())
    tem_almoco = jornada is not None and jornada["almoco_min"] > 0
    tipos = TIPOS_COM_ALMOCO if tem_almoco else TIPOS_SEM_ALMOCO

    with conectar() as conn:
        batidas = conn.execute(
            "SELECT data_hora FROM registros "
            "WHERE funcionario_id = ? AND date(data_hora) = ? ORDER BY id",
            (funcionario_id, agora.strftime("%Y-%m-%d")),
        ).fetchall()

        if len(batidas) >= len(tipos):
            return "completo", None, None

        if batidas:
            ultima = datetime.strptime(batidas[-1]["data_hora"], "%Y-%m-%d %H:%M:%S")
            if (agora - ultima).total_seconds() < INTERVALO_MINIMO_SEGUNDOS:
                return "repetida", None, None

        tipo = tipos[len(batidas)]
        conn.execute(
            "INSERT INTO registros (funcionario_id, tipo, data_hora) VALUES (?, ?, ?)",
            (funcionario_id, tipo, agora.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return "ok", tipo, agora


def batidas_entre(funcionario_id, inicio, fim_exclusivo):
    """Batidas de [inicio, fim_exclusivo) -> {'2026-11-09': ['08:00', '12:00', ...]}"""
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT data_hora FROM registros WHERE funcionario_id = ? "
            "AND data_hora >= ? AND data_hora < ? ORDER BY data_hora, id",
            (funcionario_id, inicio.isoformat(), fim_exclusivo.isoformat()),
        ).fetchall()
    por_dia = {}
    for linha in linhas:
        dia, hora = linha["data_hora"][:10], linha["data_hora"][11:16]
        por_dia.setdefault(dia, []).append(hora)
    return por_dia


def tipos_batidas_entre(funcionario_id, inicio, fim_exclusivo):
    """Tipos das batidas de [inicio, fim_exclusivo], agrupados por dia."""
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT tipo, data_hora FROM registros WHERE funcionario_id = ? "
            "AND data_hora >= ? AND data_hora < ? ORDER BY data_hora, id",
            (funcionario_id, inicio.isoformat(), fim_exclusivo.isoformat()),
        ).fetchall()
    por_dia = {}
    for linha in linhas:
        por_dia.setdefault(linha["data_hora"][:10], []).append(linha["tipo"])
    return por_dia


def adicionar_batida_manual(funcionario_id, tipo, data_hora, tipos_esperados):
    """Insere uma batida antiga sem alterar os registros já existentes."""
    try:
        datetime.strptime(data_hora, "%Y-%m-%d %H:%M:%S")
    except ValueError as erro:
        raise ValueError("Data ou horário inválido.") from erro
    if tipo not in tipos_esperados:
        raise ValueError("Tipo de batida inválido para esse dia.")

    dia = data_hora[:10]
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existentes = conn.execute(
            "SELECT tipo, data_hora FROM registros "
            "WHERE funcionario_id = ? AND date(data_hora) = ? "
            "ORDER BY data_hora, id",
            (funcionario_id, dia),
        ).fetchall()
        if any(registro["tipo"] == tipo for registro in existentes):
            raise ValueError("Esse tipo de batida já foi registrado nesse dia.")

        ordem = {tipo_esperado: indice for indice, tipo_esperado in enumerate(tipos_esperados)}
        batidas = [(registro["data_hora"], registro["tipo"]) for registro in existentes]
        batidas.append((data_hora, tipo))
        batidas.sort(key=lambda batida: batida[0])
        indices = [ordem.get(tipo_batida, -1) for _, tipo_batida in batidas]
        if -1 in indices or any(atual <= anterior for anterior, atual in zip(indices, indices[1:])):
            raise ValueError("O horário precisa manter a ordem das batidas do dia.")
        if any(horario == data_hora for horario, _ in batidas[:-1]):
            raise ValueError("Já existe uma batida nesse horário.")

        cursor = conn.execute(
            "INSERT INTO registros (funcionario_id, tipo, data_hora) VALUES (?, ?, ?)",
            (funcionario_id, tipo, data_hora),
        )
        return cursor.lastrowid


def segundos_ate_desbloqueio_login(chave, agora=None):
    agora = time.time() if agora is None else agora
    with conectar() as conn:
        linha = conn.execute(
            "SELECT bloqueado_ate, primeira_tentativa FROM tentativas_login WHERE chave = ?",
            (chave,),
        ).fetchone()
        if linha is None:
            return 0
        if linha["bloqueado_ate"] and linha["bloqueado_ate"] > agora:
            return int(linha["bloqueado_ate"] - agora + 0.999)
        if agora - linha["primeira_tentativa"] >= JANELA_TENTATIVAS_LOGIN:
            conn.execute("DELETE FROM tentativas_login WHERE chave = ?", (chave,))
        return 0


def registrar_falha_login(chave, agora=None):
    agora = time.time() if agora is None else agora
    bloqueado_ate = agora + JANELA_TENTATIVAS_LOGIN
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM tentativas_login WHERE primeira_tentativa < ?",
            (agora - 24 * 60 * 60,),
        )
        linha = conn.execute(
            "SELECT quantidade, primeira_tentativa, bloqueado_ate "
            "FROM tentativas_login WHERE chave = ?",
            (chave,),
        ).fetchone()
        if linha is None or agora - linha["primeira_tentativa"] >= JANELA_TENTATIVAS_LOGIN:
            quantidade = 1
            primeira = agora
            bloqueio = None
        elif linha["bloqueado_ate"] and linha["bloqueado_ate"] > agora:
            return int(linha["bloqueado_ate"] - agora + 0.999)
        else:
            quantidade = linha["quantidade"] + 1
            primeira = linha["primeira_tentativa"]
            bloqueio = bloqueado_ate if quantidade >= MAX_TENTATIVAS_LOGIN else None

        conn.execute(
            "INSERT INTO tentativas_login (chave, quantidade, primeira_tentativa, bloqueado_ate) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(chave) DO UPDATE SET "
            "quantidade = excluded.quantidade, primeira_tentativa = excluded.primeira_tentativa, "
            "bloqueado_ate = excluded.bloqueado_ate",
            (chave, quantidade, primeira, bloqueio),
        )
        return int(bloqueio - agora + 0.999) if bloqueio else 0


def limpar_tentativas_login(chave):
    with conectar() as conn:
        conn.execute("DELETE FROM tentativas_login WHERE chave = ?", (chave,))


def criar_backup():
    """Devolve uma cópia consistente do banco enquanto ele está em uso."""
    with closing(sqlite3.connect(DB_PATH)) as origem:
        with closing(sqlite3.connect(":memory:")) as copia:
            origem.backup(copia)
            return copia.serialize()