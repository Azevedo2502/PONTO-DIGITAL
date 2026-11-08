import sqlite3
from db import criar_tabelas, cadastrar_funcionario, buscar_funcionario_por_codigo

criar_tabelas()

# Dados de TESTE (depois o cadastro real será pelo painel do admin)
funcionarios_teste = [
    ("Ana Souza", "1001"), ("Bruno Lima", "1002"), ("Carla Dias", "1003"),
    ("Diego Alves", "1004"), ("Elisa Rocha", "1005"), ("Fábio Costa", "1006"),
    ("Gabriela Nunes", "1007"), ("Hugo Pinto", "1008"), ("Isabela Melo", "1009"),
    ("João Ramos", "1010"),
]

for nome, codigo in funcionarios_teste:
    try:
        cadastrar_funcionario(nome, codigo)
        print(f"Cadastrado: {nome}")
    except sqlite3.IntegrityError:
        print(f"Já existia: {nome}")

# Testar a busca pelo código
f = buscar_funcionario_por_codigo("1003")
print("Busca pelo código 1003 ->", f["nome"] if f else "não encontrado")