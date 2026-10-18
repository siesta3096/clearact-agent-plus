from clearact import cli


def test_project_root_resolves_to_repository_root():
    assert (cli._project_root() / "clearact.json").is_file()


def test_load_dotenv_does_not_override_existing_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("CLEARACT_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("CLEARACT_TEST_KEY", "from-environment")

    cli._load_dotenv(tmp_path)

    assert cli.os.environ["CLEARACT_TEST_KEY"] == "from-environment"


def test_doctor_accepts_inline_key_without_environment_name(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    settings = SimpleNamespace(workspace_root=tmp_path, data_root=tmp_path, models={
        "default_profile": "local", "profiles": {"local": {"provider": "openai_compatible",
                                                        "api_key": "synthetic-doctor-key"}},
    })
    monkeypatch.setattr(cli, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    assert cli._doctor() == 0
    assert "synthetic-doctor-key" not in capsys.readouterr().out
