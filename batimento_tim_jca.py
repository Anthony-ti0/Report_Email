import os
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyodbc

from report_automation import get_connection_string, send_gchat_notification
from validacao_remessas import build_drive_folder_path

CREDORES = ["5260", "8660", "6201", "6202", "4360"]

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

    Ex.: REMESSA_CYBER_ASSESSORIA_070920262920_012_6202 -> "6202".
    Retorna None se nenhum código conhecido (CREDORES) for encontrado.
    """
    match = re.search(r"(\d{4})(?:\.[^.]+)?$", filename)
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


def main(force: bool = False) -> None:
    """Ponto de entrada: consulta o banco, lê os arquivos do Drive e envia o resultado ao Chat."""
    connection_string = get_connection_string()
    reference_date = datetime.now().date()

    folder_path = build_drive_folder_path(os.environ["MEF_DRIVE_JCA_PATH"], reference_date)
    drive_files = sorted(p for p in folder_path.rglob("*") if p.is_file()) if folder_path.exists() else []

    qtde_registro_by_arquivo = fetch_qtde_registro_by_arquivo(connection_string)
    table_df = build_batimento_table(drive_files, qtde_registro_by_arquivo)

    with pd.option_context("display.max_columns", None, "display.width", None):
        print(table_df)

    divergentes = table_df[table_df["QTDE_REGISTRO"] != table_df["QTDE_ARQUIVO"]]
    if not divergentes.empty and not force:
        print(f"\n{len(divergentes)} arquivo(s) com divergência:")
        with pd.option_context("display.max_columns", None, "display.width", None):
            print(divergentes)
        print("\nRevisar antes de enviar (use --force para enviar mesmo assim).")
        return

    title = build_batimento_title(reference_date)
    mention_user_ids = get_batimento_mention_user_ids()
    payload = build_batimento_gchat_message(table_df, title, mention_user_ids)
    send_gchat_notification(os.environ["MEF_GCHAT_WEBHOOK_URL"], payload)
    print("Notificação de batimento enviada ao Google Chat.")


if __name__ == "__main__":
    import sys

    main(force="--force" in sys.argv)
