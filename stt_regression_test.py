#!/usr/bin/env python3
import argparse
import datetime as dt
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass

from service_control import SERVICE


@dataclass
class TestCase:
    phrase: str
    must_include: list[str]


# Standard STT regression suite. Keep this stable so improvements/regressions are measurable.
SUITE = [
    TestCase("turn on kitchen lights", ["turn", "on", "kitchen", "light"]),
    TestCase("turn off kitchen lights", ["turn", "off", "kitchen", "light"]),
    TestCase("turn kitchen lights on", ["turn", "kitchen", "light", "on"]),
    TestCase("switch kitchen lights off", ["switch", "kitchen", "light", "off"]),
    TestCase("set kitchen lights to 50 percent", ["set", "kitchen", "light", "50"]),
    TestCase("turn on kitchen lights after 5 minutes", ["turn", "on", "kitchen", "light", "after", "5"]),
    TestCase("ask cloud explain why the sky is blue", ["ask", "cloud", "sky", "blue"]),
    TestCase("send to cloud what is the weather likely this week", ["send", "cloud", "weather", "week"]),
]


def _now_marker() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _fetch_since(marker: str) -> str:
    cmd = [
        "journalctl",
        "-u",
        SERVICE,
        "--since",
        marker,
        "--no-pager",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout


def _extract_latest(log_text: str) -> tuple[str | None, float | None]:
    stt_results = re.findall(r"STT (?:result|\(server\)|\(cli\)):\s*(.+)$", log_text, flags=re.MULTILINE)
    timings = re.findall(r"STT timings .* total=([0-9.]+)s", log_text, flags=re.MULTILINE)

    transcript = stt_results[-1].strip() if stt_results else None
    latency = float(timings[-1]) if timings else None
    return transcript, latency


def _wait_for_stt(marker: str, timeout_s: float) -> tuple[str | None, float | None]:
    deadline = time.time() + timeout_s
    last_seen = (None, None)
    while time.time() < deadline:
        log_text = _fetch_since(marker)
        transcript, latency = _extract_latest(log_text)
        if transcript:
            return transcript, latency
        last_seen = (transcript, latency)
        time.sleep(0.4)
    return last_seen


def _passes(case: TestCase, transcript: str) -> tuple[bool, list[str]]:
    t = _normalize(transcript)
    missing = [w for w in case.must_include if w not in t]
    return len(missing) == 0, missing


def run_suite(timeout_s: float) -> int:
    print("STT regression suite starting.")
    print(f"Service: {SERVICE}")
    print(f"Cases: {len(SUITE)}")
    print()

    results = []

    for idx, case in enumerate(SUITE, start=1):
        print(f"[{idx}/{len(SUITE)}] Say exactly: \"{case.phrase}\"")
        input("Press Enter, speak the phrase once, then wait... ")

        marker = _now_marker()
        transcript, latency = _wait_for_stt(marker, timeout_s=timeout_s)

        if not transcript:
            print("  -> FAIL: no STT result captured within timeout")
            results.append((False, None, None, ["<no stt result>"]))
            print()
            continue

        ok, missing = _passes(case, transcript)
        status = "PASS" if ok else "FAIL"
        lat_txt = f"{latency:.2f}s" if latency is not None else "n/a"

        print(f"  -> {status}: transcript=\"{transcript}\" latency={lat_txt}")
        if missing:
            print(f"     missing keywords: {', '.join(missing)}")

        results.append((ok, transcript, latency, missing))
        print()

    passed = sum(1 for r in results if r[0])
    latencies = [r[2] for r in results if isinstance(r[2], float)]

    print("Summary")
    print(f"  Passed: {passed}/{len(SUITE)}")
    if latencies:
        print(f"  STT total avg: {statistics.mean(latencies):.2f}s")
        print(f"  STT total p95: {sorted(latencies)[max(0, int(0.95 * (len(latencies) - 1)) )]:.2f}s")

    return 0 if passed == len(SUITE) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run standard STT regression phrase suite")
    parser.add_argument("--timeout", type=float, default=12.0, help="Per-phrase timeout in seconds")
    args = parser.parse_args()

    try:
        return run_suite(timeout_s=args.timeout)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
