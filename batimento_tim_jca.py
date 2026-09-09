import os
import re
from datetime import date, datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: script only saves PNG, never shows a window

import matplotlib.pyplot as plt
import pandas as pd
import paramiko
import pyodbc
from google.oauth2 import service_account
from googleapiclient.discovery import build as build_google_api_service
from googleapiclient.http import MediaFileUpload

from report_automation import get_connection_string, send_gchat_notification
from validacao_remessas import already_sent_today, build_drive_folder_path, mark_sent_today

CREDORES = ["5260", "8660", "6201", "6202", "4360", "4361"]

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

QTDE_REGISTRO_QUERY = """
    select NOME_ARQUIVO, QTDE_REGISTRO, DATA_IMPORTACAO from tbimportacao
    where NOME_ARQUIVO like '%REMESSA%'
    and DATA_IMPORTACAO >= cast(getdate() as date)
    and DATA_IMPORTACAO < dateadd(day, 1, cast(getdate() as date))
    order by DATA_IMPORTACAO desc
"""

HEADER_TEXT = "BATIMENTO TIM JCA"


def build_batimento_title(reference_date: date) -> str:
    """Monta o título do report: '↘️ BATIMENTO TIM JCA | dd/mm/aaaa'."""
    return f"↘️ {HEADER_TEXT} | {reference_date.strftime('%d/%m/%Y')}"


def extract_credor(filename: str) -> str | None:
    """Extrai o código do credor (4 dígitos) do final do nome do arquivo.

    Ex.: REMESSA_CYBER_ASSESSORIA_070920262920_012_6202 -> "6202"; também
    aceita extensões compostas, ex.: "..._012_6202.txt.gz" (formato real dos
    arquivos no SFTP, compactados) -> "6202".
    Retorna None se nenhum código conhecido (CREDORES) for encontrado.
    """
    match = re.search(r"(\d{4})(?:\.\w+)*$", filename)
    if match and match.group(1) in CREDORES:
        return match.group(1)
    return None


def is_remessa_file(filename: str) -> bool:
    """Confere se o arquivo é uma remessa válida para o batimento.

    Precisa ter "REMESSA" no nome (mesmo filtro da query no banco,
    ``NOME_ARQUIVO like '%REMESSA%'``) e terminar em um credor conhecido.
    Sem o filtro por "REMESSA", arquivos de outro tipo que só coincidem no
    sufixo (ex.: EXC_BAI_..._6201.txt, de exclusão/baixa) entrariam na
    tabela e dariam divergência falsa, já que não existem na consulta.
    """
    return "REMESSA" in filename.upper() and extract_credor(filename) is not None


def count_file_lines(file_path: Path) -> int:
    """Conta a quantidade de linhas de um arquivo de remessa."""
    with open(file_path, "r", encoding="latin-1", errors="replace") as f:
        return sum(1 for _ in f)


def fetch_qtde_registro_by_arquivo(connection_string: str) -> dict[str, int]:
    """Consulta no banco o QTDE_REGISTRO de cada arquivo de remessa importado hoje.

    Normaliza NOME_ARQUIVO para o nome-base (sem caminho), em minúsculas,
    igual ao lookup já usado em validacao_remessas.build_validation_table.
    """
    conn = pyodbc.connect(connection_string)
    try:
        df = pd.read_sql(QTDE_REGISTRO_QUERY, conn)
    finally:
        conn.close()

    lookup: dict[str, int] = {}
    for _, row in df.iterrows():
        basename = str(row["NOME_ARQUIVO"]).replace("/", "\\").split("\\")[-1].strip().lower()
        if basename not in lookup:
            lookup[basename] = int(row["QTDE_REGISTRO"])
    return lookup


def build_batimento_table(
    drive_files: list[Path],
    qtde_registro_by_arquivo: dict[str, int],
) -> pd.DataFrame:
    """Monta a tabela REGISTRO/ARQUIVO/QTDE_REGISTRO/QTDE_ARQUIVO.

    QTDE_REGISTRO vem do banco (por nome de arquivo); QTDE_ARQUIVO é a
    contagem de linhas do arquivo físico no Drive. Recebe os caminhos
    completos (não só o nome) porque os arquivos ficam em subpastas por
    credor. Arquivos que não são remessas (sem "REMESSA" no nome ou sem
    credor conhecido) são ignorados.
    """
    rows = []
    for file_path in drive_files:
        arquivo = file_path.name
        if not is_remessa_file(arquivo):
            continue
        qtde_registro = qtde_registro_by_arquivo.get(arquivo.strip().lower(), 0)
        qtde_arquivo = count_file_lines(file_path)
        rows.append(
            {
                "ARQUIVO": arquivo,
                "QTDE_REGISTRO": qtde_registro,
                "QTDE_ARQUIVO": qtde_arquivo,
            }
        )

    df = pd.DataFrame(rows, columns=["ARQUIVO", "QTDE_REGISTRO", "QTDE_ARQUIVO"])
    df.insert(0, "REGISTRO", range(1, len(df) + 1))
    return df


def credores_pendentes(table_df: pd.DataFrame) -> list[str]:
    """Credores esperados (CREDORES) que ainda não têm arquivo de remessa na tabela."""
    credores_presentes = {extract_credor(arquivo) for arquivo in table_df["ARQUIVO"]}
    return sorted(set(CREDORES) - credores_presentes)


def list_sftp_files(
    host: str, port: int, username: str, password: str, remote_path: str, reference_date: date
) -> list[str]:
    """Lista os arquivos da pasta remota do SFTP modificados em `reference_date`.

    Filtra por `st_mtime` (data de modificação remota) porque a pasta
    acumula milhares de arquivos históricos de vários processos, não só os
    de hoje — sem esse filtro, todo credor que já mandou arquivo alguma vez
    apareceria pra sempre como "já chegou", mesmo com o arquivo de semanas
    atrás. É só leitura (listdir_attr) — não baixa nem altera nada no
    servidor remoto.
    """
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_client.connect(hostname=host, port=port, username=username, password=password, timeout=15)
    try:
        sftp = ssh_client.open_sftp()
        try:
            return [
                entrada.filename
                for entrada in sftp.listdir_attr(remote_path)
                if datetime.fromtimestamp(entrada.st_mtime).date() == reference_date
            ]
        finally:
            sftp.close()
    finally:
        ssh_client.close()


def credores_chegados_sftp(sftp_filenames: list[str]) -> set[str]:
    """Credores cujo arquivo de remessa já apareceu na pasta do SFTP."""
    return {extract_credor(nome) for nome in sftp_filenames if is_remessa_file(nome)}


def get_batimento_mention_user_ids() -> list[str]:
    """Lista fixa de usuários marcados neste report (independe do dia da semana)."""
    return [
        user_id.strip()
        for user_id in os.environ["MEF_GCHAT_MENTION_USER_IDS_BATIMENTO_JCA"].split(",")
        if user_id.strip()
    ]


def build_batimento_gchat_message(table_df: pd.DataFrame, title: str, mention_user_ids: list[str]) -> dict:
    """Monta a mensagem do Google Chat: um bloco por arquivo (✅/⚠️ + nome, com QTDE_REGISTRO/QTDE_ARQUIVO abaixo)."""
    if table_df.empty:
        corpo = f"{title}\n\nNenhum arquivo de remessa encontrado hoje."
    else:
        blocos = []
        for _, row in table_df.iterrows():
            status = "✅" if row["QTDE_REGISTRO"] == row["QTDE_ARQUIVO"] else "⚠️"
            blocos.append(
                f"*{status}{row['ARQUIVO']}*\n"
                f"- 🗂️ Registro_Base: *{row['QTDE_REGISTRO']}*\n"
                f"- 📄 Registro_Arquivo: *{row['QTDE_ARQUIVO']}*"
            )
        corpo = f"{title}\n\n" + "\n".join(blocos)

    mencoes = " ".join(f"<users/{user_id}>" for user_id in mention_user_ids)
    return {"text": f"{corpo}\n\n{mencoes}"}


def create_batimento_report_png(table_df: pd.DataFrame, output_path: str, title: str) -> None:
    """Gera o PNG do batimento, destacando em vermelho as linhas que não batem."""
    col_labels = ["REGISTRO", "ARQUIVO", "QTDE_REGISTRO", "QTDE_ARQUIVO"]
    table_data = table_df[col_labels].values.tolist() if not table_df.empty else []
    if not table_data:
        table_data = [["-", "(nenhum arquivo recebido hoje)", "-", "-"]]

    fig_height = min(1.2 + len(table_data) * 0.3, 8.0)
    fig, ax = plt.subplots(figsize=(10, fig_height), dpi=200)
    ax.axis("tight")
    ax.axis("off")
    ax.set_title(title, loc="left", fontsize=14, weight="bold")

    table = ax.table(cellText=table_data, colLabels=col_labels, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1.1, 1.4)

    col_widths = {0: 0.1, 1: 0.55, 2: 0.175, 3: 0.175}
    for (row, col), cell in table.get_celld().items():
        cell.set_width(col_widths[col])
        cell.set_edgecolor("black")
        cell.set_linewidth(0.6)
        if row == 0:
            cell.set_facecolor("#E6E6E6")
            cell.set_text_props(weight="bold", color="black")
        elif table_data[row - 1][2] != table_data[row - 1][3]:
            cell.set_facecolor("#F8D7DA")
            cell.set_text_props(color="black", weight="bold")
        else:
            cell.set_facecolor("#FFFFFF" if row % 2 else "#F3F3F3")
            cell.set_text_props(color="black")

    fig.tight_layout(pad=0.6)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    plt.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)

    print(f"\nRelatório de batimento gerado com sucesso em: {output_path}")


def upload_image_to_drive(service_account_json_path: str, folder_id: str, file_path: str) -> str:
    """Sobe o PNG para uma pasta do Google Drive, torna público e devolve a URL de imagem embutível.

    Requer uma conta de serviço com a Drive API ativada e com a pasta de
    destino compartilhada com ela (Editor). O arquivo fica acessível a
    "qualquer pessoa com o link" — é o único jeito do Google Chat conseguir
    baixar a imagem para exibir no CardsV2 (que só aceita URL, não upload
    binário direto).
    """
    if not Path(service_account_json_path).is_file():
        raise FileNotFoundError(
            f"Caminho da conta de serviço do Drive inválido: '{service_account_json_path}' "
            "(confira o .env — deve ser o caminho completo até o .json, não uma pasta)."
        )

    credentials = service_account.Credentials.from_service_account_file(
        service_account_json_path, scopes=DRIVE_SCOPES
    )
    drive_service = build_google_api_service("drive", "v3", credentials=credentials)

    file_metadata = {"name": os.path.basename(file_path), "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype="image/png")
    uploaded = drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    file_id = uploaded["id"]

    drive_service.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()

    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w1200"


def build_batimento_card_payload(title: str, image_url: str, mention_user_ids: list[str]) -> dict:
    """Monta o payload CardsV2 do Google Chat: imagem do report + menções."""
    mencoes = " ".join(f"<users/{user_id}>" for user_id in mention_user_ids)
    return {
        "cardsV2": [
            {
                "cardId": "batimento-tim-jca",
                "card": {
                    "header": {"title": title},
                    "sections": [
                        {"widgets": [{"image": {"imageUrl": image_url}}]},
                        {"widgets": [{"textParagraph": {"text": mencoes}}]},
                    ],
                },
            }
        ]
    }


def _checar_chegada_sftp(reference_date: date) -> None:
    """Consulta o SFTP e loga quais credores esperados ainda não apareceram lá hoje.

    Só informativo — não bloqueia o envio do report (decisão do usuário:
    "seguir com os outros processos mesmo que falte algum credor"). Uma
    falha de conexão com o SFTP é logada e ignorada, nunca derruba o report:
    a contagem de linhas continua vindo do arquivo físico já sincronizado
    na pasta do Drive, então o report sai mesmo sem confirmação do SFTP.
    """
    try:
        sftp_filenames = list_sftp_files(
            host=os.environ["MEF_SFTP_HOST"],
            port=int(os.environ.get("MEF_SFTP_PORT", "22")),
            username=os.environ["MEF_SFTP_USERNAME"],
            password=os.environ["MEF_SFTP_PASSWORD"],
            remote_path=os.environ.get("MEF_SFTP_REMOTE_PATH", "/tim_files/Recebidos"),
            reference_date=reference_date,
        )
    except Exception as e:
        print(f"[AVISO] Falha ao consultar o SFTP ({e}) — seguindo só com o que já está sincronizado no Drive.")
        return

    chegados = credores_chegados_sftp(sftp_filenames)
    pendentes_sftp = sorted(set(CREDORES) - chegados)
    if pendentes_sftp:
        print(f"Ainda sem sinal no SFTP para os credores {pendentes_sftp} — seguindo mesmo assim com o que já chegou.")
    else:
        print("Todos os credores esperados já apareceram no SFTP.")


def main() -> None:
    """Ponto de entrada do comando: identifica arquivos, valida BANCO x DRIVE e notifica.

    Mesmo padrão de execução autônoma do validacao_remessas.main(): pensado
    para rodar em polling (ex.: a cada poucos minutos via Agendador de
    Tarefas). Passos, na ordem:

    1. Se o report de hoje já foi enviado, encerra sem fazer nada
       (`already_sent_today`) — evita duplicidade em reexecuções.
    2. Confere no SFTP quais credores esperados já chegaram (`_checar_chegada_sftp`)
       — só informativo, não bloqueia o restante do processo.
    3. Identifica os arquivos de remessa recebidos no Drive e consulta o
       QTDE_REGISTRO já importado no banco para cada um (`build_batimento_table`),
       validando BANCO x DRIVE por arquivo.
    4. Envia o report ao Chat com o que já tiver chegado, mesmo que falte
       algum credor (`build_batimento_gchat_message`, já mostrando ✅/⚠️ por
       arquivo), e marca como enviado (`mark_sent_today`), para não duplicar
       o envio no mesmo dia.
    """
    connection_string = get_connection_string()
    reference_date = datetime.now().date()

    marker_path = Path(os.environ.get("MEF_BATIMENTO_JCA_MARKER_PATH", r"C:\Temp\batimento_jca_enviado.txt"))
    if already_sent_today(marker_path, reference_date):
        print(f"Report de {reference_date} já foi enviado hoje — nada a fazer.")
        return

    _checar_chegada_sftp(reference_date)

    folder_path = build_drive_folder_path(os.environ["MEF_DRIVE_JCA_PATH"], reference_date)
    drive_files = sorted(p for p in folder_path.rglob("*") if p.is_file()) if folder_path.exists() else []

    qtde_registro_by_arquivo = fetch_qtde_registro_by_arquivo(connection_string)
    table_df = build_batimento_table(drive_files, qtde_registro_by_arquivo)

    with pd.option_context("display.max_columns", None, "display.width", None):
        print(table_df)

    divergentes = table_df[table_df["QTDE_REGISTRO"] != table_df["QTDE_ARQUIVO"]]
    if not divergentes.empty:
        print(f"\n{len(divergentes)} arquivo(s) com divergência (serão sinalizados com ⚠️ no report):")
        with pd.option_context("display.max_columns", None, "display.width", None):
            print(divergentes)

    pendentes_drive = credores_pendentes(table_df)
    if pendentes_drive:
        print(f"Credores ainda sem arquivo sincronizado no Drive: {pendentes_drive} — enviando mesmo assim.")

    title = build_batimento_title(reference_date)
    mention_user_ids = get_batimento_mention_user_ids()
    payload = build_batimento_gchat_message(table_df, title, mention_user_ids)
    send_gchat_notification(os.environ["MEF_GCHAT_WEBHOOK_URL"], payload)
    print("Notificação de batimento enviada ao Google Chat.")

    mark_sent_today(marker_path, reference_date)


def main_com_imagem() -> None:
    """Variante de TESTE do main(): mesmo dedup, mas envia o report como
    CardsV2 com uma imagem, em vez do texto por arquivo
    (build_batimento_gchat_message). Usa um marcador de data separado
    (MEF_BATIMENTO_JCA_IMG_MARKER_PATH) para não interferir no fluxo de texto
    já agendado em produção — as duas variantes podem coexistir sem conflito.

    A URL da imagem tem duas origens possíveis:
    - MEF_BATIMENTO_JCA_IMAGE_URL setada no .env: usa essa URL diretamente,
      sem subir nada pro Drive — pensado pra testar o envio do CardsV2 antes
      de ter a conta de serviço do Drive configurada (você sobe o PNG
      manualmente em algum lugar público e cola o link aqui).
    - Sem essa variável: sobe o PNG pro Drive via upload_image_to_drive,
      exigindo GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON e GOOGLE_DRIVE_FOLDER_ID.
    """
    connection_string = get_connection_string()
    reference_date = datetime.now().date()

    marker_path = Path(
        os.environ.get("MEF_BATIMENTO_JCA_IMG_MARKER_PATH", r"C:\Temp\batimento_jca_imagem_enviado.txt")
    )
    if already_sent_today(marker_path, reference_date):
        print(f"Report (imagem) de {reference_date} já foi enviado hoje — nada a fazer.")
        return

    _checar_chegada_sftp(reference_date)

    folder_path = build_drive_folder_path(os.environ["MEF_DRIVE_JCA_PATH"], reference_date)
    drive_files = sorted(p for p in folder_path.rglob("*") if p.is_file()) if folder_path.exists() else []

    qtde_registro_by_arquivo = fetch_qtde_registro_by_arquivo(connection_string)
    table_df = build_batimento_table(drive_files, qtde_registro_by_arquivo)

    with pd.option_context("display.max_columns", None, "display.width", None):
        print(table_df)

    title = build_batimento_title(reference_date)
    output_png_path = os.environ.get("MEF_BATIMENTO_JCA_PNG_PATH") or r"C:\Temp\BATIMENTO_TIM_JCA.png"
    create_batimento_report_png(table_df, output_png_path, title)

    image_url = os.environ.get("MEF_BATIMENTO_JCA_IMAGE_URL")
    if image_url:
        print(f"Usando URL de imagem fornecida manualmente (sem upload ao Drive): {image_url}")
    else:
        image_url = upload_image_to_drive(
            service_account_json_path=os.environ["GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"],
            folder_id=os.environ["GOOGLE_DRIVE_FOLDER_ID"],
            file_path=output_png_path,
        )
        print(f"Imagem publicada no Drive: {image_url}")

    mention_user_ids = get_batimento_mention_user_ids()
    payload = build_batimento_card_payload(title, image_url, mention_user_ids)
    send_gchat_notification(os.environ["MEF_GCHAT_WEBHOOK_URL"], payload)
    print("Notificação de batimento (CardsV2 com imagem) enviada ao Google Chat.")

    mark_sent_today(marker_path, reference_date)


if __name__ == "__main__":
    import sys

    if "--cardv2" in sys.argv:
        main_com_imagem()
    else:
        main()
