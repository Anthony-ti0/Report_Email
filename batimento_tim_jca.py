import os
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import paramiko
import pyodbc

from report_automation import get_connection_string, send_gchat_notification
from validacao_remessas import already_sent_today, build_drive_folder_path, mark_sent_today

CREDORES = ["5260", "8660", "6201", "6202", "4360", "4361"]

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


if __name__ == "__main__":
    main()
