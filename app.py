import calendar
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
from urllib.parse import quote_from_bytes, urlsplit

from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, flash, abort, send_file)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from calculo import calcular_dia, formatar, momento_do_dia
from contas import (preparar_banco, criar_admin_inicial_se_necessario,
                    registrar_auditoria, listar_auditoria,
                    listar_admins, buscar_admin, gerar_senha_temporaria,
                    criar_admin_com_senha_temporaria, redefinir_senha_de,
                    trocar_propria_senha, remover_admin, redefinir_codigo)
from db import (criar_tabelas, buscar_funcionario_por_codigo, buscar_funcionario,
                registrar_batida, ROTULOS, verificar_admin, listar_funcionarios,
                cadastrar_funcionario, alternar_ativo, arquivar_funcionario,
                obter_jornada, salvar_jornada,
                obter_configuracoes, salvar_configuracoes,
                listar_feriados, adicionar_feriado, remover_feriado,
                montar_calendario, jornada_do_dia, batidas_entre,
                tipos_batidas_entre, adicionar_batida_manual,
                segundos_ate_desbloqueio_login, registrar_falha_login,
                limpar_tentativas_login, criar_backup,
                TIPOS_COM_ALMOCO, TIPOS_SEM_ALMOCO)
from relatorios import gerar_pdf_folha, gerar_excel_folha

app = Flask(__name__)
APP_ENV = os.environ.get("APP_ENV", "development").lower()
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    if APP_ENV == "production":
        raise RuntimeError("Defina SECRET_KEY no ambiente de produção.")
    app.secret_key = secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = APP_ENV == "production"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")
if APP_ENV == "production":
    origem = urlsplit(PUBLIC_ORIGIN)
    if (
        origem.scheme != "https"
        or not origem.netloc
        or origem.username
        or origem.password
        or origem.path not in ("", "/")
        or origem.query
        or origem.fragment
    ):
        raise RuntimeError("Defina PUBLIC_ORIGIN como a origem HTTPS pública do serviço.")
    app.config["TRUSTED_HOSTS"] = [origem.netloc]
try:
    quantidade_proxies = int(os.environ.get(
        "TRUST_PROXY_COUNT", "1" if APP_ENV == "production" else "0"
    ))
except ValueError as erro:
    raise RuntimeError("TRUST_PROXY_COUNT deve ser um número inteiro.") from erro
if quantidade_proxies < 0:
    raise RuntimeError("TRUST_PROXY_COUNT não pode ser negativo.")
if quantidade_proxies:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=quantidade_proxies,
        x_proto=quantidade_proxies,
        x_host=quantidade_proxies,
    )
criar_tabelas()
preparar_banco()
criar_admin_inicial_se_necessario()

NOMES_DIAS = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
              "Sexta-feira", "Sábado", "Domingo"]
DIAS_CURTOS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]
ROTULOS_SITUACAO = {
    "normal": "Normal",
    "positivo": "Positivo",
    "negativo": "Negativo",
    "falta": "FALTA",
    "pendente": "Sem ponto de saída",
    "aberto": "Em andamento",
    "folga": "Folga",
}
SITUACOES_CALCULADAS = ("normal", "positivo", "negativo")


@app.after_request
def proteger_admin(resposta):
    """Aplica cabeçalhos de segurança e evita index/cache do painel."""
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resposta.headers["X-Frame-Options"] = "DENY"
    if request.is_secure:
        resposta.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/admin"):
        resposta.headers["X-Robots-Tag"] = "noindex, nofollow"
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.before_request
def exigir_https_em_producao():
    if APP_ENV == "production" and not request.is_secure and request.endpoint != "health":
        destino = PUBLIC_ORIGIN + request.path
        if request.query_string:
            destino += "?" + quote_from_bytes(request.query_string, safe="=&%+")
        return redirect(destino, code=308)
    return None


@app.route("/health")
def health():
    return jsonify(status="ok")


def login_obrigatorio(funcao):
    @wraps(funcao)
    def protegida(*args, **kwargs):
        admin_id = session.get("admin_id")
        admin = buscar_admin(admin_id) if admin_id else None
        if admin is None:
            # não logado, ou o administrador foi removido
            session.clear()
            return redirect(url_for("admin_login"))
        if admin["precisa_trocar_senha"] and request.endpoint != "admin_trocar_senha":
            return redirect(url_for("admin_trocar_senha"))
        return funcao(*args, **kwargs)
    return protegida


def hora_valida(texto):
    try:
        datetime.strptime(texto, "%H:%M")
        return True
    except ValueError:
        return False


# ---------- Cálculo da folha ----------

def calcular_periodo(funcionario_id, inicio, fim, cfg, cal, agora):
    """Calcula cada dia de 'inicio' até 'fim' (inclusive). Dias futuros ficam de fora."""
    if inicio > fim:
        return []
    batidas = batidas_entre(funcionario_id, inicio, fim + timedelta(days=1))
    tipos_batidas = tipos_batidas_entre(funcionario_id, inicio, fim + timedelta(days=1))
    tolerancias = {
        "entrada": cfg["tol_entrada"],
        "almoco": cfg["tol_almoco"],
        "saida": cfg["tol_saida"],
    }
    linhas = []
    dia = inicio
    while dia <= fim:
        momento = momento_do_dia(dia, agora, cfg["hora_limite"])
        if momento != "futuro":
            jornada = jornada_do_dia(dia, cal)
            horarios = batidas.get(dia.isoformat(), [])
            tipos_esperados = TIPOS_COM_ALMOCO if jornada and jornada["almoco_min"] > 0 \
                else TIPOS_SEM_ALMOCO
            linhas.append({
                "data": dia,
                "batidas": horarios,
                "tipos_esperados": tipos_esperados,
                "esperadas": len(tipos_esperados),
                "tipos_existentes": tipos_batidas.get(dia.isoformat(), []),
                "feriado": cal["feriados"].get(dia.isoformat()),
                "r": calcular_dia(horarios, jornada, tolerancias, momento),
            })
        dia += timedelta(days=1)
    return linhas


def somar_saldo(linhas):
    """Faltas e dias pendentes ficam de fora do saldo do banco de horas."""
    return sum(l["r"]["saldo"] for l in linhas
               if l["r"]["situacao"] in SITUACOES_CALCULADAS)


def colunas_batidas(linha):
    b = linha["batidas"]
    if linha["esperadas"] == 2:
        return [b[0] if len(b) > 0 else "", "", "", b[1] if len(b) > 1 else ""]
    return (b + [""] * 4)[:4]


def linha_para_tela(linha):
    r = linha["r"]
    situacao = r["situacao"]
    texto = ROTULOS_SITUACAO[situacao]
    if linha["feriado"]:
        texto = f"{texto} ({linha['feriado']})"
    calculado = situacao in SITUACOES_CALCULADAS
    return {
        "data": linha["data"].strftime("%d/%m"),
        "data_iso": linha["data"].isoformat(),
        "dia": DIAS_CURTOS[linha["data"].weekday()],
        "colunas": colunas_batidas(linha),
        "trabalhado": formatar(r["trabalhado"]) if calculado else "—",
        "saldo": formatar(r["saldo"], True) if calculado else "—",
        "situacao": situacao,
        "texto": texto,
        "alertas": r["alertas"],
        "tipos_faltantes": [
            {"tipo": tipo, "rotulo": ROTULOS[tipo]}
            for tipo in linha["tipos_esperados"]
            if tipo not in linha["tipos_existentes"]
        ],
        "pode_ajustar": linha["data"] <= datetime.now().date(),
    }


# ---------- Tela do iPad (pública) ----------

@app.route("/")
def teclado():
    return render_template("teclado.html")


@app.route("/bater", methods=["POST"])
def bater():
    dados = request.get_json(silent=True) or {}
    codigo = str(dados.get("codigo", "")).strip()

    if not codigo.isdigit():
        return jsonify(ok=False, mensagem="Código inválido."), 400

    funcionario = buscar_funcionario_por_codigo(codigo)
    if funcionario is None:
        return jsonify(ok=False, mensagem="Código não encontrado."), 404

    status, tipo, agora = registrar_batida(funcionario["id"])
    primeiro_nome = funcionario["nome"].split()[0]

    if status == "completo":
        return jsonify(ok=False, mensagem=f"{primeiro_nome}, você já fez todas as batidas de hoje.")
    if status == "repetida":
        return jsonify(ok=False, mensagem=f"{primeiro_nome}, sua batida já foi registrada agora há pouco.")

    return jsonify(
        ok=True,
        mensagem=f"Olá, {primeiro_nome}! {ROTULOS[tipo]} registrada às {agora:%H:%M}.",
    )


# ---------- Login, senha e administradores ----------

def chave_tentativa_login(usuario):
    origem = request.remote_addr or "desconhecido"
    identificador = f"{origem}\0{usuario.casefold()}".encode()
    return hmac.new(app.secret_key.encode(), identificador, hashlib.sha256).hexdigest()


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        chave = chave_tentativa_login(usuario)
        segundos = segundos_ate_desbloqueio_login(chave)
        if segundos:
            flash(f"Muitas tentativas. Tente novamente em {max(1, (segundos + 59) // 60)} min.")
            return render_template("admin_login.html"), 429

        admin = verificar_admin(
            usuario,
            request.form.get("senha", ""),
        )
        if admin:
            limpar_tentativas_login(chave)
            session.clear()
            session["admin_id"] = admin["id"]
            session.permanent = True
            registrar_auditoria(admin["id"], "entrou no painel")
            return redirect(url_for("admin_funcionarios"))
        segundos = registrar_falha_login(chave)
        if segundos:
            flash(f"Muitas tentativas. Tente novamente em {max(1, (segundos + 59) // 60)} min.")
            return render_template("admin_login.html"), 429
        flash("Usuário ou senha incorretos.")
    return render_template("admin_login.html")


@app.route("/admin/sair")
def admin_sair():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/senha", methods=["GET", "POST"])
@login_obrigatorio
def admin_trocar_senha():
    if request.method == "POST":
        atual = request.form.get("senha_atual", "")
        nova = request.form.get("senha_nova", "")
        repeticao = request.form.get("senha_repeticao", "")
        if len(nova) < 12:
            flash("A nova senha deve ter pelo menos 12 caracteres.")
        elif nova != repeticao:
            flash("As senhas novas não conferem.")
        elif nova == atual:
            flash("A nova senha deve ser diferente da atual.")
        elif not trocar_propria_senha(session["admin_id"], atual, nova):
            flash("A senha atual está incorreta.")
        else:
            registrar_auditoria(session["admin_id"], "trocou a própria senha")
            flash("Senha alterada com sucesso.")
            return redirect(url_for("admin_funcionarios"))
    obrigatorio = bool(buscar_admin(session["admin_id"])["precisa_trocar_senha"])
    return render_template("admin_trocar_senha.html", obrigatorio=obrigatorio)


def render_admins(temporaria=None, usuario_temp=None):
    return render_template(
        "admin_admins.html",
        admins=listar_admins(),
        atual=session["admin_id"],
        atividades=listar_auditoria(30),
        temporaria=temporaria,
        usuario_temp=usuario_temp,
    )


@app.route("/admin/administradores")
@login_obrigatorio
def admin_administradores():
    return render_admins()


@app.route("/admin/administradores/novo", methods=["POST"])
@login_obrigatorio
def admin_novo_admin():
    usuario = request.form.get("usuario", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,30}", usuario):
        flash("Usuário: de 3 a 30 caracteres, só letras, números, ponto, hífen ou sublinhado.")
        return redirect(url_for("admin_administradores"))

    temporaria = gerar_senha_temporaria()
    try:
        criar_admin_com_senha_temporaria(usuario, temporaria)
    except sqlite3.IntegrityError:
        flash("Esse usuário já existe.")
        return redirect(url_for("admin_administradores"))

    registrar_auditoria(session["admin_id"], "criou o administrador", usuario)
    # Sem redirect: a senha temporária aparece uma única vez, sem ficar guardada no cookie
    return render_admins(temporaria=temporaria, usuario_temp=usuario)


@app.route("/admin/administradores/<int:admin_id>/redefinir", methods=["POST"])
@login_obrigatorio
def admin_redefinir_admin(admin_id):
    alvo = buscar_admin(admin_id)
    if alvo is None:
        abort(404)
    if alvo["id"] == session["admin_id"]:
        flash("Para trocar a sua própria senha, use \"Trocar minha senha\".")
        return redirect(url_for("admin_administradores"))

    temporaria = redefinir_senha_de(admin_id)
    registrar_auditoria(session["admin_id"], "redefiniu a senha de", alvo["usuario"])
    return render_admins(temporaria=temporaria, usuario_temp=alvo["usuario"])


@app.route("/admin/administradores/<int:admin_id>/remover", methods=["POST"])
@login_obrigatorio
def admin_remover_admin(admin_id):
    alvo = buscar_admin(admin_id)
    if alvo is None:
        abort(404)
    resultado = remover_admin(admin_id, session["admin_id"])
    if resultado == "proprio":
        flash("Você não pode remover a si mesmo.")
    elif resultado == "ultimo":
        flash("Não é possível remover o último administrador.")
    else:
        registrar_auditoria(session["admin_id"], "removeu o administrador", alvo["usuario"])
        flash(f"Administrador {alvo['usuario']} removido.")
    return redirect(url_for("admin_administradores"))


# ---------- Funcionários ----------

@app.route("/admin")
@login_obrigatorio
def admin_funcionarios():
    return render_template("admin_funcionarios.html",
                           funcionarios=listar_funcionarios())


@app.route("/admin/funcionarios/novo", methods=["POST"])
@login_obrigatorio
def admin_novo_funcionario():
    nome = request.form.get("nome", "").strip()
    codigo = request.form.get("codigo", "").strip()

    if not nome:
        flash("Informe o nome.")
    elif not (codigo.isdigit() and 4 <= len(codigo) <= 8):
        flash("O código deve ter de 4 a 8 números.")
    else:
        try:
            cadastrar_funcionario(nome, codigo)
            flash(f"{nome} cadastrado(a) com sucesso.")
        except sqlite3.IntegrityError:
            flash("Esse código já está em uso. Escolha outro.")
    return redirect(url_for("admin_funcionarios"))


@app.route("/admin/funcionarios/<int:funcionario_id>/alternar", methods=["POST"])
@login_obrigatorio
def admin_alternar(funcionario_id):
    alternar_ativo(funcionario_id)
    return redirect(url_for("admin_funcionarios"))


@app.route("/admin/funcionarios/<int:funcionario_id>/codigo", methods=["GET", "POST"])
@login_obrigatorio
def admin_codigo(funcionario_id):
    funcionario = buscar_funcionario(funcionario_id)
    if funcionario is None:
        abort(404)

    if request.method == "POST":
        codigo = request.form.get("codigo", "").strip()
        if not (codigo.isdigit() and 4 <= len(codigo) <= 8):
            flash("O código deve ter de 4 a 8 números.")
        else:
            try:
                redefinir_codigo(funcionario_id, codigo)
            except sqlite3.IntegrityError:
                flash("Esse código já está em uso. Escolha outro.")
            else:
                registrar_auditoria(session["admin_id"], "redefiniu o código de",
                                    funcionario["nome"])
                flash(f"Novo código definido para {funcionario['nome']}.")
                return redirect(url_for("admin_funcionarios"))
    return render_template("admin_codigo.html", funcionario=funcionario)


@app.route("/admin/funcionarios/<int:funcionario_id>/folha")
@login_obrigatorio
def admin_folha(funcionario_id):
    funcionario = buscar_funcionario(funcionario_id)
    if funcionario is None:
        abort(404)

    agora = datetime.now()
    texto_mes = request.args.get("mes") or agora.strftime("%Y-%m")
    dados = calcular_dados_folha(funcionario, texto_mes, agora)
    if dados is None:
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id))
    return render_template("admin_folha.html", funcionario=funcionario, **dados)


@app.route("/admin/funcionarios/<int:funcionario_id>/folha/arquivar", methods=["POST"])
@login_obrigatorio
def admin_arquivar_funcionario(funcionario_id):
    funcionario = buscar_funcionario(funcionario_id)
    if funcionario is None:
        abort(404)

    if funcionario["ativo"]:
        arquivar_funcionario(funcionario_id)
        registrar_auditoria(
            session["admin_id"], "arquivou funcionário", funcionario["nome"],
        )
        flash(f"{funcionario['nome']} foi arquivado(a). O histórico de ponto foi preservado.")
    else:
        flash(f"{funcionario['nome']} já está arquivado(a). O histórico de ponto foi preservado.")

    mes = request.form.get("mes")
    return redirect(url_for("admin_folha", funcionario_id=funcionario_id, mes=mes))


def calcular_dados_folha(funcionario, texto_mes, agora):
    try:
        primeiro = datetime.strptime(texto_mes + "-01", "%Y-%m-%d").date()
    except ValueError:
        return None
    ultimo = primeiro.replace(day=calendar.monthrange(primeiro.year, primeiro.month)[1])

    cfg = obter_configuracoes()
    cal = montar_calendario()
    cadastro = datetime.strptime(funcionario["criado_em"][:10], "%Y-%m-%d").date()
    funcionario_id = funcionario["id"]

    # Mês exibido: só conta a partir do dia do cadastro
    linhas = calcular_periodo(funcionario_id, max(primeiro, cadastro), ultimo, cfg, cal, agora)

    # Saldo anterior: soma de tudo, do cadastro até o dia anterior ao mês exibido
    anteriores = calcular_periodo(funcionario_id, cadastro,
                                  primeiro - timedelta(days=1), cfg, cal, agora)
    saldo_anterior = somar_saldo(anteriores)

    computados = [l for l in linhas if l["r"]["situacao"] in SITUACOES_CALCULADAS]
    positivo = sum(l["r"]["saldo"] for l in computados if l["r"]["saldo"] > 0)
    negativo = sum(l["r"]["saldo"] for l in computados if l["r"]["saldo"] < 0)
    saldo_mes = positivo + negativo

    resumo = {
        "dias_trabalhados": len(computados),
        "faltas": sum(1 for l in linhas if l["r"]["situacao"] == "falta"),
        "pendentes": sum(1 for l in linhas if l["r"]["situacao"] == "pendente"),
        "dias_com_atraso": sum(1 for l in computados
                               if l["r"]["atraso"] > 0 or l["r"]["almoco_excedente"] > 0),
        "positivo": formatar(positivo, True),
        "negativo": formatar(negativo, True),
        "saldo_mes": formatar(saldo_mes, True),
        "anterior": formatar(saldo_anterior, True),
        "final": formatar(saldo_anterior + saldo_mes, True),
    }

    mes_proximo = (ultimo + timedelta(days=1)).strftime("%Y-%m")
    return {
        "titulo_mes": f"{MESES[primeiro.month - 1]} de {primeiro.year}",
        "mes_anterior": (primeiro - timedelta(days=1)).strftime("%Y-%m"),
        "mes_proximo": mes_proximo,
        "tem_proximo": mes_proximo <= agora.strftime("%Y-%m"),
        "linhas": [linha_para_tela(l) for l in linhas],
        "resumo": resumo,
    }


@app.route("/admin/funcionarios/<int:funcionario_id>/folha/pdf")
@login_obrigatorio
def admin_exportar_folha_pdf(funcionario_id):
    funcionario = buscar_funcionario(funcionario_id)
    if funcionario is None:
        abort(404)
    agora = datetime.now()
    texto_mes = request.args.get("mes") or agora.strftime("%Y-%m")
    dados = calcular_dados_folha(funcionario, texto_mes, agora)
    if dados is None:
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id))

    arquivo = gerar_pdf_folha(
        funcionario["nome"], dados["titulo_mes"], dados["linhas"], dados["resumo"],
    )
    registrar_auditoria(
        session["admin_id"], "exportou a folha em PDF",
        f"{funcionario['nome']} - {texto_mes}",
    )
    nome = secure_filename(f"folha_{funcionario['nome']}_{texto_mes}.pdf")
    return send_file(
        arquivo, mimetype="application/pdf", as_attachment=True,
        download_name=nome, max_age=0,
    )


@app.route("/admin/funcionarios/<int:funcionario_id>/folha/excel")
@login_obrigatorio
def admin_exportar_folha_excel(funcionario_id):
    funcionario = buscar_funcionario(funcionario_id)
    if funcionario is None:
        abort(404)
    agora = datetime.now()
    texto_mes = request.args.get("mes") or agora.strftime("%Y-%m")
    dados = calcular_dados_folha(funcionario, texto_mes, agora)
    if dados is None:
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id))

    arquivo = gerar_excel_folha(
        funcionario["nome"], dados["titulo_mes"], dados["linhas"], dados["resumo"],
    )
    registrar_auditoria(
        session["admin_id"], "exportou a folha em Excel",
        f"{funcionario['nome']} - {texto_mes}",
    )
    nome = secure_filename(f"folha_{funcionario['nome']}_{texto_mes}.xlsx")
    return send_file(
        arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=nome,
        max_age=0,
    )


@app.route("/admin/funcionarios/<int:funcionario_id>/batidas/adicionar", methods=["POST"])
@login_obrigatorio
def admin_adicionar_batida(funcionario_id):
    funcionario = buscar_funcionario(funcionario_id)
    if funcionario is None:
        abort(404)

    texto_data = request.form.get("data", "").strip()
    hora = request.form.get("hora", "").strip()
    tipo = request.form.get("tipo", "").strip()
    motivo = request.form.get("motivo", "").strip()
    try:
        data = datetime.strptime(texto_data, "%Y-%m-%d").date()
    except ValueError:
        flash("Informe uma data válida para a batida.")
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id))
    if not hora_valida(hora):
        flash("Informe um horário válido para a batida.")
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id, mes=texto_data[:7]))
    if not motivo or len(motivo) > 500:
        flash("Informe o motivo do ajuste (até 500 caracteres).")
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id, mes=texto_data[:7]))

    agora = datetime.now()
    cadastro = datetime.strptime(funcionario["criado_em"][:10], "%Y-%m-%d").date()
    if data > agora.date() or data < cadastro:
        flash("A data precisa estar entre o cadastro do funcionário e hoje.")
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id, mes=texto_data[:7]))

    data_hora = datetime.strptime(f"{texto_data} {hora}", "%Y-%m-%d %H:%M")
    if data_hora > agora:
        flash("A batida não pode estar no futuro.")
        return redirect(url_for("admin_folha", funcionario_id=funcionario_id, mes=texto_data[:7]))

    jornada = jornada_do_dia(data, montar_calendario())
    tipos_esperados = TIPOS_COM_ALMOCO if jornada and jornada["almoco_min"] > 0 else TIPOS_SEM_ALMOCO
    try:
        adicionar_batida_manual(
            funcionario_id,
            tipo,
            data_hora.strftime("%Y-%m-%d %H:%M:%S"),
            tipos_esperados,
        )
    except ValueError as erro:
        flash(str(erro))
    else:
        registrar_auditoria(
            session["admin_id"],
            "adicionou batida esquecida",
            f"{funcionario['nome']} - {data_hora:%d/%m/%Y %H:%M} "
            f"({ROTULOS[tipo]}); motivo: {motivo}",
        )
        flash("Batida adicionada. O registro original não foi alterado.")
    return redirect(url_for("admin_folha", funcionario_id=funcionario_id, mes=texto_data[:7]))


@app.route("/admin/backup")
@login_obrigatorio
def admin_backup():
    arquivo = BytesIO(criar_backup())
    nome = f"backup_ponto_{datetime.now():%Y%m%d_%H%M%S}.sqlite"
    registrar_auditoria(session["admin_id"], "baixou backup do banco")
    return send_file(
        arquivo, mimetype="application/vnd.sqlite3", as_attachment=True,
        download_name=nome, max_age=0,
    )


# ---------- Configurações (jornada, tolerâncias, feriados) ----------

@app.route("/admin/configuracoes")
@login_obrigatorio
def admin_configuracoes():
    return render_template(
        "admin_configuracoes.html",
        jornada=obter_jornada(),
        nomes_dias=NOMES_DIAS,
        regras=obter_configuracoes(),
        feriados=listar_feriados(),
    )


@app.route("/admin/configuracoes/jornada", methods=["POST"])
@login_obrigatorio
def admin_salvar_jornada():
    novos = []
    for dia in range(7):
        trabalha = 1 if request.form.get(f"trabalha_{dia}") else 0
        entrada = request.form.get(f"entrada_{dia}", "").strip()
        saida = request.form.get(f"saida_{dia}", "").strip()
        try:
            almoco = int(request.form.get(f"almoco_{dia}") or 0)
        except ValueError:
            almoco = -1

        if trabalha:
            if not (hora_valida(entrada) and hora_valida(saida)) or entrada >= saida:
                flash(f"{NOMES_DIAS[dia]}: informe entrada e saída válidas "
                      "(a saída deve ser depois da entrada).")
                return redirect(url_for("admin_configuracoes"))
            if not 0 <= almoco <= 240:
                flash(f"{NOMES_DIAS[dia]}: o almoço deve ter de 0 a 240 minutos.")
                return redirect(url_for("admin_configuracoes"))
        else:
            entrada, saida, almoco = None, None, 0

        novos.append((dia, trabalha, entrada, saida, almoco))

    salvar_jornada(novos)
    flash("Jornada salva.")
    return redirect(url_for("admin_configuracoes"))


@app.route("/admin/configuracoes/regras", methods=["POST"])
@login_obrigatorio
def admin_salvar_regras():
    try:
        tolerancias = [int(request.form.get(campo, ""))
                       for campo in ("tol_entrada", "tol_almoco", "tol_saida")]
    except ValueError:
        tolerancias = [-1]
    hora_limite = request.form.get("hora_limite", "").strip()

    if not all(0 <= t <= 30 for t in tolerancias):
        flash("As tolerâncias devem ser números de 0 a 30 minutos.")
    elif not hora_valida(hora_limite):
        flash("Informe um horário limite válido.")
    else:
        salvar_configuracoes(*tolerancias, hora_limite)
        flash("Regras salvas.")
    return redirect(url_for("admin_configuracoes"))


@app.route("/admin/configuracoes/feriados/novo", methods=["POST"])
@login_obrigatorio
def admin_novo_feriado():
    data = request.form.get("data", "").strip()
    descricao = request.form.get("descricao", "").strip()
    try:
        datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        flash("Informe uma data válida.")
        return redirect(url_for("admin_configuracoes"))
    if not descricao:
        flash("Informe a descrição do feriado ou folga.")
        return redirect(url_for("admin_configuracoes"))
    adicionar_feriado(data, descricao)
    flash("Feriado/folga salvo.")
    return redirect(url_for("admin_configuracoes"))


@app.route("/admin/configuracoes/feriados/remover", methods=["POST"])
@login_obrigatorio
def admin_remover_feriado():
    remover_feriado(request.form.get("data", ""))
    return redirect(url_for("admin_configuracoes"))


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )