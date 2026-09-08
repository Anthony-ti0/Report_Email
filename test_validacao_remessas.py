from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd

import validacao_remessas as vr


class TestAlreadySentToday:
    def test_false_when_marker_missing(self, tmp_path):
        marker = tmp_path / "marker.txt"
        assert vr.already_sent_today(marker, date(2026, 9, 4)) is False

    def test_true_when_marker_matches_date(self, tmp_path):
        marker = tmp_path / "marker.txt"
        marker.write_text("2026-09-04")
        assert vr.already_sent_today(marker, date(2026, 9, 4)) is True

    def test_false_when_marker_is_a_different_date(self, tmp_path):
        marker = tmp_path / "marker.txt"
        marker.write_text("2026-09-03")
        assert vr.already_sent_today(marker, date(2026, 9, 4)) is False


class TestMarkSentToday:
    def test_writes_date_and_creates_parent_dirs(self, tmp_path):
        marker = tmp_path / "nested" / "marker.txt"
        vr.mark_sent_today(marker, date(2026, 9, 4))
        assert marker.read_text().strip() == "2026-09-04"


class TestBuildDriveFolderPath:
    def test_builds_year_month_day_structure(self):
        path = vr.build_drive_folder_path(r"G:\Base\REMESSAS", date(2026, 9, 4))
        assert str(path) == r"G:\Base\REMESSAS\2026\09.SETEMBRO\04"

    def test_pads_single_digit_day(self):
        path = vr.build_drive_folder_path(r"G:\Base\REMESSAS", date(2026, 1, 5))
        assert str(path) == r"G:\Base\REMESSAS\2026\01.JANEIRO\05"


class TestListDriveFiles:
    def test_lists_loose_files_in_the_day_folder(self, tmp_path):
        folder = tmp_path / "04"
        folder.mkdir()
        (folder / "A.txt").write_text("x")
        (folder / "B.txt").write_text("x")

        result = vr.list_drive_files(folder)

        assert result == ["A.txt", "B.txt"]

    def test_lists_files_nested_in_contratante_subfolders(self, tmp_path):
        # Estrutura real: pasta do dia > subpasta por código de contratante > arquivos
        folder = tmp_path / "04"
        (folder / "4360").mkdir(parents=True)
        (folder / "4361").mkdir(parents=True)
        (folder / "OUTROS").mkdir(parents=True)
        (folder / "4360" / "REMESSA_A.txt").write_text("x")
        (folder / "4361" / "REMESSA_B.txt").write_text("x")
        (folder / "OUTROS" / "ENRIQUECIMENTO.csv").write_text("x")

        result = vr.list_drive_files(folder)

        assert result == ["ENRIQUECIMENTO.csv", "REMESSA_A.txt", "REMESSA_B.txt"]

    def test_missing_folder_returns_empty_list(self, tmp_path):
        result = vr.list_drive_files(tmp_path / "nao_existe")
        assert result == []


class TestReconcileFiles:
    def test_all_received_files_imported(self):
        drive_files = ["A.txt", "B.txt"]
        validation_df = pd.DataFrame({"NOME_ARQUIVO": [r"C:\import\A.txt", r"C:\import\B.txt"]})

        result = vr.reconcile_files(drive_files, validation_df)

        assert result["total_recebidos"] == 2
        assert result["total_importados"] == 2
        assert result["pendentes"] == []

    def test_detects_pending_files(self):
        drive_files = ["A.txt", "B.txt", "C.txt"]
        validation_df = pd.DataFrame({"NOME_ARQUIVO": [r"C:\import\A.txt"]})

        result = vr.reconcile_files(drive_files, validation_df)

        assert result["total_recebidos"] == 3
        assert result["total_importados"] == 1
        assert result["pendentes"] == ["B.txt", "C.txt"]

    def test_matches_case_insensitively(self):
        drive_files = ["remessa_A.TXT"]
        validation_df = pd.DataFrame({"NOME_ARQUIVO": [r"C:\import\REMESSA_a.txt"]})

        result = vr.reconcile_files(drive_files, validation_df)

        assert result["pendentes"] == []
        assert result["importados"] == ["remessa_A.TXT"]

    def test_matches_basename_ignoring_full_db_path(self):
        drive_files = ["REMESSA_CYBER_ASSESSORIA_001.txt"]
        validation_df = pd.DataFrame({
            "NOME_ARQUIVO": [
                r"D:\importador\entrada\lote1\REMESSA_CYBER_ASSESSORIA_001.txt"
            ]
        })

        result = vr.reconcile_files(drive_files, validation_df)

        assert result["pendentes"] == []

    def test_empty_drive_folder(self):
        result = vr.reconcile_files([], pd.DataFrame({"NOME_ARQUIVO": []}))
        assert result["total_recebidos"] == 0
        assert result["pendentes"] == []


class TestFetchValidationData:
    @patch("validacao_remessas.pd.read_sql")
    @patch("validacao_remessas.pyodbc.connect")
    def test_closes_connection(self, mock_connect, mock_read_sql):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_read_sql.return_value = pd.DataFrame({"NOME_ARQUIVO": ["A.txt"]})

        df = vr.fetch_validation_data("fake-connection-string")

        mock_connect.assert_called_once_with("fake-connection-string")
        mock_conn.close.assert_called_once()
        assert list(df["NOME_ARQUIVO"]) == ["A.txt"]


class TestBuildValidationTable:
    def test_matches_dates_by_basename(self):
        drive_files = ["A.txt", "B.txt"]
        validation_df = pd.DataFrame({
            "NOME_ARQUIVO": [r"C:\import\A.txt"],
            "DATA_IMPORTACAO": [pd.Timestamp("2026-09-04 03:00:00")],
            "DATA_IMPORTACAO_FINAL": [pd.Timestamp("2026-09-04 03:00:10")],
        })

        table = vr.build_validation_table(drive_files, validation_df)

        assert list(table["ARQUIVO"]) == ["A.txt", "B.txt"]
        assert table.iloc[0]["DATA_TERMINO"] == pd.Timestamp("2026-09-04 03:00:10")
        assert pd.isna(table.iloc[1]["DATA_PROCESSAMENTO"])
        assert pd.isna(table.iloc[1]["DATA_TERMINO"])

    def test_empty_drive_files_returns_empty_table(self):
        table = vr.build_validation_table([], pd.DataFrame({"NOME_ARQUIVO": [], "DATA_IMPORTACAO": [], "DATA_IMPORTACAO_FINAL": []}))
        assert table.empty
        assert list(table.columns) == ["ARQUIVO", "DATA_PROCESSAMENTO", "DATA_TERMINO"]


class TestCreateValidationReportPng:
    def test_generates_png_with_pending_files(self, tmp_path):
        table_df = pd.DataFrame([
            {
                "ARQUIVO": "A.txt",
                "DATA_PROCESSAMENTO": pd.Timestamp("2026-09-04 03:00:00"),
                "DATA_TERMINO": pd.Timestamp("2026-09-04 03:00:10"),
            },
            {"ARQUIVO": "B.txt", "DATA_PROCESSAMENTO": pd.NaT, "DATA_TERMINO": pd.NaT},
        ])
        output_path = tmp_path / "validacao.png"

        vr.create_validation_report_png(table_df, str(output_path), "Header")

        assert output_path.exists()
        assert output_path.stat().st_size > 0

    def test_generates_png_when_nothing_received(self, tmp_path):
        table_df = pd.DataFrame(columns=["ARQUIVO", "DATA_PROCESSAMENTO", "DATA_TERMINO"])
        output_path = tmp_path / "validacao_vazia.png"

        vr.create_validation_report_png(table_df, str(output_path), "Header")

        assert output_path.exists()

    def test_output_is_3x4_aspect_ratio(self, tmp_path):
        from PIL import Image

        table_df = pd.DataFrame([
            {
                "ARQUIVO": "A.txt",
                "DATA_PROCESSAMENTO": pd.Timestamp("2026-09-04 03:00:00"),
                "DATA_TERMINO": pd.Timestamp("2026-09-04 03:00:10"),
            }
        ])
        output_path = tmp_path / "validacao_ratio.png"

        vr.create_validation_report_png(table_df, str(output_path), "Header")

        with Image.open(output_path) as img:
            width, height = img.size
        assert abs((width / height) - (3 / 4)) < 0.05


class TestBuildValidationEmailSubject:
    def test_formats_date(self):
        subject = vr.build_validation_email_subject(date(2026, 9, 4))
        assert subject == (
            "Comunicado Importante - MEF - Importação de arquivos - "
            "Tim Telecobrança - 04.09.2026"
        )


class TestBuildValidationEmailHtmlBody:
    def test_flags_pending_files(self):
        reconciliation = {
            "total_recebidos": 2,
            "total_importados": 1,
            "importados": ["A.txt"],
            "pendentes": ["B.txt"],
        }
        body = vr.build_validation_email_html_body(reconciliation)
        assert "Atenção" in body
        assert "B.txt" in body
        assert "1/2" in body

    def test_all_imported_has_no_warning(self):
        reconciliation = {
            "total_recebidos": 2,
            "total_importados": 2,
            "importados": ["A.txt", "B.txt"],
            "pendentes": [],
        }
        body = vr.build_validation_email_html_body(reconciliation)
        assert "Atenção" not in body
        assert "Todos os arquivos recebidos foram importados" in body


class TestBuildValidationGchatMessage:
    def test_warns_about_pending_files(self):
        reconciliation = {
            "total_recebidos": 3,
            "total_importados": 2,
            "importados": ["A.txt", "B.txt"],
            "pendentes": ["C.txt"],
        }
        payload = vr.build_validation_gchat_message(reconciliation, ["111"])
        assert "C.txt" in payload["text"]
        assert "2/3" in payload["text"]
        assert "<users/111>" in payload["text"]

    def test_all_imported_success_message(self):
        reconciliation = {
            "total_recebidos": 2,
            "total_importados": 2,
            "importados": ["A.txt", "B.txt"],
            "pendentes": [],
        }
        payload = vr.build_validation_gchat_message(reconciliation, ["111", "222"])
        assert "validação concluída" in payload["text"]
        assert "<users/111>" in payload["text"]
        assert "<users/222>" in payload["text"]


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEF_GCHAT_MENTION_USER_IDS", "111")
    monkeypatch.setenv("MEF_SMTP_USER", "user@example.com")
    monkeypatch.setenv("MEF_SMTP_PASSWORD", "pwd")
    monkeypatch.setenv("MEF_VALIDATION_EMAIL_TO", "dest@example.com")
    monkeypatch.setenv("MEF_GCHAT_WEBHOOK_URL", "https://example.com/webhook")
    marker_path = tmp_path / "marker.txt"
    monkeypatch.setenv("MEF_VALIDATION_MARKER_PATH", str(marker_path))
    return marker_path


def _fake_validation_df():
    return pd.DataFrame({
        "NOME_ARQUIVO": ["A.txt"],
        "DATA_IMPORTACAO": [pd.Timestamp.now()],
        "DATA_IMPORTACAO_FINAL": [pd.Timestamp.now()],
    })


class TestMainGating:
    def test_skips_everything_when_already_sent_today(self, tmp_path, monkeypatch):
        marker_path = _setup_env(monkeypatch, tmp_path)
        marker_path.write_text(datetime.now().date().isoformat())

        with patch.object(vr, "fetch_validation_data") as mock_fetch:
            vr.main()

        mock_fetch.assert_not_called()

    def test_does_not_send_when_incomplete_and_not_forced(self, tmp_path, monkeypatch):
        marker_path = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setattr(vr, "get_connection_string", lambda: "fake-conn")
        monkeypatch.setattr(vr, "list_drive_files", lambda folder: ["A.txt", "B.txt"])
        monkeypatch.setattr(vr, "fetch_validation_data", lambda conn: _fake_validation_df())

        with patch.object(vr, "send_email_report_smtp") as mock_email, \
             patch.object(vr, "send_gchat_notification") as mock_chat:
            vr.main()

        mock_email.assert_not_called()
        mock_chat.assert_not_called()
        assert not marker_path.exists()

    def test_sends_when_complete(self, tmp_path, monkeypatch):
        marker_path = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setattr(vr, "get_connection_string", lambda: "fake-conn")
        monkeypatch.setattr(vr, "list_drive_files", lambda folder: ["A.txt"])
        monkeypatch.setattr(vr, "fetch_validation_data", lambda conn: _fake_validation_df())

        with patch.object(vr, "create_validation_report_png"), \
             patch.object(vr, "send_email_report_smtp") as mock_email, \
             patch.object(vr, "send_gchat_notification") as mock_chat:
            vr.main()

        mock_email.assert_called_once()
        mock_chat.assert_called_once()
        assert marker_path.read_text().strip() == datetime.now().date().isoformat()

    def test_force_alerts_via_chat_only_when_incomplete(self, tmp_path, monkeypatch):
        marker_path = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setattr(vr, "get_connection_string", lambda: "fake-conn")
        monkeypatch.setattr(vr, "list_drive_files", lambda folder: ["A.txt", "B.txt"])
        monkeypatch.setattr(vr, "fetch_validation_data", lambda conn: _fake_validation_df())

        with patch.object(vr, "create_validation_report_png"), \
             patch.object(vr, "send_email_report_smtp") as mock_email, \
             patch.object(vr, "send_gchat_notification") as mock_chat:
            vr.main(force=True)

        mock_email.assert_not_called()
        mock_chat.assert_called_once()
        assert not marker_path.exists()

    def test_force_sends_email_and_chat_when_actually_complete(self, tmp_path, monkeypatch):
        marker_path = _setup_env(monkeypatch, tmp_path)
        monkeypatch.setattr(vr, "get_connection_string", lambda: "fake-conn")
        monkeypatch.setattr(vr, "list_drive_files", lambda folder: ["A.txt"])
        monkeypatch.setattr(vr, "fetch_validation_data", lambda conn: _fake_validation_df())

        with patch.object(vr, "create_validation_report_png"), \
             patch.object(vr, "send_email_report_smtp") as mock_email, \
             patch.object(vr, "send_gchat_notification") as mock_chat:
            vr.main(force=True)

        mock_email.assert_called_once()
        mock_chat.assert_called_once()
        assert marker_path.exists()
