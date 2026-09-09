from datetime import date

import pandas as pd

import batimento_tim_jca as bj


class TestBuildBatimentoTitle:
    def test_formats_title_with_date(self):
        assert bj.build_batimento_title(date(2026, 9, 8)) == "↘️ BATIMENTO TIM JCA | 08/09/2026"


class TestExtractCredor:
    def test_finds_known_credor_at_end_of_filename(self):
        assert bj.extract_credor("REMESSA_CYBER_ASSESSORIA_070920262920_012_6202") == "6202"

    def test_finds_known_credor_before_extension(self):
        assert bj.extract_credor("REMESSA_CYBER_ASSESSORIA_070920262927_005_4360.txt") == "4360"

    def test_returns_none_for_unknown_code(self):
        assert bj.extract_credor("REMESSA_CYBER_ASSESSORIA_070920262927_005_9999") is None

    def test_returns_none_when_no_trailing_digits(self):
        assert bj.extract_credor("REMESSA_CYBER_ASSESSORIA_SEM_CODIGO") is None


class TestIsRemessaFile:
    def test_true_for_remessa_with_known_credor(self):
        assert bj.is_remessa_file("REMESSA_CYBER_ASSESSORIA_X_012_6202") is True

    def test_false_for_non_remessa_file_with_matching_credor_suffix(self):
        """EXC_BAI (exclusão/baixa) não é remessa, mesmo terminando num credor conhecido."""
        assert bj.is_remessa_file("EXC_BAI_CYBER_ASSESSORIA_07092026222920_010_6201.txt") is False

    def test_false_for_remessa_without_known_credor(self):
        assert bj.is_remessa_file("REMESSA_CYBER_ASSESSORIA_SEM_CODIGO") is False


class TestCountFileLines:
    def test_counts_lines_in_file(self, tmp_path):
        file_path = tmp_path / "arquivo.txt"
        file_path.write_text("linha1\nlinha2\nlinha3\n", encoding="latin-1")
        assert bj.count_file_lines(file_path) == 3

    def test_counts_zero_for_empty_file(self, tmp_path):
        file_path = tmp_path / "vazio.txt"
        file_path.write_text("", encoding="latin-1")
        assert bj.count_file_lines(file_path) == 0


class TestBuildBatimentoTable:
    def test_builds_table_with_matching_and_diverging_rows(self, tmp_path):
        arquivo_ok = "REMESSA_CYBER_ASSESSORIA_X_012_6202"
        arquivo_divergente = "REMESSA_CYBER_ASSESSORIA_X_005_4360"
        path_ok = tmp_path / arquivo_ok
        path_divergente = tmp_path / arquivo_divergente
        path_ok.write_text("a\nb\nc\n", encoding="latin-1")
        path_divergente.write_text("a\nb\n", encoding="latin-1")

        df = bj.build_batimento_table(
            drive_files=[path_ok, path_divergente],
            qtde_registro_by_arquivo={arquivo_ok.lower(): 3, arquivo_divergente.lower(): 5},
        )

        assert list(df["REGISTRO"]) == [1, 2]
        assert list(df["QTDE_REGISTRO"]) == [3, 5]
        assert list(df["QTDE_ARQUIVO"]) == [3, 2]

    def test_reads_files_nested_in_subfolders(self, tmp_path):
        """Os arquivos ficam em subpastas por credor (ex.: 6201/, 4360/), não soltos na pasta do dia."""
        arquivo = "REMESSA_X_010_6201"
        subpasta = tmp_path / "6201"
        subpasta.mkdir()
        (subpasta / arquivo).write_text("a\nb\nc\n", encoding="latin-1")

        df = bj.build_batimento_table(
            drive_files=[subpasta / arquivo],
            qtde_registro_by_arquivo={arquivo.lower(): 3},
        )

        assert df.loc[0, "ARQUIVO"] == arquivo
        assert df.loc[0, "QTDE_ARQUIVO"] == 3

    def test_ignores_files_without_known_credor(self, tmp_path):
        path = tmp_path / "ARQUIVO_SEM_CREDOR"
        path.write_text("a\n", encoding="latin-1")

        df = bj.build_batimento_table(
            drive_files=[path],
            qtde_registro_by_arquivo={},
        )

        assert df.empty

    def test_ignores_non_remessa_files_even_with_matching_credor_suffix(self, tmp_path):
        path = tmp_path / "EXC_BAI_CYBER_ASSESSORIA_07092026222920_010_6201.txt"
        path.write_text("a\nb\nc\n", encoding="latin-1")

        df = bj.build_batimento_table(
            drive_files=[path],
            qtde_registro_by_arquivo={},
        )

        assert df.empty

    def test_defaults_missing_db_count_to_zero(self, tmp_path):
        arquivo = "REMESSA_X_007_5260"
        path = tmp_path / arquivo
        path.write_text("a\nb\n", encoding="latin-1")

        df = bj.build_batimento_table(
            drive_files=[path],
            qtde_registro_by_arquivo={},
        )

        assert df.loc[0, "QTDE_REGISTRO"] == 0
        assert df.loc[0, "QTDE_ARQUIVO"] == 2


class TestGetBatimentoMentionUserIds:
    def test_parses_comma_separated_ids(self, monkeypatch):
        monkeypatch.setenv("MEF_GCHAT_MENTION_USER_IDS_BATIMENTO_JCA", "111, 222,333")
        assert bj.get_batimento_mention_user_ids() == ["111", "222", "333"]


class TestBuildBatimentoGchatMessage:
    def test_marks_matching_row_with_check_and_diverging_with_warning(self):
        df = pd.DataFrame(
            [
                {"REGISTRO": 1, "ARQUIVO": "REMESSA_A_6201", "QTDE_REGISTRO": 10, "QTDE_ARQUIVO": 10},
                {"REGISTRO": 2, "ARQUIVO": "REMESSA_B_4360", "QTDE_REGISTRO": 5, "QTDE_ARQUIVO": 7},
            ]
        )
        payload = bj.build_batimento_gchat_message(df, title="TITULO", mention_user_ids=["111"])
        texto = payload["text"]

        assert "*✅REMESSA_A_6201*\n- 🗂️ Registro_Base: *10*\n- 📄 Registro_Arquivo: *10*" in texto
        assert "*⚠️REMESSA_B_4360*\n- 🗂️ Registro_Base: *5*\n- 📄 Registro_Arquivo: *7*" in texto
        assert "<users/111>" in texto

    def test_empty_table_still_includes_title_and_mentions(self):
        df = pd.DataFrame(columns=["REGISTRO", "ARQUIVO", "QTDE_REGISTRO", "QTDE_ARQUIVO"])
        payload = bj.build_batimento_gchat_message(df, title="TITULO", mention_user_ids=["111"])

        assert "TITULO" in payload["text"]
        assert "Nenhum arquivo de remessa encontrado hoje." in payload["text"]
        assert "<users/111>" in payload["text"]
