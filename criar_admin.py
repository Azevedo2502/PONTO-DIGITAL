from getpass import getpass
from db import criar_tabelas, criar_admin

criar_tabelas()

usuario = input("Usuário do administrador: ").strip()
senha = getpass("Senha (não aparece enquanto digita): ")
repeticao = getpass("Repita a senha: ")

if len(senha) < 8:
    print("A senha precisa ter pelo menos 8 caracteres.")
elif senha != repeticao:
    print("As senhas não conferem.")
else:
    criar_admin(usuario, senha)
    print("Administrador criado com sucesso.")