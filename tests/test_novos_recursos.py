import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

TEST_DATA = tempfile.TemporaryDirectory(prefix="ponto-testes-")
os.environ["PONTO_DB_PATH"] = str(Path(TEST_DATA.name) / "teste.sqlite")
os.environ["APP_ENV"] = "development"

import app as modulo_app
import db
from contas import criar_admin_inicial_se_necessario, preparar_banco
from openpyxl import load_workbook


class RecursosNovosTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        TEST_DATA.cleanup()

    def setUp(self):
        db.DB_PATH = Path(os.environ["PONTO_DB_PATH"])
        db.criar_tabelas()
        preparar_banco()
        with db.conectar() as conn:
            conn.execute("DELETE FROM registros")
            conn.execute("DELETE FROM funcionarios")
            conn.execute("DELETE FROM admins")
            conn.execute("DELETE FROM auditoria")
            conn.execute("DELETE FROM tentativas_login")
        modulo_app.app.config.update(TESTING=True)
        self.cliente = modulo_app.app.test_client()
        db.cadastrar_funcionario("Funcionário Teste", "12345")
        self.funcionario_id = db.buscar_funcionario_por_codigo("12345")["id"]
        db.criar_admin("admin-teste", "senha-teste")
        resposta = self.cliente.post(
            "/admin/login",
            data={"usuario": "admin-teste", "senha": "senha-teste"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            follow_redirects=True,
        )
        self.assertEqual(resposta.status_code, 200)

    def test_cria_admin_inicial_e_exige_troca_de_senha(self):
        with db.conectar() as conn:
            conn.execute("DELETE FROM admins")

        with patch.dict(os.environ, {
            "ADMIN_INICIAL_USUARIO": "admin-inicial",
            "ADMIN_INICIAL_SENHA": "senha-inicial-segura",
        }):
            self.assertTrue(criar_admin_inicial_se_necessario())
            self.assertFalse(criar_admin_inicial_se_necessario())

        admin = db.verificar_admin("admin-inicial", "senha-inicial-segura")
        self.assertIsNotNone(admin)
        self.assertEqual(admin["precisa_trocar_senha"], 1)
        with modulo_app.app.test_client() as cliente:
            resposta = cliente.post(
                "/admin/login",
                data={"usuario": "admin-inicial", "senha": "senha-inicial-segura"},
                follow_redirects=True,
            )
        self.assertEqual(resposta.request.path, "/admin/senha")

    def test_admin_inicial_nao_substitui_admin_existente(self):
        with patch.dict(os.environ, {
            "ADMIN_INICIAL_USUARIO": "outro-admin",
            "ADMIN_INICIAL_SENHA": "senha-inicial-segura",
        }):
            self.assertFalse(criar_admin_inicial_se_necessario())

        with db.conectar() as conn:
            admins = conn.execute("SELECT usuario FROM admins").fetchall()
        self.assertEqual([admin["usuario"] for admin in admins], ["admin-teste"])

    def test_admin_inicial_rejeita_senha_curta(self):
        with db.conectar() as conn:
            conn.execute("DELETE FROM admins")

        with patch.dict(os.environ, {
            "ADMIN_INICIAL_USUARIO": "admin-inicial",
            "ADMIN_INICIAL_SENHA": "curta",
        }), self.assertRaisesRegex(RuntimeError, "pelo menos 8 caracteres"):
            criar_admin_inicial_se_necessario()

        with db.conectar() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
        self.assertEqual(total, 0)

    def test_adiciona_batida_sem_apagar_historico(self):
        agora = datetime.now()
        data = agora.date().isoformat()
        horario = (agora - timedelta(minutes=10)).strftime("%H:%M")
        resposta = self.cliente.post(
            f"/admin/funcionarios/{self.funcionario_id}/batidas/adicionar",
            data={
                "data": data,
                "hora": horario,
                "tipo": "entrada",
                "motivo": "Registro esquecido",
            },
            follow_redirects=True,
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"Batida adicionada", resposta.data)
        with db.conectar() as conn:
            batidas = conn.execute(
                "SELECT tipo, data_hora FROM registros WHERE funcionario_id = ?",
                (self.funcionario_id,),
            ).fetchall()
            auditoria = conn.execute(
                "SELECT acao, detalhe FROM auditoria WHERE acao = ?",
                ("adicionou batida esquecida",),
            ).fetchone()
        self.assertEqual(len(batidas), 1)
        self.assertEqual(batidas[0]["tipo"], "entrada")
        self.assertIn("Registro esquecido", auditoria["detalhe"])

    def test_arquiva_funcionario_preservando_historico_e_registrando_auditoria(self):
        agora = datetime.now()
        data_hora = (agora - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with db.conectar() as conn:
            conn.execute(
                "INSERT INTO registros (funcionario_id, tipo, data_hora) VALUES (?, ?, ?)",
                (self.funcionario_id, "entrada", data_hora),
            )

        folha_url = f"/admin/funcionarios/{self.funcionario_id}/folha"
        folha = self.cliente.get(folha_url)
        self.assertIn(b"Arquivar funcion", folha.data)
        resposta = self.cliente.post(
            f"{folha_url}/arquivar",
            data={"mes": agora.strftime("%Y-%m")},
            follow_redirects=True,
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"preservado.", resposta.data)
        self.assertIn(b"arquivado", resposta.data)

        funcionario = db.buscar_funcionario(self.funcionario_id)
        self.assertEqual(funcionario["ativo"], 0)
        self.assertIsNone(db.buscar_funcionario_por_codigo("12345"))
        with db.conectar() as conn:
            total_batidas = conn.execute(
                "SELECT COUNT(*) AS total FROM registros WHERE funcionario_id = ?",
                (self.funcionario_id,),
            ).fetchone()["total"]
            auditoria = conn.execute(
                "SELECT acao, detalhe FROM auditoria WHERE acao = ?",
                ("arquivou funcionário",),
            ).fetchone()
        self.assertEqual(total_batidas, 1)
        self.assertEqual(auditoria["detalhe"], "Funcionário Teste")

    def test_folha_usa_tema_star_e_preserva_acoes(self):
        folha = self.cliente.get(f"/admin/funcionarios/{self.funcionario_id}/folha")
        css = self.cliente.get("/static/admin.css")
        try:
            self.assertEqual(folha.status_code, 200)
            self.assertIn(b"admin.css", folha.data)
            self.assertIn(b"img/logo-star.png", folha.data)
            self.assertIn(b"Registros do m", folha.data)
            self.assertIn(b"Baixar PDF", folha.data)
            self.assertIn(b"Baixar Excel", folha.data)
            self.assertIn(b"Adicionar batida esquecida", folha.data)
            self.assertEqual(css.status_code, 200)
            self.assertIn(b".folha-resumo", css.data)
            self.assertIn(b".folha-tabela", css.data)
        finally:
            folha.close()
            css.close()

    def test_tela_quiosque_tem_logo_e_arquivos_de_instalacao(self):
        resposta = self.cliente.get("/")
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"STAR Servi", resposta.data)
        self.assertIn(b"apple-mobile-web-app-capable", resposta.data)
        manifest = self.cliente.get("/static/manifest.webmanifest")
        icone = self.cliente.get("/static/img/icon-192.png")
        try:
            self.assertEqual(manifest.status_code, 200)
            self.assertEqual(icone.status_code, 200)
        finally:
            manifest.close()
            icone.close()

    def test_paginas_admin_usam_tema_star_e_exibem_configuracoes(self):
        configuracoes = self.cliente.get("/admin/configuracoes")
        administradores = self.cliente.get("/admin/administradores")
        css = self.cliente.get("/static/admin.css")
        try:
            self.assertEqual(configuracoes.status_code, 200)
            self.assertEqual(administradores.status_code, 200)
            self.assertEqual(css.status_code, 200)
            self.assertIn(b"Jornada semanal", configuracoes.data)
            self.assertIn(b"Feriados e folgas", configuracoes.data)
            self.assertIn(b"Salvar regras", configuracoes.data)
            self.assertIn(b"STAR Servi", configuracoes.data)
            self.assertIn(b"STAR Servi", administradores.data)
            self.assertIn(b"Atividades recentes", administradores.data)
            self.assertIn(b"--amarelo", css.data)
        finally:
            configuracoes.close()
            administradores.close()
            css.close()

    def test_login_e_painel_funcionarios_usam_tema_star(self):
        with modulo_app.app.test_client() as anonimo:
            login = anonimo.get("/admin/login")
            redirecionamento = anonimo.get("/admin")
        painel = self.cliente.get("/admin")
        try:
            self.assertEqual(login.status_code, 200)
            self.assertIn(b"admin.css", login.data)
            self.assertIn(b"img/logo-star.png", login.data)
            self.assertIn(b'autocomplete="username"', login.data)
            self.assertEqual(redirecionamento.status_code, 302)
            self.assertIn("/admin/login", redirecionamento.headers["Location"])
            self.assertEqual(painel.status_code, 200)
            self.assertIn(b"admin.css", painel.data)
            self.assertIn(b"img/logo-star.png", painel.data)
            self.assertIn(b"Cadastrar funcion", painel.data)
            self.assertIn(b"Redefinir c", painel.data)
        finally:
            login.close()
            redirecionamento.close()
            painel.close()

    def test_troca_senha_usa_tema_star_e_preserva_campos_de_seguranca(self):
        resposta = self.cliente.get("/admin/senha")
        try:
            self.assertEqual(resposta.status_code, 200)
            self.assertIn(b"admin.css", resposta.data)
            self.assertIn(b"img/logo-star.png", resposta.data)
            self.assertIn(b'name="senha_atual"', resposta.data)
            self.assertIn(b'name="senha_nova"', resposta.data)
            self.assertIn(b'minlength="12"', resposta.data)
            self.assertIn(b"Salvar nova senha", resposta.data)
        finally:
            resposta.close()

    def test_migra_hash_de_codigo_ao_validar_codigo_antigo(self):
        chave_atual = db.CHAVE_SECRETA
        chave_anterior = db.CHAVE_SECRETA_ANTERIOR
        try:
            db.CHAVE_SECRETA = b"chave-nova-de-teste"
            db.CHAVE_SECRETA_ANTERIOR = chave_atual
            funcionario = db.buscar_funcionario_por_codigo("12345")
            self.assertIsNotNone(funcionario)
            with db.conectar() as conn:
                hash_salvo = conn.execute(
                    "SELECT codigo_hash FROM funcionarios WHERE id = ?",
                    (funcionario["id"],),
                ).fetchone()["codigo_hash"]
            self.assertEqual(hash_salvo, db.hash_codigo("12345"))
        finally:
            db.CHAVE_SECRETA = chave_atual
            db.CHAVE_SECRETA_ANTERIOR = chave_anterior

    def test_bloqueia_login_apos_cinco_falhas(self):
        for tentativa in range(5):
            resposta = self.cliente.post(
                "/admin/login",
                data={"usuario": "inexistente", "senha": f"errada-{tentativa}"},
                environ_base={"REMOTE_ADDR": "192.0.2.10"},
            )
        self.assertEqual(resposta.status_code, 429)
        self.assertIn(b"Muitas tentativas", resposta.data)

    def test_rejeita_batida_fora_da_ordem(self):
        agora = datetime.now()
        data_hora = (agora - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with db.conectar() as conn:
            conn.execute(
                "INSERT INTO registros (funcionario_id, tipo, data_hora) VALUES (?, ?, ?)",
                (self.funcionario_id, "entrada", data_hora),
            )
        resposta = self.cliente.post(
            f"/admin/funcionarios/{self.funcionario_id}/batidas/adicionar",
            data={
                "data": agora.date().isoformat(),
                "hora": (agora - timedelta(hours=2)).strftime("%H:%M"),
                "tipo": "saida",
                "motivo": "A saída foi esquecida",
            },
            follow_redirects=True,
        )
        self.assertIn(b"ordem das batidas", resposta.data)
        with db.conectar() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS total FROM registros WHERE funcionario_id = ?",
                (self.funcionario_id,),
            ).fetchone()["total"]
        self.assertEqual(total, 1)

    def test_exporta_pdf_excel_e_backup_so_para_admin(self):
        with modulo_app.app.test_client() as anonimo:
            self.assertEqual(
                anonimo.get(f"/admin/funcionarios/{self.funcionario_id}/folha/pdf").status_code,
                302,
            )
            self.assertEqual(anonimo.get("/admin/backup").status_code, 302)

        folha_url = f"/admin/funcionarios/{self.funcionario_id}/folha"
        pdf = self.cliente.get(f"{folha_url}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertIn("attachment", pdf.headers["Content-Disposition"])
        pdf.close()

        excel = self.cliente.get(f"{folha_url}/excel")
        self.assertEqual(excel.status_code, 200)
        self.assertTrue(excel.data.startswith(b"PK"))
        pasta = load_workbook(BytesIO(excel.data), read_only=True)
        valores = [celula for linha in pasta.active.iter_rows(values_only=True) for celula in linha]
        pasta.close()
        self.assertIn("Assinatura do funcionário", valores)
        self.assertIn("Assinatura do responsável", valores)
        excel.close()

        backup = self.cliente.get("/admin/backup")
        self.assertEqual(backup.status_code, 200)
        self.assertTrue(backup.data.startswith(b"SQLite format 3"))
        self.assertIn("attachment", backup.headers["Content-Disposition"])
        with closing(sqlite3.connect(":memory:")) as restaurado:
            restaurado.deserialize(backup.data)
            tabelas = restaurado.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        self.assertGreater(tabelas, 0)
        backup.close()


if __name__ == "__main__":
    unittest.main()
