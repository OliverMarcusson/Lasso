from lasso import config


def test_missing_file_gives_defaults(tmp_path):
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.phone_address is None and cfg.idle_minutes == 5 and cfg.deny_names == []


def test_unknown_keys_ignored(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('idle_minutes = 9\nsomething_else = 1\ndeny_names = ["a.example"]\n')
    cfg = config.load(p)
    assert cfg.idle_minutes == 9 and cfg.deny_names == ["a.example"]


def test_save_phone_keeps_other_keys_and_replaces(tmp_path):
    p = tmp_path / "lasso" / "c.toml"
    p.parent.mkdir()
    p.write_text('idle_minutes = 9\nphone_address = "old"\n\n[extra]\nx = 1\n')
    config.save_phone("AA:BB", "Galaxy", p)
    config.save_phone("CC:DD", "Galaxy", p)
    cfg = config.load(p)
    assert (cfg.phone_address, cfg.phone_name, cfg.idle_minutes) == ("CC:DD", "Galaxy", 9)
    assert p.read_text().count("phone_address") == 1


def test_save_phone_creates_file(tmp_path):
    p = tmp_path / "sub" / "c.toml"
    config.save_phone("AA:BB", "P", p)
    assert config.load(p).phone_address == "AA:BB"


def test_save_phone_writes_valid_toml_for_any_name(tmp_path):
    import tomllib
    p = tmp_path / "c.toml"
    for name in ["Phone 😀", 'Ann\'s "Pixel" \\ C:\\x', "tab\there\nnew\x7f\x00", "Ünïcødé ✓"]:
        config.save_phone("AA:BB", name, p)
        assert tomllib.loads(p.read_text(encoding="utf-8"))["phone_name"] == name
        assert config.load(p).phone_name == name
    assert "😀" not in p.read_text(encoding="utf-8")  # last name replaced the first
    config.save_phone("AA:BB", "Phone 😀", p)
    assert '"Phone 😀"' in p.read_text(encoding="utf-8")  # emitted directly, not \ud83d escapes
