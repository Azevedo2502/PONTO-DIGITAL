from getpass import getpass

from db import conectar, criar_tabelas
from contas import preparar_banco, registrar_auditoria
from werkzeug.security import generate_password_hash

criar_tabelas()
preparar_banco()

usuario = input("Usuário do administrador: ").strip()
senha = getpass("Nova senha (mínimo 12 caracteres): ")
repeticao = getpass("Repita a nova senha: ")

if len(senha) < 12:
    print("A senha precisa ter pelo menos 12 caracteres.")
elif senha != repeticao:
    print("As senhas não conferem.")
else:
    with conectar() as conn:
        resultado = conn.execute(
            "UPDATE admins SET senha_hash = ?, precisa_trocar_senha = 0, "
            "senha_alterada_em = datetime('now', 'localtime') WHERE usuario = ?",
            (generate_password_hash(senha), usuario),
        )
    if resultado.rowcount == 0:
        print("Usuário não encontrado.")
    else:
        registrar_auditoria(None, "redefiniu a senha pelo script", usuario)
        print("Senha redefinida.")