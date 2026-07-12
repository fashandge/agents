from agents import env


def test_build_env_sources_linux_bashrc_and_exports_bare_secrets(monkeypatch, tmp_path):
    """Linux uses Bash startup and exports bare secrets.env assignments."""
    (tmp_path / ".bashrc").write_text(
        'export PATH="/linux-agent-bin:$PATH"\nexport BASHRC_MARKER=loaded\n',
        encoding="utf-8",
    )
    secrets_dir = tmp_path / ".config"
    secrets_dir.mkdir()
    (secrets_dir / "secrets.env").write_text(
        "BARE_SECRET=available\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USER", "test-user")
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setattr(env.sys, "platform", "linux")

    built = env.build_env()

    assert built["BASHRC_MARKER"] == "loaded"
    assert "/linux-agent-bin" in built["PATH"]
    assert built["BARE_SECRET"] == "available"
