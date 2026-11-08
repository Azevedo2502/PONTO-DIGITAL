from datetime import date, datetime

HORA_LIMITE_SAIDA = "22:00"


def para_minutos(hhmm):
    """'08:15' -> 495"""
    horas, minutos = hhmm.split(":")
    return int(horas) * 60 + int(minutos)


def formatar(minutos, com_sinal=False):
    """495 -> '08:15'. Com sinal: 25 -> '+00:25' e -25 -> '-00:25'."""
    if minutos < 0:
        sinal = "-"
    elif com_sinal and minutos > 0:
        sinal = "+"
    else:
        sinal = ""
    m = abs(minutos)
    return f"{sinal}{m // 60:02d}:{m % 60:02d}"


def momento_do_dia(data, agora, hora_limite=HORA_LIMITE_SAIDA):
    """Classifica um dia (date) em relação a 'agora' (datetime):
    'futuro'  -> depois de hoje
    'hoje'    -> hoje, antes do horário limite (ainda pode bater ponto)
    'passado' -> antes de hoje, ou hoje depois do horário limite"""
    hoje = agora.date()
    if data > hoje:
        return "futuro"
    if data < hoje:
        return "passado"
    minutos_agora = agora.hour * 60 + agora.minute
    return "hoje" if minutos_agora < para_minutos(hora_limite) else "passado"


def _resultado(situacao, **valores):
    base = {
        "situacao": situacao,
        "trabalhado": 0,
        "previsto": 0,
        "saldo": 0,
        "atraso": 0,
        "saida_antecipada": 0,
        "almoco_excedente": 0,
        "extra": 0,
        "alertas": [],
    }
    base.update(valores)
    return base


def calcular_dia(batidas, jornada, tolerancias, momento):
    """
    batidas:     lista de horários 'HH:MM' do dia, em ordem
    jornada:     dict com 'entrada', 'saida' e 'almoco_min' (ou None se o dia
                 não é de trabalho: domingo, feriado, folga)
    tolerancias: dict com minutos tolerados: 'entrada', 'almoco' e 'saida'
    momento:     'passado', 'hoje' ou 'futuro' (veja momento_do_dia)
    """
    if momento == "futuro":
        return _resultado("futuro")

    tol_ent = tolerancias["entrada"]
    tol_alm = tolerancias["almoco"]
    tol_sai = tolerancias["saida"]

    trabalha = jornada is not None
    previsto = 0
    esperadas = 4

    if trabalha:
        prev_ent = para_minutos(jornada["entrada"])
        prev_sai = para_minutos(jornada["saida"])
        prev_alm = jornada["almoco_min"]
        previsto = (prev_sai - prev_ent) - prev_alm
        esperadas = 4 if prev_alm > 0 else 2
    elif len(batidas) in (2, 4):
        esperadas = len(batidas)

    # 1) Dia sem nenhuma batida
    if not batidas:
        if not trabalha:
            return _resultado("folga")
        if momento == "passado":
            return _resultado("falta", previsto=previsto, saldo=-previsto)
        return _resultado("aberto", previsto=previsto)

    # 2) Batidas incompletas ou a mais
    if len(batidas) < esperadas and momento == "hoje":
        return _resultado("aberto", previsto=previsto)
    if len(batidas) != esperadas:
        return _resultado(
            "pendente", previsto=previsto,
            alertas=["Sem ponto de saída ou batidas incompletas: precisa de ajuste."],
        )

    # 3) Dia completo: calcular
    ent = para_minutos(batidas[0])
    sai = para_minutos(batidas[-1])
    alertas = []
    atraso = antecipada = excedente = extra = 0
    desconto_almoco = 0

    if trabalha:
        # Normalização: dentro da tolerância, vale o horário previsto
        if abs(ent - prev_ent) <= tol_ent:
            ent = prev_ent
        if abs(sai - prev_sai) <= tol_sai:
            sai = prev_sai

        atraso = max(0, ent - prev_ent)
        antecipada = max(0, prev_sai - sai)
        extra = max(0, prev_ent - ent) + max(0, sai - prev_sai)

    if esperadas == 4:
        real = para_minutos(batidas[2]) - para_minutos(batidas[1])
        if trabalha:
            if real - prev_alm > tol_alm:
                desconto_almoco = real              # passou da tolerância: conta tudo
                excedente = real - prev_alm
            else:
                desconto_almoco = prev_alm          # dentro da tolerância: 1h exata
            if prev_alm - real > tol_alm:
                alertas.append(f"Intervalo menor que o previsto ({real} min).")
        else:
            desconto_almoco = real

    trabalhado = (sai - ent) - desconto_almoco

    if trabalha:
        saldo = trabalhado - previsto
    else:
        saldo = trabalhado      # em dia de folga, tudo é hora extra
        extra = trabalhado

    if saldo > 0:
        situacao = "positivo"
    elif saldo < 0:
        situacao = "negativo"
    else:
        situacao = "normal"

    return _resultado(
        situacao,
        trabalhado=trabalhado, previsto=previsto, saldo=saldo,
        atraso=atraso, saida_antecipada=antecipada,
        almoco_excedente=excedente, extra=extra, alertas=alertas,
    )