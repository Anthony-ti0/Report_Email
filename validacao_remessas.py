import os
from datetime import date, datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: script only saves PNG, never shows a window

import matplotlib.pyplot as plt
import pandas as pd
import pyodbc

from report_automation import get_connection_string, send_email_report_smtp, send_gchat_notification

DRIVE_BASE_PATH = r"G:\Drives compartilhados\Inteligência de Negócios 5\TIM\REMESSAS"

MESES_PT = {
    1: "JANEIRO",
    2: "FEVEREIRO",
    3: "MARÇO",
    4: "ABRIL",
    5: "MAIO",
    6: "JUNHO",
    7: "JULHO",
    8: "AGOSTO",
    9: "SETEMBRO",
    10: "OUTUBRO",
    11: "NOVEMBRO",
    12: "DEZEMBRO",
}

VALIDATION_QUERY = """
    select NOME_ARQUIVO, DATA_IMPORTACAO, DATA_IMPORTACAO_FINAL
    from tbimportacao
    where DATA_IMPORTACAO >= cast(getdate() as date)
    and DATA_IMPORTACAO < dateadd(day, 1, cast(getdate() as date))
    order by DATA_IMPORTACAO desc
"""

HEADER_TEXT = "MEF_Validação_Recepção_Remessas"


def build_drive_folder_path(base_path: str, reference_date: date) -> Path:
    mes_nome = f"{reference_date.month:02d}.{MESES_PT[reference_date.month]}"
    return Path(base_path) / str(reference_date.year) / mes_nome / f"{reference_date.day:02d}"


def list_drive_files(folder_path: Path) -> list[str]:
    # Arquivos ficam em subpastas por código de contratante (ex.: 4360, 4361,
    # OUTROS), não soltos na pasta do dia — por isso a busca é recursiva.
    if not folder_path.exists():
        return []
    return sorted(p.name for p in folder_path.rglob("*") if p.is_file())


def fetch_validation_data(connection_string: str) -> pd.DataFrame:
    conn = pyodbc.connect(connection_string)
    try:
        df = pd.read_sql(VALIDATION_QUERY, conn)
    finally:
        conn.close()
    return df


def reconcile_files(drive_filenames: list[str], validation_df: pd.DataFrame) -> dict:
    imported_basenames = {
        str(nome).replace("/", "\\").split("\\")[-1].strip().lower()
        for nome in validation_df["NOME_ARQUIVO"]
    }
    importados = [f for f in drive_filenames if f.strip().lower() in imported_basenames]
    pendentes = [f for f in drive_filenames if f.strip().lower() not in imported_basenames]
    return {
        "total_recebidos": len(drive_filenames),
        "total_importados": len(importados),
        "importados": importados,
        "pendentes": pendentes,
    }


def build_validation_table(drive_filenames: list[str], validation_df: pd.DataFrame) -> pd.DataFrame:
    lookup: dict[str, tuple] = {}
    for _, row in validation_df.iterrows():
        basename = str(row["NOME_ARQUIVO"]).replace("/", "\\").split("\\")[-1].strip().lower()
        if basename not in lookup:
            lookup[basename] = (row["DATA_IMPORTACAO"], row["DATA_IMPORTACAO_FINAL"])

    rows = []
    for arquivo in drive_filenames:
        data_processamento, data_termino = lookup.get(arquivo.strip().lower(), (None, None))
        rows.append(
            {"ARQUIVO": arquivo, "DATA_PROCESSAMENTO": data_processamento, "DATA_TERMINO": data_termino}
        )

    return pd.DataFrame(rows, columns=["ARQUIVO", "DATA_PROCESSAMENTO", "DATA_TERMINO"])


def create_validation_report_png(table_df: pd.DataFrame, output_path: str, header_text: str) -> None:
    date_format = "%Y-%m-%d %H:%M:%S"

    def fmt(value) -> str:
        if pd.isna(value):
            return "-"
        return pd.Timestamp(value).strftime(date_format)

    col_labels = ["ARQUIVO", "DATA_PROCESSAMENTO", "DATA_TERMINO"]
    table_data = [
        [row["ARQUIVO"], fmt(row["DATA_PROCESSAMENTO"]), fmt(row["DATA_TERMINO"])]
        for _, row in table_df.iterrows()
    ]
    if not table_data:
        table_data = [["(nenhum arquivo recebido hoje)", "-", "-"]]

    report_datetime = datetime.now().strftime(date_format)
    full_header = f"{header_text}\n{report_datetime}"

    # Proporção 3:4 (retrato); canvas um pouco maior que antes para caber os
    # nomes de arquivo, que são bem mais longos que as colunas de data.
    fig_height = min(1.2 + len(table_data) * 0.28, 9.0)
    fig_width = fig_height * 3 / 4
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=150)
    ax.axis("tight")
    ax.axis("off")
    ax.set_title(full_header, loc="left", fontsize=8, weight="bold")

    table = ax.table(cellText=table_data, colLabels=col_labels, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(5)
    table.scale(1.0, 1.2)

    # ARQUIVO precisa de bem mais espaço que as colunas de data.
    col_widths = {0: 0.56, 1: 0.22, 2: 0.22}
    for (row, col), cell in table.get_celld().items():
        cell.set_width(col_widths[col])
        cell.set_edgecolor("black")
        cell.set_linewidth(0.5)
        if row == 0:
            cell.set_facecolor("#E6E6E6")
            cell.set_text_props(weight="bold", color="black")
        elif table_data[row - 1][2] == "-":
            cell.set_facecolor("#F8D7DA")
            cell.set_text_props(color="black", weight="bold")
        else:
            cell.set_facecolor("#FFFFFF" if row % 2 else "#F3F3F3")
            cell.set_text_props(color="black")

    fig.tight_layout(pad=0.6)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    plt.savefig(output_path, dpi=150)
    plt.close(fig)

    print(f"\nRelatório de validação gerado com sucesso em: {output_path}")


def build_validation_email_subject(report_date: date) -> str:
    return (
        "Comunicado Importante - MEF - Importação de arquivos - "
        f"Tim Telecobrança - {report_date.strftime('%d.%m.%Y')}"
    )


def build_validation_email_html_body(reconciliation: dict) -> str:
    if reconciliation["pendentes"]:
        pendentes_html = "<br>".join(f"• {p}" for p in reconciliation["pendentes"])
        status_html = (
            f"<b>Atenção:</b> {len(reconciliation['pendentes'])} arquivo(s) recebido(s) "
            f"ainda não foram importados:<br>{pendentes_html}"
        )
    else:
        status_html = "Todos os arquivos recebidos foram importados com sucesso."

    return (
        "Bom dia!<br><br>"
        "Prezados, validação de recepção concluída: "
        f"{reconciliation['total_importados']}/{reconciliation['total_recebidos']} "
        "arquivo(s) recebido(s) hoje foram importados, conforme comprova a evidência "
        "das automações abaixo.<br><br>"
        f"{status_html}<br><br>"
        "<img src=\"cid:report_image\">"
    )


def build_validation_gchat_message(reconciliation: dict, mention_user_ids: list[str]) -> dict:
    if reconciliation["pendentes"]:
        lista = "\n".join(f"• {p}" for p in reconciliation["pendentes"])
        corpo = (
            f"⚠️ *{HEADER_TEXT}*: "
            f"{reconciliation['total_importados']}/{reconciliation['total_recebidos']} "
            f"arquivos recebidos hoje foram importados. Pendente(s):\n{lista}"
        )
    else:
        corpo = (
            f"✅ *{HEADER_TEXT}*: validação concluída — "
            f"{reconciliation['total_importados']}/{reconciliation['total_recebidos']} "
            "arquivos recebidos hoje foram importados com sucesso."
        )

    mencoes = " ".join(f"<users/{user_id}>" for user_id in mention_user_ids)
    return {"text": f"{corpo}\n\n{mencoes}"}


def already_sent_today(marker_path: Path, reference_date: date) -> bool:
    if not marker_path.exists():
        return False
    return marker_path.read_text().strip() == reference_date.isoformat()


def mark_sent_today(marker_path: Path, reference_date: date) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(reference_date.isoformat())


def main(force: bool = False) -> None:
    connection_string = get_connection_string()
    reference_date = datetime.now().date()

    marker_path = Path(os.environ.get("MEF_VALIDATION_MARKER_PATH", r"C:\Temp\validacao_enviado.txt"))
    if already_sent_today(marker_path, reference_date):
        print(f"Relatório de {reference_date} já foi enviado hoje — nada a fazer.")
        return

    folder_path = build_drive_folder_path(
        os.environ.get("MEF_DRIVE_REMESSAS_PATH", DRIVE_BASE_PATH), reference_date
    )
    drive_files = list_drive_files(folder_path)

    validation_df = fetch_validation_data(connection_string)
    reconciliation = reconcile_files(drive_files, validation_df)

    print(f"Pasta verificada: {folder_path}")
    print(f"Recebidos: {reconciliation['total_recebidos']}")
    print(f"Importados: {reconciliation['total_importados']}")
    print(f"Pendentes: {reconciliation['pendentes']}")

    concluido = reconciliation["total_recebidos"] > 0 and not reconciliation["pendentes"]
    if not concluido and not force:
        print("Importação ainda não concluída (ou nada recebido ainda) — aguardando próxima checagem.")
        return

    mention_user_ids = [
        user_id.strip()
        for user_id in os.environ["MEF_GCHAT_MENTION_USER_IDS"].split(",")
        if user_id.strip()
    ]

    if not concluido:
        # Fim da janela e ainda incompleto: só avisa no Chat, sem gerar e-mail
        # (o e-mail é o relatório oficial e só deve sair uma vez, quando fechado).
        payload = build_validation_gchat_message(reconciliation, mention_user_ids)
        send_gchat_notification(os.environ["MEF_GCHAT_WEBHOOK_URL"], payload)
        print("Importação incompleta — alerta enviado ao Google Chat (sem e-mail).")
        return

    output_png_path = os.environ.get("MEF_VALIDATION_PNG_PATH", r"C:\Temp\VALIDACAO_REMESSA_TIM.png")
    table_df = build_validation_table(drive_files, validation_df)
    create_validation_report_png(table_df, output_png_path, HEADER_TEXT)

    send_email_report_smtp(
        smtp_host=os.environ.get("MEF_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.environ.get("MEF_SMTP_PORT", "587")),
        username=os.environ["MEF_SMTP_USER"],
        password=os.environ["MEF_SMTP_PASSWORD"],
        to_address=os.environ["MEF_VALIDATION_EMAIL_TO"],
        subject=build_validation_email_subject(reference_date),
        html_body=build_validation_email_html_body(reconciliation),
        image_path=output_png_path,
    )
    print("E-mail de validação enviado.")

    payload = build_validation_gchat_message(reconciliation, mention_user_ids)
    send_gchat_notification(os.environ["MEF_GCHAT_WEBHOOK_URL"], payload)
    print("Notificação de validação enviada ao Google Chat.")

    mark_sent_today(marker_path, reference_date)


if __name__ == "__main__":
    import sys

    main(force="--force" in sys.argv)
