#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
from pathlib import Path
import re
import subprocess
import sys
import time

from service_control import SERVICE
REPORT_DIR = Path(__file__).resolve().parent / "test_reports"

PHRASES = [
    "turn on kitchen lights",
    "turn off kitchen lights",
    "turn kitchen lights on",
    "switch kitchen lights off",
    "set kitchen lights to 50 percent",
    "turn on kitchen lights after 5 minutes",
    "ask cloud explain why the sky is blue",
    "send to cloud what is the weather likely this week",
]


def _norm(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _exact_match(expected: str, detected: str) -> tuple[bool, str]:
    e = _norm(expected)
    d = _norm(detected)
    if e == d:
        return True, "exact normalized match"
    return False, f"expected='{e}' detected='{d}'"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _now_marker() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _journal_since(marker: str) -> str:
    cmd = [
        "journalctl",
        "-u",
        SERVICE,
        "--since",
        marker,
        "--no-pager",
    ]
    res = _run(cmd, check=False)
    return res.stdout


def _latest_stt_result(log_text: str) -> str | None:
    matches = re.findall(r"STT (?:result|\(server\)|\(cli\)):\s*(.+)$", log_text, flags=re.MULTILINE)
    if not matches:
        return None
    return matches[-1].strip()


def _journal_tail(lines: int = 300) -> str:
    cmd = [
        "journalctl",
        "-u",
        SERVICE,
        "-n",
        str(lines),
        "--no-pager",
    ]
    res = _run(cmd, check=False)
    return res.stdout


def _latest_stt_event() -> tuple[str | None, str | None]:
    """
    Returns (event_id, transcript), where event_id is the full STT result log line.
    Using full-line identity avoids timestamp marker race conditions.
    """
    log_text = _journal_tail(300)
    lines = [ln for ln in log_text.splitlines() if re.search(r"STT (?:result|\(server\)|\(cli\)):", ln)]
    if not lines:
        return None, None
    event_line = lines[-1]
    m = re.search(r"STT (?:result|\(server\)|\(cli\)):\s*(.+)$", event_line)
    transcript = m.group(1).strip() if m else ""
    return event_line, transcript


def _latest_abort_event() -> tuple[str | None, str | None]:
    """
    Returns (event_id, reason) for receiver-side no-speech abort events.
    """
    log_text = _journal_tail(300)
    lines = log_text.splitlines()

    for ln in reversed(lines):
        if "Pre-speech timeout" in ln:
            return ln, "pre_speech_timeout"
        if "Aborting silent/short utterance" in ln:
            return ln, "silent_abort"
    return None, None


def _set_test_mode(enabled: bool) -> None:
    from service_control import set_test_mode
    set_test_mode(enabled)


def _wait_for_service_ready(timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        res = _run(["systemctl", "is-active", SERVICE], check=False)
        if res.returncode == 0 and res.stdout.strip() == "active":
            return True
        time.sleep(0.3)
    return False


def _wait_for_phrase_detection(
    previous_event_id: str | None,
    previous_abort_id: str | None,
    timeout_s: float = 20.0,
) -> tuple[str | None, str | None, str | None, str | None]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        event_id, transcript = _latest_stt_event()
        if event_id and event_id != previous_event_id:
            return event_id, transcript, previous_abort_id, None

        abort_id, reason = _latest_abort_event()
        if abort_id and abort_id != previous_abort_id:
            return previous_event_id, None, abort_id, reason
        time.sleep(0.4)
    return previous_event_id, None, previous_abort_id, "timeout"


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive phrase routine in STT test mode")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout per phrase in seconds")
    args = parser.parse_args()

    print("Starting STT phrase routine (transcribe-only mode).")
    print("Commands, cloud calls, and TTS are disabled during this routine.")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"testphrases_{ts}.csv"

    rows = []
    timed_out = False

    try:
        _set_test_mode(True)
        if not _wait_for_service_ready():
            print("ERROR: service did not become active in test mode.")
            return 1

        print("\nService ready in STT test mode.")
        print("Speak each phrase once when prompted.")
        print("Tip: say wakeword and phrase in one continuous utterance with minimal pause.\n")

        last_event_id, _ = _latest_stt_event()
        last_abort_id, _ = _latest_abort_event()

        for i, phrase in enumerate(PHRASES, start=1):
            print(f"[{i}/{len(PHRASES)}] Please say: \"{phrase}\"")
            last_event_id, transcript, last_abort_id, reason = _wait_for_phrase_detection(
                last_event_id,
                last_abort_id,
                timeout_s=args.timeout,
            )
            if transcript is None:
                if reason == "pre_speech_timeout":
                    print("  -> No speech onset detected after wakeword (pre-speech timeout)")
                elif reason == "silent_abort":
                    print("  -> Receiver aborted silent/short utterance")
                else:
                    print("  -> Timeout: no STT result detected")
                print("Run aborted. No report has been recorded.")
                timed_out = True
                break

            print(f"  -> Detected: \"{transcript}\"")
            exact_ok, exact_reason = _exact_match(phrase, transcript)
            print(f"  -> Exact: {'PASS' if exact_ok else 'FAIL'} ({exact_reason})")
            rows.append({
                "index": i,
                "expected_phrase": phrase,
                "detected_transcript": transcript,
                "status": "detected",
                "exact_pass": "1" if exact_ok else "0",
                "exact_reason": exact_reason,
            })
            print()

        if timed_out:
            input("Press <CR> to return to normal mode...")
            return 1

        with report_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "index",
                    "expected_phrase",
                    "detected_transcript",
                    "status",
                    "exact_pass",
                    "exact_reason",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)

        print(f"Report written: {report_path}")

        input("Sequence complete. Press <CR> to return to normal mode...")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        try:
            _set_test_mode(False)
            _wait_for_service_ready()
            print("Normal mode restored.")
        except Exception as e:
            print(f"WARNING: failed to fully restore normal mode: {e}")


if __name__ == "__main__":
    sys.exit(main())
