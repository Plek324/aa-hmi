from aa_hmi.cli import build_parser, cmd_run, main
from aa_hmi.errors import HINT_NMCLI_NOT_AUTHORIZED, NmcliError


def _fake_cmd(record, key="ok"):
    def fn(args):
        record[key] = True
        return 0
    return fn


def test_run_is_the_default_subcommand():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.command == "run"
    assert args.rescan is False


def test_bare_invocation_defaults_to_run(monkeypatch):
    called = {}
    monkeypatch.setattr("aa_hmi.cli.cmd_run", _fake_cmd(called))
    rc = main([])
    assert rc == 0
    assert called.get("ok") is True


def test_flags_parse_onto_run_namespace():
    parser = build_parser()
    args = parser.parse_args([
        "run", "--rescan", "--device", "AA:BB:CC:DD:EE:FF", "--channel", "4",
        "--non-interactive", "--no-wifi-connect", "--json",
        "--radio-coexistence-workaround", "--wifi-iface", "wlan0",
        "--bt-timeout", "60", "-v",
    ])
    assert args.rescan is True
    assert args.device == "AA:BB:CC:DD:EE:FF"
    assert args.channel == 4
    assert args.non_interactive is True
    assert args.no_wifi_connect is True
    assert args.json is True
    assert args.radio_coexistence_workaround is True
    assert args.wifi_iface == "wlan0"
    assert args.bt_timeout == 60.0
    assert args.verbose is True


def test_list_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(["list"])
    assert args.command == "list"


def test_forget_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(["forget", "TF811BT", "--all"])
    assert args.command == "forget"
    assert args.mac_or_name == "TF811BT"
    assert args.all is True


def test_main_dispatches_to_list(monkeypatch):
    called = {}
    monkeypatch.setattr("aa_hmi.cli.cmd_list", _fake_cmd(called))
    rc = main(["list"])
    assert rc == 0
    assert called.get("ok") is True


def test_run_shows_polkit_hint_on_nmcli_not_authorized(monkeypatch, capsys):
    """Regression test for a real failure hit during live testing
    (2026-09-18): a plain SSH session without an active console/logind
    session gets 'Not authorized to control networking' from nmcli.
    cmd_run must surface the actionable hint, not just the raw error."""
    def fake_inner(args):
        raise NmcliError(["nmcli", "device", "wifi", "connect", "x"], 4,
                          "Error: Failed to add/activate new connection: Not authorized to control networking.")

    monkeypatch.setattr("aa_hmi.cli._cmd_run_inner", fake_inner)
    parser = build_parser()
    args = parser.parse_args(["run"])
    rc = cmd_run(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert HINT_NMCLI_NOT_AUTHORIZED in captured.err


def test_run_does_not_show_polkit_hint_for_unrelated_nmcli_failures(monkeypatch, capsys):
    def fake_inner(args):
        raise NmcliError(["nmcli", "connection", "delete", "x"], 1, "Error: Permission denied")

    monkeypatch.setattr("aa_hmi.cli._cmd_run_inner", fake_inner)
    parser = build_parser()
    args = parser.parse_args(["run"])
    rc = cmd_run(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert HINT_NMCLI_NOT_AUTHORIZED not in captured.err


def test_main_dispatches_to_forget(monkeypatch):
    called = {}
    monkeypatch.setattr("aa_hmi.cli.cmd_forget", _fake_cmd(called))
    rc = main(["forget", "--all"])
    assert rc == 0
    assert called.get("ok") is True


def test_serve_subcommand_parses_bootstrap_and_daemon_flags():
    parser = build_parser()
    args = parser.parse_args([
        "serve", "--device", "AA:BB:CC:DD:EE:FF", "--channel", "4", "--rescan",
        "--radio-coexistence-workaround", "--wifi-iface", "wlan0", "--bt-timeout", "60",
        "--socket-path", "/tmp/x.sock", "--display-ip", "192.168.10.1",
        "--cert", "/tmp/c.pem", "--key", "/tmp/k.pem",
        "--persistent-session", "--reconnect-max-attempts", "10", "--liveness-timeout", "45", "-v",
    ])
    assert args.command == "serve"
    assert args.device == "AA:BB:CC:DD:EE:FF"
    assert args.channel == 4
    assert args.rescan is True
    assert args.radio_coexistence_workaround is True
    assert args.wifi_iface == "wlan0"
    assert args.bt_timeout == 60.0
    assert args.socket_path == "/tmp/x.sock"
    assert args.display_ip == "192.168.10.1"
    assert args.cert == "/tmp/c.pem"
    assert args.key == "/tmp/k.pem"
    assert args.persistent_session is True
    assert args.reconnect_max_attempts == 10
    assert args.liveness_timeout == 45.0
    assert args.verbose is True


def test_serve_liveness_timeout_defaults_to_60():
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.liveness_timeout == 60.0


def test_serve_defaults_to_non_interactive():
    """An unattended daemon must never block on input() -- unlike `run`,
    which defaults non_interactive to False."""
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.non_interactive is True


def test_run_defaults_radio_coexistence_workaround_off():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.radio_coexistence_workaround is False


def test_serve_defaults_radio_coexistence_workaround_on():
    """Regression test for a real bug found live (2026-09-22): serve's
    own reconnect loop redoes the full BT bootstrap on every drop, so
    without this defaulting on, WiFi/BT radio contention (and leftover
    WiFi state from any prior run, graceful or not) would keep breaking
    reconnects. `run` intentionally keeps the old opt-in default."""
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.radio_coexistence_workaround is True


def test_serve_radio_coexistence_workaround_can_be_disabled_explicitly():
    parser = build_parser()
    args = parser.parse_args(["serve", "--no-radio-coexistence-workaround"])
    assert args.radio_coexistence_workaround is False


def test_serve_is_in_default_subcommand_dispatch_list():
    """`aa-hmi serve ...` must not get an implicit `run` prepended the
    way a bare `aa-hmi -v` does."""
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.command == "serve"


def test_main_dispatches_to_serve(monkeypatch):
    called = {}
    monkeypatch.setattr("aa_hmi.cli.cmd_serve", _fake_cmd(called))
    rc = main(["serve"])
    assert rc == 0
    assert called.get("ok") is True


def test_cmd_serve_shows_polkit_hint_on_nmcli_not_authorized(monkeypatch, capsys):
    def fake_run_daemon(args):
        raise NmcliError(["nmcli", "device", "wifi", "connect", "x"], 4,
                          "Error: Failed to add/activate new connection: Not authorized to control networking.")

    monkeypatch.setattr("aa_hmi.daemon.run_daemon", fake_run_daemon)
    parser = build_parser()
    args = parser.parse_args(["serve"])
    from aa_hmi.cli import cmd_serve
    rc = cmd_serve(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert HINT_NMCLI_NOT_AUTHORIZED in captured.err


def test_serve_timestamp_mode_defaults_to_elapsed_and_accepts_per_slice():
    parser = build_parser()
    assert parser.parse_args(["serve"]).timestamp_mode == "elapsed"
    assert parser.parse_args(["serve", "--timestamp-mode", "per-slice"]).timestamp_mode == "per-slice"
