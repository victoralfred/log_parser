"""Shared fixtures: synthetic agent-format logs with known level counts."""

import pytest

# Counts per (logger, level): deterministic totals for assertions.
SYNTH_SPEC = {
    ("CORE", "INFO"): 100,
    ("CORE", "WARN"): 50,
    ("CORE", "ERROR"): 10,
    ("TRACE", "INFO"): 20,
    ("TRACE", "ERROR"): 5,
}
SYNTH_TOTAL = sum(SYNTH_SPEC.values())          # 185
SYNTH_ERRORS = sum(n for (_, lvl), n in SYNTH_SPEC.items() if lvl == "ERROR")  # 15


def make_line(logger, level, i, msg=None):
    msg = msg or f"synthetic {level.lower()} message number {i}"
    return (f"2026-06-01 10:{i // 60 % 60:02d}:{i % 60:02d} UTC | {logger} | {level} | "
            f"(pkg/synth/{logger.lower()}/file.go:{i + 1} in doWork) | {msg}")


def write_synth_logs(directory):
    """Write agent.log + trace-agent.log; returns the directory."""
    agent_lines, trace_lines = [], []
    for (logger, level), count in SYNTH_SPEC.items():
        target = agent_lines if logger == "CORE" else trace_lines
        for i in range(count):
            if logger == "CORE" and level == "WARN":
                # regex-test fodder: variable path so fingerprints collapse
                msg = f"permission denied for /run/docker/netns/{i:012x}"
            elif logger == "CORE" and level == "ERROR":
                msg = f'Post "https://intake.example.com/api/v{i}": connection refused'
            else:
                msg = None
            target.append(make_line(logger, level, i, msg))
    (directory / "agent.log").write_text("\n".join(agent_lines) + "\n")
    (directory / "trace-agent.log").write_text("\n".join(trace_lines) + "\n")
    return directory


@pytest.fixture(scope="session")
def synth_logs(tmp_path_factory):
    return write_synth_logs(tmp_path_factory.mktemp("synthlogs"))
