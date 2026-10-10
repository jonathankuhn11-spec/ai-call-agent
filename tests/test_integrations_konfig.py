"""Konfiguration: Umgebung vor .env, Kommentare, fehlende Geheimnisse vor dem Go-live."""
import os

from integrations import konfig


def test_env_file_parsing_and_precedence(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('TELLI_API_KEY="aus-datei"\nTOOL_SECRET=geheim # Kommentar\n# nur Kommentar\n'
                                   'KUNDE_WEBHOOK_URL=https://kunde.example/x#anker\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELLI_API_KEY", raising=False)
    monkeypatch.setenv("TOOL_SECRET", "aus-umgebung")
    k = konfig.Konfig.aus_umgebung()
    assert k.telli_api_key == "aus-datei"
    assert k.tool_secret == "aus-umgebung"                              # Umgebung schlägt Datei
    assert k.kunde_webhook_url == "https://kunde.example/x#anker"       # Raute ohne Leerzeichen bleibt
    assert k.fehlende_geheimnisse() == ["TELLI_WEBHOOK_SECRET"]
    assert konfig.Konfig().fehlende_geheimnisse() == ["TELLI_API_KEY", "TELLI_WEBHOOK_SECRET", "TOOL_SECRET"]


def test_example_env_file_is_parseable_and_empty_by_default():
    werte = konfig._env_datei(os.path.join(os.path.dirname(__file__), "..", ".env.example"))
    assert set(werte) >= {"TELLI_API_KEY", "TELLI_WEBHOOK_SECRET", "TOOL_SECRET", "KUNDE_WEBHOOK_URL", "HUBSPOT_TOKEN"}
    assert werte["TELLI_API_KEY"] == "" and werte["ZEITZONE"] == "Europe/Berlin"
