from __future__ import annotations

import pytest

from app import cli

pytestmark = pytest.mark.unit


def test_filter_rows_matches_driver_and_device_prefix():
    rows = [
        cli.ProbeResult(
            id="antsdre200:0",
            driver="antsdre200",
            label="ANTSDR",
            busy="no",
            owner="",
            serial="A1",
            notes="",
            freq_min_hz=1_000_000,
            freq_max_hz=6_000_000_000,
            max_sample_rate_sps=20_000_000,
        ),
        cli.ProbeResult(
            id="hackrf:0",
            driver="hackrf",
            label="HackRF",
            busy="no",
            owner="",
            serial="H1",
            notes="",
            freq_min_hz=1_000_000,
            freq_max_hz=6_000_000_000,
            max_sample_rate_sps=20_000_000,
        ),
    ]

    assert [row.id for row in cli._filter_rows(rows, "antsdre200")] == ["antsdre200:0"]
    assert [row.id for row in cli._filter_rows(rows, "hackrf:0")] == ["hackrf:0"]


def test_run_probe_with_test_invokes_stream_probe(monkeypatch, capsys):
    row = cli.ProbeResult(
        id="antsdre200:0",
        driver="antsdre200",
        label="ANTSDR",
        busy="no",
        owner="",
        serial="A1",
        notes="",
        freq_min_hz=1_000_000,
        freq_max_hz=6_000_000_000,
        max_sample_rate_sps=20_000_000,
    )

    monkeypatch.setattr(cli, "_probe_via_http", lambda base_url, token: ([row], None))
    monkeypatch.setattr(cli, "_run_stream_test", lambda base_url, token, selected: (True, f"{selected.id}: ok"))

    args = cli._parse_args.__globals__["argparse"].Namespace(
        probe=True,
        driver="antsdre200",
        test=True,
        base_url="http://127.0.0.1:8080",
        token="",
        format="entries",
        host="127.0.0.1",
        port=8080,
        reload=False,
    )

    rc = cli._run_probe(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "antsdre200:0" in out
    assert "TEST antsdre200:0: ok" in out
