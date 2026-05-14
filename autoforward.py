#!/usr/bin/env python3
"""Maintain port forwards across both routers in a double-NAT setup.

Discovers the inner router (default gateway), reads its WAN IP via
GetExternalIPAddress, locates the outer router via traceroute hop 2,
checks each desired mapping on the outer router via
GetSpecificPortMappingEntry, and applies only the missing or stale
entries — with destination pointing back to the inner router's WAN IP.

Cron-safe: idempotent in the steady state, exit 0 only when every
desired mapping is in place.

This script uses upnp.py two ways: as a subprocess (for discover and
forward, where --json output keeps the boundary clean), and as an
imported module (for the per-port GetSpecificPortMappingEntry check).
The direct-call path is required because some carrier routers
(observed: Zhiyun-IGD) don't implement GetGenericPortMappingEntry and
return UPnPError 401 to upnp.py status, so the bulk-diff approach
doesn't work against them.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
UPNP_CMD = [sys.executable, str(SCRIPT_DIR / "upnp.py")]

# Make upnp.py importable as a module so we can call its SOAP layer
# directly for the per-port check below.
sys.path.insert(0, str(SCRIPT_DIR))
import upnp  # noqa: E402


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
    """Expand upnp.py's default preset into desired-mapping dicts."""
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


def service_from_cache(host: str) -> upnp.ServiceInfo:
    """Reconstruct a ServiceInfo from the on-disk discovery cache.
    Requires `discover` to have been run already for this host."""
    entry = upnp.cache_get(upnp.CACHE_PATH, host)
    if not entry:
        raise RuntimeError(
            f"{host!r} not in discovery cache; run `upnp.py discover {host}` first"
        )
    return upnp.ServiceInfo(
        service_type=entry["service_type"],
        service_id="",
        control_url=entry["control_url"],
        scpd_url="",
    )


def check_mapping(svc: upnp.ServiceInfo, desired: dict,
                  timeout: float = 5.0) -> str:
    """Classify one desired mapping as 'present', 'absent', or 'stale'.

    Uses GetSpecificPortMappingEntry rather than enumerating with
    GetGenericPortMappingEntry, because the latter is not implemented
    by every IGD (e.g. Zhiyun-IGD returns 401 Invalid Action).

    Compares NewInternalClient only — NewInternalPort cannot be trusted
    on routers that byte-swap it in the response (observed on
    Zhiyun-IGD: a port-60010 mapping reads back as 27370,
    0xEA6A ↔ 0x6AEA). Our preset always sets external_port ==
    internal_port, so a client match is sufficient to identify our
    mapping.
    """
    try:
        resp = upnp.soap_call(svc, "GetSpecificPortMappingEntry", {
            "NewRemoteHost": "",
            "NewExternalPort": str(desired["external_port"]),
            "NewProtocol": desired["protocol"],
        }, timeout=timeout)
    except upnp.SoapFault as e:
        if e.code == 714:  # NoSuchEntryInArray — spec-compliant "absent"
            return "absent"
        raise
    if resp.get("NewInternalClient") == desired["internal_client"]:
        return "present"
    return "stale"


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

    outer_svc = service_from_cache(outer_ip)
    try:
        ext = upnp.soap_call(outer_svc, "GetExternalIPAddress", {}, timeout=5.0)
        print(f"  external IP: {ext.get('NewExternalIPAddress', '?')}")
    except (upnp.SoapFault, RuntimeError) as e:
        print(f"  external IP: (lookup failed: {e})")

    desired = desired_mappings(inner_wan_ip)
    to_apply: list[dict] = []
    counts = {"present": 0, "absent": 0, "stale": 0}
    for d in desired:
        try:
            state = check_mapping(outer_svc, d)
        except upnp.SoapFault as e:
            sys.stderr.write(
                f"error: GetSpecificPortMappingEntry "
                f"{d['protocol']} {d['external_port']}: {e}\n"
            )
            return 1
        counts[state] += 1
        if state != "present":
            to_apply.append(d)

    print("\n== Plan ==")
    print(f"  desired: {len(desired)}")
    print(f"  present: {counts['present']}")
    print(f"  absent:  {counts['absent']}")
    print(f"  stale:   {counts['stale']}")

    if not to_apply:
        print("\nAll desired mappings already in place.")
        return 0

    specs = [f"{d['external_port']}/{d['protocol'].lower()}" for d in to_apply]
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
