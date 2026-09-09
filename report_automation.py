import os
import smtplib
import sys
from datetime import datetime, date
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: script only saves PNG, never shows a window

import matplotlib.pyplot as plt
import pandas as pd
import pyodbc
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# As mensagens usam emojis (✅, ⚠️, ↘️) que não existem no codepage padrão do
# console do Windows em português (cp1252/cp850). Sem isto, um simples
# print() derruba o script com UnicodeEncodeError assim que a primeira
# mensagem com emoji é impressa.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ODBC_DRIVERS_SUPORTADOS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server")

SQL_QUERY = """
    select right([NOME_ARQUIVO], 52) as arquivo,
    [DATA_IMPORTACAO] as data_processamento,
    [DATA_IMPORTACAO_FINAL] as data_termino
    from [dbActyon_TIM].[dbo].[tbimportacao]
    order by [CONTRATANTE_ID] desc
"""

HEADER_TEXT = "MEF_Dados_Importações_Remessas"


def detect_odbc_driver() -> str:
    """Detecta o primeiro driver ODBC de SQL Server disponível nesta máquina.

    Tenta os drivers modernos primeiro (18, depois 17) e cai para o driver
    legado "SQL Server" por último, para não quebrar em máquinas que só têm
    ele instalado. Levanta RuntimeError se nenhum estiver presente.
    """
    drivers_disponiveis = pyodbc.drivers()
    for candidato in ODBC_DRIVERS_SUPORTADOS:
        if candidato in drivers_disponiveis:
            return candidato
    raise RuntimeError(
        f"Nenhum driver ODBC de SQL Server encontrado nesta máquina. "
        f"Drivers disponíveis: {drivers_disponiveis}"
    )


def get_connection_string() -> str:
    """Monta a connection string do SQL Server a partir das variáveis de ambiente (.env)."""
    return (
        f"DRIVER={{{detect_odbc_driver()}}};"
        f"SERVER={os.environ['MEF_DB_SERVER']},1433;"
        f"DATABASE={os.environ['MEF_DB_NAME']};"
        f"UID={os.environ['MEF_DB_UID']};"
        f"PWD={os.environ['MEF_DB_PWD']};"
    )


def fetch_import_data(connection_string: str) -> pd.DataFrame:
    """Consulta todos os arquivos já importados no banco (sem filtro de data)."""
    conn = pyodbc.connect(connection_string)
    try:
        df = pd.read_sql(SQL_QUERY, conn)
    finally:
        conn.close()
    df["data_processamento"] = pd.to_datetime(df["data_processamento"])
    df["data_termino"] = pd.to_datetime(df["data_termino"])
    return df


def filter_today_unique(df: pd.DataFrame, reference_date: date) -> pd.DataFrame:
    """Filtra para os arquivos concluídos na data de referência, únicos e ordenados.

    Exclui nomes com CSP_IMP/TIM, mantém só quem terminou (data_termino) na
    data informada, remove duplicatas (mantendo o primeiro) e ordena do mais
    recente para o mais antigo.
    """
    df_filtrado = df[~df["arquivo"].str.contains("CSP_IMP", case=False, na=False)]
    df_filtrado = df_filtrado[~df_filtrado["arquivo"].str.contains("TIM", case=False, na=False)]
    # data_termino (DATA_IMPORTACAO_FINAL) é quando o arquivo de fato terminou de importar;
    # arquivos ainda em processamento (data_termino nulo) ficam de fora.
    df_hoje = df_filtrado[df_filtrado["data_termino"].dt.date == reference_date]
    df_ordenado = df_hoje.sort_values(by="data_termino", ascending=True)
    df_unicos = df_ordenado.drop_duplicates(subset=["arquivo"], keep="first")
    return df_unicos.sort_values(by="data_termino", ascending=False)


def debug_filter_pipeline(df: pd.DataFrame, reference_date: date) -> dict:
    """Mostra quantos registros (e quais) são descartados em cada etapa do filtro,
    para diagnosticar divergências entre a query bruta e o relatório final."""
    apos_csp = df[~df["arquivo"].str.contains("CSP_IMP", case=False, na=False)]
    removidos_csp = df.loc[~df.index.isin(apos_csp.index), "arquivo"].tolist()

    apos_tim = apos_csp[~apos_csp["arquivo"].str.contains("TIM", case=False, na=False)]
    removidos_tim = apos_csp.loc[~apos_csp.index.isin(apos_tim.index), "arquivo"].tolist()

    apos_data = apos_tim[apos_tim["data_termino"].dt.date == reference_date]
    removidos_data = apos_tim.loc[~apos_tim.index.isin(apos_data.index), "arquivo"].tolist()

    apos_dedup = apos_data.sort_values("data_termino").drop_duplicates(
        subset=["arquivo"], keep="first"
    )
    removidos_dedup = apos_data.loc[~apos_data.index.isin(apos_dedup.index), "arquivo"].tolist()

    return {
        "total_no_banco": len(df),
        "apos_excluir_csp_imp": len(apos_csp),
        "removidos_csp_imp": removidos_csp,
        "apos_excluir_tim": len(apos_tim),
        "removidos_tim": removidos_tim,
        "apos_filtro_data": len(apos_data),
        "removidos_por_data": removidos_data,
        "apos_dedup": len(apos_dedup),
        "removidos_por_duplicata": removidos_dedup,
    }


def create_report_png(df_data: pd.DataFrame, output_path: str, header_text: str) -> None:
    """Gera o PNG do relatório diário (arquivo/data_processamento/data_termino)."""
    date_format = "%Y-%m-%d %H:%M:%S"
    df_display = df_data.copy()

    df_display["data_processamento"] = df_display["data_processamento"].dt.strftime(date_format)
    df_display["data_termino"] = df_display["data_termino"].dt.strftime(date_format)

    col_labels = ["arquivo", "data_processamento", "data_termino"]
    table_data = df_display[col_labels].values.tolist()
    if not table_data:
        table_data = [["(nenhum arquivo processado hoje)", "-", "-"]]

    report_datetime = datetime.now().strftime(date_format)
    full_header = f"{header_text}\n{report_datetime}"

    fig_height = min(1.0 + len(df_data) * 0.3, 6.5)
    fig, ax = plt.subplots(figsize=(10, fig_height), dpi=200)

    ax.axis("tight")
    ax.axis("off")

    ax.set_title(full_header, loc="left", fontsize=15, weight="bold")

    table = ax.table(
        cellText=table_data,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )

    table.auto_set_font_size(False)
    table.set_fontsize(6)
    table.scale(1.2, 1.3)

    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("black")
        cell.set_linewidth(0.7)

        if row == 0:
            cell.set_facecolor("#E6E6E6")
            cell.set_text_props(weight="bold", color="black")
        else:
            cell.set_facecolor("#FFFFFF" if row % 2 else "#F3F3F3")
            cell.set_text_props(color="black")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    plt.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)

    print(f"\nRelatório PNG gerado com sucesso em: {output_path}")


def build_gchat_message(df_final: pd.DataFrame, mention_user_ids: list[str] | str) -> dict:
    """Monta a mensagem do Google Chat listando os arquivos importados hoje, marcando os usuários."""
    if isinstance(mention_user_ids, str):
        mention_user_ids = [mention_user_ids]

    arquivos = df_final["arquivo"].tolist()

    if arquivos:
        lista = "\n".join(f"• {arquivo}" for arquivo in arquivos)
        corpo = (
            f"✅ *{HEADER_TEXT}*: e-mail de report entregue ao fornecedor.\n"
            f"{len(arquivos)} arquivo(s) importado(s) com sucesso:\n{lista}"
        )
    else:
        corpo = (
            f"✅ *{HEADER_TEXT}*: e-mail de report entregue ao fornecedor.\n"
            "Nenhum arquivo processado hoje."
        )

    mencoes = " ".join(f"<users/{user_id}>" for user_id in mention_user_ids)
    texto = f"{corpo}\n\n{mencoes} favor validar o relatório."
    return {"text": texto}


def send_gchat_notification(webhook_url: str, payload: dict) -> requests.Response:
    """Envia o payload (montado por build_*_gchat_message) para o webhook do Google Chat."""
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
    return response


def send_email_report_smtp(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    to_address: str,
    subject: str,
    html_body: str,
    image_path: str,
) -> None:
    """Envia e-mail HTML com a imagem do relatório embutida (cid:report_image), via SMTP/STARTTLS."""
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_address
    msg.attach(MIMEText(html_body, "html"))

    with open(image_path, "rb") as f:
        img = MIMEImage(f.read())
    img.add_header("Content-ID", "<report_image>")
    img.add_header("Content-Disposition", "inline", filename=os.path.basename(image_path))
    msg.attach(img)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)
