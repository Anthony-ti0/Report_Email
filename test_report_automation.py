from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

import report_automation as ra


def make_df(rows):
    df = pd.DataFrame(rows, columns=["arquivo", "data_processamento", "data_termino"])
    df["data_processamento"] = pd.to_datetime(df["data_processamento"])
    df["data_termino"] = pd.to_datetime(df["data_termino"])
    return df


class TestFilterTodayUnique:
    def test_excludes_csp_imp_and_tim(self):
        df = make_df([
            ("REMESSA_CSP_IMP_001.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("REMESSA_TIM_002.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("REMESSA_CYBER_ASSESSORIA_003.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
        ])

        result = ra.filter_today_unique(df, date(2026, 7, 24))

        assert list(result["arquivo"]) == ["REMESSA_CYBER_ASSESSORIA_003.txt"]

    def test_filters_only_reference_date(self):
        df = make_df([
            ("A.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("B.txt", "2026-07-23 01:00:00", "2026-07-23 01:00:10"),
        ])

        result = ra.filter_today_unique(df, date(2026, 7, 24))

        assert list(result["arquivo"]) == ["A.txt"]

    def test_deduplicates_keeping_first_by_finish_time(self):
        df = make_df([
            ("A.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("A.txt", "2026-07-24 02:00:00", "2026-07-24 02:00:10"),
        ])

        result = ra.filter_today_unique(df, date(2026, 7, 24))

        assert len(result) == 1
        assert result.iloc[0]["data_termino"] == pd.Timestamp("2026-07-24 01:00:10")

    def test_sorted_descending_by_finish_time(self):
        df = make_df([
            ("A.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("B.txt", "2026-07-24 03:00:00", "2026-07-24 03:00:10"),
            ("C.txt", "2026-07-24 02:00:00", "2026-07-24 02:00:10"),
        ])

        result = ra.filter_today_unique(df, date(2026, 7, 24))

        assert list(result["arquivo"]) == ["B.txt", "C.txt", "A.txt"]

    def test_started_today_but_unfinished_is_excluded(self):
        df = make_df([
            ("A.txt", "2026-07-24 23:59:00", "2026-07-24 23:59:59"),
            ("B.txt", "2026-07-24 23:59:00", None),
        ])

        result = ra.filter_today_unique(df, date(2026, 7, 24))

        assert list(result["arquivo"]) == ["A.txt"]

    def test_finished_next_day_is_not_reported_as_today(self):
        # Começa perto da meia-noite de um dia e só termina no dia seguinte:
        # deve contar para o dia em que TERMINOU, não em que começou.
        df = make_df([
            ("A.txt", "2026-07-24 23:59:00", "2026-07-25 00:00:05"),
        ])

        result_dia_24 = ra.filter_today_unique(df, date(2026, 7, 24))
        result_dia_25 = ra.filter_today_unique(df, date(2026, 7, 25))

        assert result_dia_24.empty
        assert list(result_dia_25["arquivo"]) == ["A.txt"]

    def test_empty_input_returns_empty(self):
        df = make_df([])
        result = ra.filter_today_unique(df, date(2026, 7, 24))
        assert result.empty

    def test_case_insensitive_exclusion(self):
        df = make_df([
            ("remessa_csp_imp_lower.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("remessa_tim_lower.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
        ])

        result = ra.filter_today_unique(df, date(2026, 7, 24))

        assert result.empty


class TestFetchImportData:
    @patch("report_automation.pd.read_sql")
    @patch("report_automation.pyodbc.connect")
    def test_closes_connection_and_parses_dates(self, mock_connect, mock_read_sql):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_read_sql.return_value = pd.DataFrame({
            "arquivo": ["A.txt"],
            "data_processamento": ["2026-07-24 01:00:00"],
            "data_termino": ["2026-07-24 01:00:10"],
        })

        df = ra.fetch_import_data("fake-connection-string")

        mock_connect.assert_called_once_with("fake-connection-string")
        mock_conn.close.assert_called_once()
        assert pd.api.types.is_datetime64_any_dtype(df["data_processamento"])
        assert pd.api.types.is_datetime64_any_dtype(df["data_termino"])

    @patch("report_automation.pd.read_sql", side_effect=RuntimeError("boom"))
    @patch("report_automation.pyodbc.connect")
    def test_closes_connection_even_on_query_failure(self, mock_connect, mock_read_sql):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        with pytest.raises(RuntimeError):
            ra.fetch_import_data("fake-connection-string")

        mock_conn.close.assert_called_once()


class TestCreateReportPng:
    def test_generates_png_file(self, tmp_path):
        df = make_df([
            ("A.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("B.txt", "2026-07-24 02:00:00", "2026-07-24 02:00:10"),
        ])
        output_path = tmp_path / "report.png"

        ra.create_report_png(df, str(output_path), "Header de Teste")

        assert output_path.exists()
        assert output_path.stat().st_size > 0

    def test_handles_empty_dataframe(self, tmp_path):
        df = make_df([])
        output_path = tmp_path / "empty_report.png"

        ra.create_report_png(df, str(output_path), "Header Vazio")

        assert output_path.exists()

    def test_creates_missing_output_directory(self, tmp_path):
        df = make_df([("A.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10")])
        output_path = tmp_path / "nested" / "dir" / "report.png"

        ra.create_report_png(df, str(output_path), "Header")

        assert output_path.exists()


class TestDebugFilterPipeline:
    def test_reports_counts_at_each_stage(self):
        df = make_df([
            ("A_CSP_IMP_1.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("B_TIM_1.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("C_YESTERDAY.txt", "2026-07-23 01:00:00", "2026-07-23 01:00:10"),
            ("D_OK.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("D_OK.txt", "2026-07-24 02:00:00", "2026-07-24 02:00:10"),
        ])

        diag = ra.debug_filter_pipeline(df, date(2026, 7, 24))

        assert diag["total_no_banco"] == 5
        assert diag["removidos_csp_imp"] == ["A_CSP_IMP_1.txt"]
        assert diag["removidos_tim"] == ["B_TIM_1.txt"]
        assert diag["removidos_por_data"] == ["C_YESTERDAY.txt"]
        assert diag["removidos_por_duplicata"] == ["D_OK.txt"]
        assert diag["apos_dedup"] == 1

    def test_late_night_batch_falls_outside_reference_date(self):
        # Reproduz o caso relatado: arquivos importados perto da virada do dia
        # (23:54) ficam de fora se reference_date for o dia seguinte.
        df = make_df([
            ("REMESSA_CYBER_ASSESSORIA_007.txt", "2026-09-02 23:54:12", "2026-09-02 23:54:13"),
            ("EXC_RED_CYBER_ASSESSORIA_007.txt", "2026-09-02 23:54:11", "2026-09-02 23:54:12"),
        ])

        diag = ra.debug_filter_pipeline(df, date(2026, 9, 3))

        assert diag["apos_filtro_data"] == 0
        assert len(diag["removidos_por_data"]) == 2


class TestBuildGchatMessage:
    def test_lists_all_files_and_mentions_user(self):
        df = make_df([
            ("A.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10"),
            ("B.txt", "2026-07-24 02:00:00", "2026-07-24 02:00:10"),
        ])

        payload = ra.build_gchat_message(df, "103237789261244417964")

        assert "text" in payload
        assert "A.txt" in payload["text"]
        assert "B.txt" in payload["text"]
        assert "2 arquivo(s)" in payload["text"]
        assert "<users/103237789261244417964>" in payload["text"]

    def test_empty_dataframe_still_mentions_user(self):
        df = make_df([])

        payload = ra.build_gchat_message(df, "12345")

        assert "Nenhum arquivo processado hoje" in payload["text"]
        assert "<users/12345>" in payload["text"]

    def test_mentions_multiple_users(self):
        df = make_df([("A.txt", "2026-07-24 01:00:00", "2026-07-24 01:00:10")])

        payload = ra.build_gchat_message(df, ["111", "222", "333"])

        assert "<users/111>" in payload["text"]
        assert "<users/222>" in payload["text"]
        assert "<users/333>" in payload["text"]


class TestSendGchatNotification:
    @patch("report_automation.requests.post")
    def test_posts_payload_to_webhook(self, mock_post):
        mock_response = MagicMock()
        mock_post.return_value = mock_response

        payload = {"text": "hello"}
        result = ra.send_gchat_notification("https://chat.googleapis.com/fake", payload)

        mock_post.assert_called_once_with("https://chat.googleapis.com/fake", json=payload, timeout=10)
        mock_response.raise_for_status.assert_called_once()
        assert result is mock_response

    @patch("report_automation.requests.post")
    def test_raises_on_http_error(self, mock_post):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("boom")
        mock_post.return_value = mock_response

        with pytest.raises(requests.HTTPError):
            ra.send_gchat_notification("https://chat.googleapis.com/fake", {"text": "hello"})


class TestSendEmailReportSmtp:
    @patch("report_automation.smtplib.SMTP")
    def test_sends_via_starttls_with_embedded_image(self, mock_smtp_cls, tmp_path):
        image_path = tmp_path / "report.png"
        png_signature = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        image_path.write_bytes(png_signature)

        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        ra.send_email_report_smtp(
            smtp_host="smtp.office365.com",
            smtp_port=587,
            username="gabrielanthony@meirelesefreitas.com.br",
            password="fake-password",
            to_address="someone@example.com",
            subject="Assunto",
            html_body="<p>corpo</p>",
            image_path=str(image_path),
        )

        mock_smtp_cls.assert_called_once_with("smtp.office365.com", 587, timeout=30)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with(
            "gabrielanthony@meirelesefreitas.com.br", "fake-password"
        )
        mock_server.send_message.assert_called_once()
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["To"] == "someone@example.com"
        assert sent_msg["Subject"] == "Assunto"


class TestGetConnectionString:
    def test_builds_string_from_env(self, monkeypatch):
        monkeypatch.setenv("MEF_DB_SERVER", "10.10.222.10")
        monkeypatch.setenv("MEF_DB_NAME", "dbActyon_TIM")
        monkeypatch.setenv("MEF_DB_UID", "user")
        monkeypatch.setenv("MEF_DB_PWD", "secret")

        conn_str = ra.get_connection_string()

        assert "SERVER=10.10.222.10,1433;" in conn_str
        assert "DATABASE=dbActyon_TIM;" in conn_str
        assert "UID=user;" in conn_str
        assert "PWD=secret;" in conn_str

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("MEF_DB_SERVER", raising=False)
        with pytest.raises(KeyError):
            ra.get_connection_string()
