"""Codex セッションログから取得する残量表示の契約テスト。"""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TARGET = (
    Path(os.environ.get("CODEX_RATE_STATUS_TARGET", ""))
    if os.environ.get("CODEX_RATE_STATUS_TARGET")
    else Path(__file__).with_name("codex-rate-status")
)


class TestCodexRateStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex_home = self.root / "custom codex"
        self.now = datetime.now(timezone.utc)
        self.environment = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C.UTF-8",
            "TZ": "Pacific/Honolulu",
            "TMPDIR": str(self.root),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def event(self, used: Any = 21, weekly: Any = 6, **changes: Any) -> dict:
        result = {
            "timestamp": self.now.isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {"used_percent": used, "window_minutes": 300},
                    "secondary": {"used_percent": weekly, "window_minutes": 10080},
                },
            },
        }
        result.update(changes)
        return result

    def write(
        self, *events: Any, name: str = "session", base: Path | None = None
    ) -> Path:
        path = (base or self.codex_home) / "sessions/2026/09/11" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    def snapshot(self) -> dict:
        return {
            str(path.relative_to(self.root)): (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.root.rglob("*")
        }

    def assert_output(self, expected: str) -> None:
        before = self.snapshot()
        self.assertTrue(TARGET.is_file(), f"実行対象が存在しない: {TARGET}")
        self.assertTrue(os.access(TARGET, os.X_OK), "CLI に実行権限が必要")
        result = subprocess.run(
            [str(TARGET)],
            env=self.environment,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)
        self.assertEqual(self.snapshot(), before, "読み出しでファイルを変更しない")

    def test_displays_remaining_percent_with_minimal_path(self) -> None:
        self.write(self.event())
        self.assert_output("5h:79% 7d:94%\n")

    def test_uses_home_when_codex_home_is_missing_or_empty(self) -> None:
        self.write(self.event(50, 25), base=self.home / ".codex")
        self.write(self.event())
        for value in (None, ""):
            with self.subTest(value=value):
                if value is None:
                    self.environment.pop("CODEX_HOME", None)
                else:
                    self.environment["CODEX_HOME"] = value
                self.assert_output("5h:50% 7d:75%\n")

    def test_explicit_codex_home_takes_precedence(self) -> None:
        self.write(self.event(99, 99), base=self.home / ".codex")
        self.write(self.event())
        self.assert_output("5h:79% 7d:94%\n")

    def test_rounds_half_up_and_accepts_range_endpoints(self) -> None:
        for used, weekly, expected in (
            (20.5, 6.51, "79% 7d:93%"),
            (20.51, 6.49, "79% 7d:94%"),
            (0, 100, "100% 7d:0%"),
            (100, 0, "0% 7d:100%"),
        ):
            with self.subTest(used=used, weekly=weekly):
                self.write(self.event(used, weekly))
                self.assert_output(f"5h:{expected}\n")

    def test_matches_duration_even_when_windows_are_reversed(self) -> None:
        event = self.event()
        limits = event["payload"]["rate_limits"]
        limits["primary"], limits["secondary"] = limits["secondary"], limits["primary"]
        self.write(event)
        self.assert_output("5h:79% 7d:94%\n")

    def test_accepts_legacy_missing_or_null_limit_id(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing):
                event = self.event()
                event["payload"]["rate_limits"]["limit_id"] = None
                if missing:
                    del event["payload"]["rate_limits"]["limit_id"]
                self.write(event)
                self.assert_output("5h:79% 7d:94%\n")

    def test_marks_old_event_regardless_of_file_mtime(self) -> None:
        self.write(
            self.event(timestamp=(self.now - timedelta(seconds=905)).isoformat())
        )
        self.assert_output("5h:79% 7d:94%*\n")

    def test_recent_and_future_events_are_fresh(self) -> None:
        for age in (895, -3600):
            with self.subTest(age=age):
                path = self.write(
                    self.event(
                        timestamp=(self.now - timedelta(seconds=age)).isoformat()
                    )
                )
                os.utime(path, (1, 1))
                self.assert_output("5h:79% 7d:94%\n")

    def test_selects_latest_timestamp_across_files(self) -> None:
        latest = self.event(
            timestamp=self.now.astimezone(timezone(timedelta(hours=9))).isoformat()
        )
        older = self.event(
            60, 60, timestamp=(self.now - timedelta(minutes=1)).isoformat()
        )
        path = self.write(older, latest, name="a")
        os.utime(path, (1, 1))
        self.write(older, name="z")
        self.assert_output("5h:79% 7d:94%\n")

    def test_empty_newer_sessions_do_not_hide_previous_event(self) -> None:
        path = self.write(self.event(), name="old")
        os.utime(path, (1, 1))
        for index in range(130):
            self.write(name=f"new-{index}")
        self.assert_output("5h:79% 7d:94%\n")

    def test_preserves_valid_event_before_large_irrelevant_tail(self) -> None:
        path = self.write(self.event())
        with path.open("ab") as stream:
            stream.write(
                (b'{"type":"response_item","text":"' + b"x" * 10000 + b'"}\n') * 220
            )
            stream.write(b'{"unfinished":')
        self.assert_output("5h:79% 7d:94%\n")

    def test_accepts_complete_final_line_without_newline_and_crlf(self) -> None:
        path = self.write()
        path.write_bytes(b"{}\r\n" + json.dumps(self.event()).encode())
        self.assert_output("5h:79% 7d:94%\n")

    def test_ignores_broken_null_and_other_limit_events(self) -> None:
        premium = self.event(99, 99)
        premium["payload"]["rate_limits"]["limit_id"] = "codex_other"
        absent = self.event()
        absent["payload"]["rate_limits"] = None
        path = self.write(self.event(), premium, absent)
        with path.open("ab") as stream:
            stream.write(b"null\n[]\nnot-json\n\xff\n")
        self.assert_output("5h:79% 7d:94%\n")

    def test_invalid_percent_values_do_not_replace_valid_event(self) -> None:
        invalid: tuple[Any, ...] = (
            -0.01,
            100.01,
            True,
            False,
            None,
            "21",
            "０",
            [],
            {},
            float("inf"),
            float("nan"),
            10**400,
        )
        for window in ("primary", "secondary"):
            for value in invalid:
                with self.subTest(window=window, value=value):
                    event = self.event(99, 99)
                    event["payload"]["rate_limits"][window]["used_percent"] = value
                    self.write(self.event(), event)
                    self.assert_output("5h:79% 7d:94%\n")

    def test_invalid_envelope_and_windows_produce_no_output(self) -> None:
        events: list[Any] = [None, [], {}, self.event(type="response_item")]
        for timestamp in (None, True, 123, "bad", "2026-09-11T12:00:00"):
            events.append(self.event(timestamp=timestamp))
        payloads: tuple[Any, ...] = (None, [], "bad")
        for value in payloads:
            events.append(self.event(payload=value))
        event = self.event()
        event["payload"]["type"] = "other"
        events.append(event)
        replacements: tuple[Any, ...] = (
            None,
            [],
            {},
            {"used_percent": 2, "window_minutes": 60},
        )
        for window in ("primary", "secondary"):
            for replacement in replacements:
                event = self.event()
                event["payload"]["rate_limits"][window] = replacement
                events.append(event)
        for event in events:
            with self.subTest(event=event):
                self.write(event)
                self.assert_output("")

    def test_missing_and_corrupt_sessions_are_silent(self) -> None:
        self.assert_output("")
        path = self.write(None, [], {})
        with path.open("ab") as stream:
            stream.write(b"not-json\n\xff\n")
        self.assert_output("")


if __name__ == "__main__":
    unittest.main()
