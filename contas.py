import os
import re
import secrets
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash

from db import conectar, hash_codigo

# Sem letras e números parecidos (0/O, 1/l/I) para a senha temporária ser fácil de ler
ALFABETO_SENHA = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def preparar_banco():
    """Cria a tabela de auditoria e acrescenta colunas novas na tabela de
    administradores, sem perder os dados que já existem."""
    with conectar() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS auditoria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quando TEXT NOT NULL,
                admin_id INTEGER,
                admin_usuario TEXT NOT NULL,
                acao TEXT NOT NULL,
                detalhe TEXT NOT NULL DEFAULT ''
            );
        """)
        colunas = [linha["name"] for linha in conn.execute("PRAGMA table_info(admins)")]
        if "precisa_trocar_senha" not in colunas:
            conn.execute(
                "ALTER TABLE admins ADD COLUMN precisa_trocar_senha INTEGER NOT NULL DEFAULT 0"
            )
        if "senha_alterada_em" not in colunas:
            conn.execute("ALTER TABLE admins ADD COLUMN senha_alterada_em TEXT")


def criar_admin_inicial_se_necessario():
    """Cria o primeiro administrador a partir do ambiente, se configurado."""
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        total = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
        if total:
            return False

        usuario = os.environ.get("ADMIN_INICIAL_USUARIO", "").strip()
        senha = os.environ.get("ADMIN_INICIAL_SENHA", "")
        if not usuario and not senha:
            return False
        if not usuario or not senha:
            raise RuntimeError(
                "Defina ADMIN_INICIAL_USUARIO e ADMIN_INICIAL_SENHA juntos."
            )
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,30}", usuario):
            raise RuntimeError(
                "ADMIN_INICIAL_USUARIO deve ter de 3 a 30 caracteres: "
                "letras, números, ponto, hífen ou sublinhado."
            )
        if len(senha) < 8:
            raise RuntimeError("ADMIN_INICIAL_SENHA deve ter pelo menos 8 caracteres.")

        conn.execute(
            "INSERT INTO admins (usuario, senha_hash, precisa_trocar_senha) "
            "VALUES (?, ?, 1)",
            (usuario, generate_password_hash(senha)),
        )
    return True


# ---------- Auditoria ----------

def registrar_auditoria(admin_id, acao, detalhe=""):
    """Registra quem fez o quê. NUNCA grave senhas ou códigos aqui."""
    with conectar() as conn:
        usuario = "script de emergência"
        if admin_id is not None:
            linha = conn.execute(
                "SELECT usuario FROM admins WHERE id = ?", (admin_id,)
            ).fetchone()
            usuario = linha["usuario"] if linha else "desconhecido"
        conn.execute(
            "INSERT INTO auditoria (quando, admin_id, admin_usuario, acao, detalhe) "
            "VALUES (?, ?, ?, ?, ?)",
            (_agora(), admin_id, usuario, acao, detalhe),
        )


def listar_auditoria(limite=30):
    with conectar() as conn:
        return conn.execute(
            "SELECT quando, admin_usuario, acao, detalhe FROM auditoria "
            "ORDER BY id DESC LIMIT ?",
            (limite,),
        ).fetchall()


# ---------- Administradores ----------

def gerar_senha_temporaria(tamanho=12):
    # secrets é feito para segurança; random NÃO deve ser usado para senhas
    return "".join(secrets.choice(ALFABETO_SENHA) for _ in range(tamanho))


def buscar_admin(admin_id):
    with conectar() as conn:
        return conn.execute(
            "SELECT id, usuario, precisa_trocar_senha, senha_alterada_em "
            "FROM admins WHERE id = ?",
            (admin_id,),
        ).fetchone()


def listar_admins():
    with conectar() as conn:
        return conn.execute(
            "SELECT id, usuario, precisa_trocar_senha, senha_alterada_em "
            "FROM admins ORDER BY usuario"
        ).fetchall()


def criar_admin_com_senha_temporaria(usuario, senha_temporaria):
    """Levanta sqlite3.IntegrityError se o usuário já existir."""
    with conectar() as conn:
        conn.execute(
            "INSERT INTO admins (usuario, senha_hash, precisa_trocar_senha) "
            "VALUES (?, ?, 1)",
            (usuario, generate_password_hash(senha_temporaria)),
        )


def redefinir_senha_de(admin_id):
    """Define uma senha temporária e obriga a troca. Devolve a senha temporária."""
    temporaria = gerar_senha_temporaria()
    with conectar() as conn:
        conn.execute(
            "UPDATE admins SET senha_hash = ?, precisa_trocar_senha = 1, "
            "senha_alterada_em = ? WHERE id = ?",
            (generate_password_hash(temporaria), _agora(), admin_id),
        )
    return temporaria


def trocar_propria_senha(admin_id, senha_atual, senha_nova):
    with conectar() as conn:
        admin = conn.execute(
            "SELECT senha_hash FROM admins WHERE id = ?", (admin_id,)
        ).fetchone()
        if admin is None or not check_password_hash(admin["senha_hash"], senha_atual):
            return False
        conn.execute(
            "UPDATE admins SET senha_hash = ?, precisa_trocar_senha = 0, "
            "senha_alterada_em = ? WHERE id = ?",
            (generate_password_hash(senha_nova), _agora(), admin_id),
        )
    return True


def remover_admin(admin_id, executado_por):
    """Devolve 'ok', 'proprio' ou 'ultimo'."""
    if admin_id == executado_por:
        return "proprio"
    with conectar() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
        if total <= 1:
            return "ultimo"
        conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
    return "ok"


# ---------- Código do funcionário ----------

def redefinir_codigo(funcionario_id, novo_codigo):
    """Levanta sqlite3.IntegrityError se o código já pertencer a outra pessoa."""
    with conectar() as conn:
        conn.execute(
            "UPDATE funcionarios SET codigo_hash = ? WHERE id = ?",
            (hash_codigo(novo_codigo), funcionario_id),
        )