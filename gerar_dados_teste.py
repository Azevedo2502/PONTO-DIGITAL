from datetime import date, timedelta

from db import (criar_tabelas, conectar, buscar_funcionario_por_codigo,
                montar_calendario, jornada_do_dia,
                TIPOS_COM_ALMOCO, TIPOS_SEM_ALMOCO)

CENARIOS = [
    ["07:55", "12:00", "13:00", "17:00"],   # normal (chegou 5 min cedo)
    ["08:10", "12:00", "13:00", "17:20"],   # saldo +20
    ["08:15", "12:00", "13:10", "17:00"],   # saldo -25
    ["07:50", "12:00", "13:00", "17:40"],   # saldo +40
    None,                                   # falta
    ["08:00", "12:00", "12:50", "17:30"],   # saldo +30, alerta de almoço curto
    ["08:00", "12:00", "13:00"],            # sem ponto de saída
]

criar_tabelas()
hoje = date.today()
primeiro = hoje.replace(day=1)
ana = buscar_funcionario_por_codigo("1001")

if ana is None:
    print("Funcionária de teste (código 1001) não encontrada.")
    raise SystemExit

cal = montar_calendario()

with conectar() as conn:
    # ATENÇÃO: apaga as batidas da Ana. Use só em ambiente de teste.
    conn.execute("DELETE FROM registros WHERE funcionario_id = ?", (ana["id"],))
    conn.execute("UPDATE funcionarios SET criado_em = ? WHERE id = ?",
                 (f"{primeiro.isoformat()} 07:00:00", ana["id"]))

    dia = primeiro
    i = 0
    while dia < hoje:
        jornada = jornada_do_dia(dia, cal)
        if jornada is not None:
            if jornada["almoco_min"] > 0:
                horarios = CENARIOS[i % len(CENARIOS)]
                i += 1
                tipos = TIPOS_COM_ALMOCO
            else:
                horarios = ["08:00", "14:30"]
                tipos = TIPOS_SEM_ALMOCO
            for tipo, hora in zip(tipos, horarios or []):
                conn.execute(
                    "INSERT INTO registros (funcionario_id, tipo, data_hora) VALUES (?, ?, ?)",
                    (ana["id"], tipo, f"{dia.isoformat()} {hora}:00"),
                )
        dia += timedelta(days=1)

print("Histórico de teste criado para Ana Souza (de dia 1 até ontem).")