#!/usr/bin/env python3
"""Maintain port forwards across both routers in a double-NAT setup.

Discovers the inner router (default gateway), reads its WAN IP via
GetExternalIPAddress, locates the outer router via traceroute hop 2,
diffs the default preset against what is already forwarded on the
outer router, and applies only the missing entries — with destination
pointing back to the inner router's WAN IP.

Cron-safe: idempotent in the steady state, exit 0 only when every
desired mapping is in place. Calls upnp.py as a subprocess and consumes
its --json output, so it stays decoupled from upnp.py's internals.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
UPNP_CMD = [sys.executable, str(SCRIPT_DIR / "upnp.py")]


def run_upnp(subcommand: str, *args: str) -> dict:
    """Run upnp.py SUBCOMMAND --json ARGS and return parsed JSON.
    Raises RuntimeError on non-zero exit, surfacing stderr."""
    cmd = [*UPNP_CMD, subcommand, "--json", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"`{' '.join(cmd)}` exited {result.returncode}")
    return json.loads(result.stdout)


def find_outer_router_ip() -> str:
    """Return the second-hop gateway IP. Tries traceroute then tracepath."""
    for cmd in (
        ["traceroute", "-n", "-m", "3", "-w", "2", "8.8.8.8"],
        ["tracepath", "-n", "8.8.8.8"],
    ):
        try:
            out = subprocess.check_output(
                cmd, text=True, stderr=subprocess.DEVNULL, timeout=15,
            )
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            continue
        # traceroute:  " 2  192.168.71.1  ..."
        # tracepath:   " 2:  192.168.71.1   ..."
        for line in out.splitlines():
            m = re.match(r"\s*2[:\s]\s+(\S+)", line)
            if m and m.group(1) != "*":
                return m.group(1)
    raise RuntimeError(
        "could not determine outer router IP via traceroute or tracepath"
    )


def desired_mappings(internal_ip: str) -> list[dict]:
    """Expand upnp.py's default preset into desired (proto, port, client) dicts."""
    sys.path.insert(0, str(SCRIPT_DIR))
    import upnp  # noqa: E402

    pairs = upnp.expand_port_specs(upnp.PRESETS["default"], default_proto="both")
    return [
        {
            "protocol": proto,
            "external_port": port,
            "internal_client": internal_ip,
            "internal_port": port,
        }
        for port, proto in pairs
    ]


def is_present(desired: dict, current: list[dict]) -> bool:
    """A mapping is present iff all four routing fields match exactly."""
    return any(
        m["protocol"] == desired["protocol"]
        and m["external_port"] == desired["external_port"]
        and m["internal_client"] == desired["internal_client"]
        and m["internal_port"] == desired["internal_port"]
        for m in current
    )


def main() -> int:
    print("== Inner router ==")
    inner = run_upnp("discover")
    print(f"  host:        {inner['host']}")
    print(f"  model:       {inner['model_name']}")
    print(f"  rootDesc:    {inner['root_desc_url']}")

    inner_status = run_upnp("status")
    inner_wan_ip = inner_status["external_ip"]
    if not inner_wan_ip:
        sys.stderr.write("error: inner router returned empty external IP\n")
        return 2
    print(f"  WAN IP:      {inner_wan_ip}")

    print("\n== Outer router ==")
    outer_ip = find_outer_router_ip()
    print(f"  IP:          {outer_ip} (via traceroute hop 2)")
    outer = run_upnp("discover", outer_ip)
    print(f"  model:       {outer['model_name']}")
    print(f"  rootDesc:    {outer['root_desc_url']}")

    outer_status = run_upnp("status", "--host", outer_ip)
    print(f"  external IP: {outer_status['external_ip']}")
    print(f"  mappings:    {len(outer_status['mappings'])} currently registered")

    desired = desired_mappings(inner_wan_ip)
    missing = [d for d in desired if not is_present(d, outer_status["mappings"])]
    print("\n== Plan ==")
    print(f"  desired: {len(desired)}")
    print(f"  present: {len(desired) - len(missing)}")
    print(f"  missing: {len(missing)}")

    if not missing:
        print("\nAll desired mappings already in place.")
        return 0

    specs = [f"{m['external_port']}/{m['protocol'].lower()}" for m in missing]
    print(f"\n== Applying {len(specs)} mapping(s) ==")
    result = run_upnp(
        "forward",
        "--host", outer_ip,
        "--internal", inner_wan_ip,
        *specs,
    )

    successes = [r for r in result["results"] if r["status"] == "ok"]
    failures = [r for r in result["results"] if r["status"] != "ok"]
    for r in successes:
        print(f"  ok    {r['protocol']:<4} {r['external_port']}")
    for r in failures:
        sys.stderr.write(
            f"  fail  {r['protocol']:<4} {r['external_port']}   "
            f"error: {r.get('error_code', 0)} "
            f"{r.get('error_description', '')}\n"
        )

    if failures:
        sys.stderr.write(f"\nerror: {len(failures)} mapping(s) failed\n")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
