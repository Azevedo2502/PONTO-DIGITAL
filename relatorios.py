from html import escape
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _texto_seguro(texto):
    return escape(str(texto or "—"))


def _literal_excel(valor):
    if isinstance(valor, str) and valor.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + valor
    return valor


def gerar_pdf_folha(nome, titulo_mes, linhas, resumo):
    arquivo = BytesIO()
    pagina = landscape(A4)
    estilos = getSampleStyleSheet()
    estilos["Title"].textColor = colors.HexColor("#171717")
    estilos["Title"].fontSize = 17
    estilos["Title"].leading = 20
    estilos["Normal"].fontSize = 8
    estilos["Normal"].leading = 10
    documento = SimpleDocTemplate(
        arquivo,
        pagesize=pagina,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=26 * mm,
        title=f"Folha de ponto - {nome} - {titulo_mes}",
        author="Ponto STAR",
    )

    conteudo = [
        Paragraph("Folha de ponto — STAR Serviços Automotivos", estilos["Title"]),
        Paragraph(f"Funcionário: <b>{_texto_seguro(nome)}</b>", estilos["Normal"]),
        Paragraph(f"Período: <b>{_texto_seguro(titulo_mes)}</b>", estilos["Normal"]),
        Spacer(1, 5 * mm),
    ]

    indicadores = [
        ("Dias trabalhados", resumo["dias_trabalhados"]),
        ("Faltas", resumo["faltas"]),
        ("Dias pendentes", resumo["pendentes"]),
        ("Dias com atraso", resumo["dias_com_atraso"]),
        ("Saldo do mês", resumo["saldo_mes"]),
        ("Saldo anterior", resumo["anterior"]),
        ("Saldo final", resumo["final"]),
    ]
    conteudo.append(Table(
        [[Paragraph(
            f"<b>{_texto_seguro(rotulo)}</b><br/>{_texto_seguro(valor)}",
            estilos["Normal"],
        ) for rotulo, valor in indicadores]],
        colWidths=[(pagina[0] - 24 * mm) / len(indicadores)] * len(indicadores),
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f3f3")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]),
    ))
    conteudo.append(Spacer(1, 4 * mm))

    cabecalho = [
        "Data", "Dia", "Entrada", "Saída almoço", "Volta", "Saída",
        "Trabalhado", "Saldo", "Situação / observações",
    ]
    dados = [cabecalho]
    for linha in linhas:
        colunas = linha["colunas"]
        observacoes = [linha["texto"], *linha["alertas"]]
        dados.append([
            linha["data"],
            linha["dia"],
            *[horario or "—" for horario in colunas],
            linha["trabalhado"],
            linha["saldo"],
            Paragraph(
                "<br/>".join(_texto_seguro(texto) for texto in observacoes),
                estilos["Normal"],
            ),
        ])

    tabela = Table(
        dados,
        repeatRows=1,
        colWidths=[12 * mm, 10 * mm, 15 * mm, 20 * mm, 14 * mm,
                   14 * mm, 20 * mm, 17 * mm, 140 * mm],
        hAlign="LEFT",
    )
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222426")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEADING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c8c8c8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-2, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    conteudo.append(tabela)
    conteudo.extend([
        Spacer(1, 13 * mm),
        Table(
            [
                ["________________________________________", "________________________________________"],
                ["Assinatura do funcionário", "Assinatura do responsável"],
            ],
            colWidths=[(pagina[0] - 24 * mm) / 2] * 2,
            style=TableStyle([
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica"),
                ("FONTSIZE", (0, 1), (-1, 1), 8),
                ("TOPPADDING", (0, 1), (-1, 1), 4),
            ]),
        ),
    ])

    def rodape(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawRightString(pagina[0] - 12 * mm, 8 * mm, f"Página {doc.page}")
        canvas.restoreState()

    documento.build(conteudo, onFirstPage=rodape, onLaterPages=rodape)
    arquivo.seek(0)
    return arquivo


def gerar_excel_folha(nome, titulo_mes, linhas, resumo):
    arquivo = BytesIO()
    pasta = Workbook()
    planilha = pasta.active
    planilha.title = "Folha de ponto"
    planilha.sheet_view.showGridLines = False
    planilha.merge_cells("A1:J1")
    planilha["A1"] = "STAR Serviços Automotivos — Folha de ponto"
    planilha["A1"].font = Font(size=16, bold=True, color="FFFFFF")
    planilha["A1"].fill = PatternFill("solid", fgColor="222426")
    planilha["A1"].alignment = Alignment(horizontal="center")
    planilha.merge_cells("A2:J2")
    planilha["A2"] = _literal_excel(f"Funcionário: {nome}    |    Período: {titulo_mes}")
    planilha["A2"].font = Font(bold=True)

    indicadores = [
        ("Dias trabalhados", resumo["dias_trabalhados"]),
        ("Faltas", resumo["faltas"]),
        ("Dias pendentes", resumo["pendentes"]),
        ("Dias com atraso", resumo["dias_com_atraso"]),
        ("Saldo do mês", resumo["saldo_mes"]),
        ("Saldo anterior", resumo["anterior"]),
        ("Saldo final", resumo["final"]),
    ]
    for coluna, (rotulo, valor) in enumerate(indicadores, start=1):
        celula = planilha.cell(
            row=4, column=coluna, value=_literal_excel(f"{rotulo}: {valor}"),
        )
        celula.alignment = Alignment(wrap_text=True, vertical="center")
        celula.font = Font(bold=True, color="333333")
        celula.fill = PatternFill("solid", fgColor="F3F3F3")
    planilha.row_dimensions[4].height = 30

    cabecalho = [
        "Data", "Dia", "Entrada", "Saída almoço", "Volta", "Saída",
        "Trabalhado", "Saldo", "Situação", "Observações",
    ]
    linha_cabecalho = 6
    for coluna, titulo in enumerate(cabecalho, start=1):
        celula = planilha.cell(row=linha_cabecalho, column=coluna, value=titulo)
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="222426")
        celula.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for indice, linha in enumerate(linhas, start=linha_cabecalho + 1):
        observacoes = "; ".join(linha["alertas"])
        valores = [
            linha["data"], linha["dia"], *[horario or "—" for horario in linha["colunas"]],
            linha["trabalhado"], linha["saldo"], linha["texto"], observacoes,
        ]
        for coluna, valor in enumerate(valores, start=1):
            celula = planilha.cell(row=indice, column=coluna, value=_literal_excel(valor))
            celula.alignment = Alignment(vertical="top", wrap_text=True)
            if indice % 2:
                celula.fill = PatternFill("solid", fgColor="F7F7F7")

    assinatura = linha_cabecalho + len(linhas) + 3
    borda = Border(bottom=Side(style="thin", color="666666"))
    for coluna in range(1, 11):
        planilha.cell(row=assinatura, column=coluna).border = borda
    planilha.merge_cells(start_row=assinatura + 1, start_column=1,
                         end_row=assinatura + 1, end_column=5)
    planilha.merge_cells(start_row=assinatura + 1, start_column=6,
                         end_row=assinatura + 1, end_column=10)
    planilha.cell(assinatura + 1, 1, "Assinatura do funcionário")
    planilha.cell(assinatura + 1, 6, "Assinatura do responsável")
    planilha.cell(assinatura + 1, 1).alignment = Alignment(horizontal="center")
    planilha.cell(assinatura + 1, 6).alignment = Alignment(horizontal="center")

    larguras = [12, 9, 12, 15, 11, 11, 14, 12, 24, 48]
    for coluna, largura in enumerate(larguras, start=1):
        planilha.column_dimensions[get_column_letter(coluna)].width = largura
    planilha.freeze_panes = "A7"
    planilha.auto_filter.ref = f"A{linha_cabecalho}:J{linha_cabecalho + len(linhas)}"
    planilha.sheet_properties.pageSetUpPr.fitToPage = True
    planilha.page_setup.orientation = "landscape"
    planilha.page_setup.fitToWidth = 1
    planilha.page_setup.fitToHeight = 0
    planilha.print_title_rows = f"1:{linha_cabecalho}"
    planilha.print_area = f"A1:J{assinatura + 1}"

    pasta.save(arquivo)
    arquivo.seek(0)
    return arquivo
