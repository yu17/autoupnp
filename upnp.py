#!/usr/bin/env python3
"""UPnP IGD discovery and port-mapping client. See docs/superpowers/specs/."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_PATH = SCRIPT_DIR / ".upnp-cache.json"

PRESETS = {
    "default": [
        "60010/both", "60020/both", "60022/both", "60023/both",
        "60024/both", "60030/both", "60042/both", "60050/both",
        "60060/both", "60061/both", "60062/both", "60080/both",
        "60081/both", "60082/both", "60083/both", "60090/both",
        "30030/both",
    ],
}


def parse_port_spec(spec: str, default_proto: str) -> list[tuple[int, str]]:
    """Parse one port spec like '60010', '60010/tcp', '60010-60015/both'.
    Returns a list of (port, 'TCP'|'UDP') pairs. Raises ValueError on bad input."""
    if "/" in spec:
        port_part, proto_part = spec.rsplit("/", 1)
        proto_part = proto_part.lower()
    else:
        port_part, proto_part = spec, default_proto.lower()

    if proto_part not in ("tcp", "udp", "both"):
        raise ValueError(f"invalid protocol in {spec!r}: {proto_part!r}")

    if "-" in port_part:
        lo_s, hi_s = port_part.split("-", 1)
        try:
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            raise ValueError(f"invalid port range in {spec!r}")
        if lo > hi:
            raise ValueError(f"inverted port range in {spec!r}")
        ports = list(range(lo, hi + 1))
    else:
        try:
            ports = [int(port_part)]
        except ValueError:
            raise ValueError(f"invalid port in {spec!r}")

    for p in ports:
        if not (1 <= p <= 65535):
            raise ValueError(f"port out of range in {spec!r}: {p}")

    protos = ["TCP", "UDP"] if proto_part == "both" else [proto_part.upper()]
    return [(p, proto) for p in ports for proto in protos]


def expand_port_specs(specs: list[str], default_proto: str) -> list[tuple[int, str]]:
    """Expand multiple port specs into a deduplicated, ordered list."""
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []
    for s in specs:
        for pair in parse_port_spec(s, default_proto):
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out


def parse_ssdp_response(raw: bytes) -> dict[str, str]:
    """Parse an SSDP HTTP/1.1 response into lowercase-keyed headers
    plus a 'status' field. Raises ValueError on non-200 or missing
    LOCATION header."""
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    if not lines:
        raise ValueError("empty SSDP response")

    status_line = lines[0]
    parts = status_line.split(None, 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise ValueError(f"malformed SSDP status line: {status_line!r}")
    status = parts[1]
    if status != "200":
        raise ValueError(f"SSDP non-200 status: {status}")

    headers: dict[str, str] = {"status": status}
    for line in lines[1:]:
        if not line:
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()

    if "location" not in headers:
        raise ValueError("SSDP response missing LOCATION header")
    return headers


SSDP_ADDR = "239.255.255.250"  # standard multicast; we use unicast though
SSDP_PORT = 1900
SSDP_ST_IGD = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"


def _now() -> float:
    return time.monotonic()


def ssdp_msearch(
    host: str,
    timeout: float = 3.0,
    mx: int = 2,
    st: str = SSDP_ST_IGD,
) -> list[dict[str, str]]:
    """Send a unicast SSDP M-SEARCH to host:1900 and return parsed responses.
    Returns an empty list on timeout (no exception).

    Note: HOST is the multicast SSDP address rather than the unicast
    destination. UPnP 1.0 allowed either, but UPnP 2.0 requires the
    multicast address and many responders (e.g. miniupnpd on DD-WRT)
    silently drop M-SEARCH packets that carry the unicast IP in HOST.
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n"
        "\r\n"
    ).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    responses: list[dict[str, str]] = []
    try:
        sock.sendto(msg, (host, SSDP_PORT))
        end = _now() + timeout
        while True:
            remaining = end - _now()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            try:
                responses.append(parse_ssdp_response(data))
            except ValueError:
                continue
    finally:
        sock.close()
    return responses


UPNP_NS = "{urn:schemas-upnp-org:device-1-0}"


@dataclass
class ServiceInfo:
    service_type: str
    service_id: str
    control_url: str
    scpd_url: str


@dataclass
class RootDesc:
    base_url: str
    friendly_name: str = ""
    manufacturer: str = ""
    model_name: str = ""
    services: list[ServiceInfo] = field(default_factory=list)


def parse_root_desc(xml_text: str, base_url: str) -> RootDesc:
    """Parse an IGD rootDesc.xml document. base_url is the URL it was
    fetched from; relative controlURLs are resolved against it."""
    root = ET.fromstring(xml_text)
    rd = RootDesc(base_url=base_url)

    device = root.find(f"{UPNP_NS}device")
    if device is None:
        raise ValueError("rootDesc has no <device> element")

    fn = device.findtext(f"{UPNP_NS}friendlyName")
    mf = device.findtext(f"{UPNP_NS}manufacturer")
    mn = device.findtext(f"{UPNP_NS}modelName")
    rd.friendly_name = fn or ""
    rd.manufacturer = mf or ""
    rd.model_name = mn or ""

    def walk(dev: ET.Element) -> None:
        slist = dev.find(f"{UPNP_NS}serviceList")
        if slist is not None:
            for svc in slist.findall(f"{UPNP_NS}service"):
                st = svc.findtext(f"{UPNP_NS}serviceType") or ""
                sid = svc.findtext(f"{UPNP_NS}serviceId") or ""
                ctrl = svc.findtext(f"{UPNP_NS}controlURL") or ""
                scpd = svc.findtext(f"{UPNP_NS}SCPDURL") or ""
                rd.services.append(ServiceInfo(
                    service_type=st,
                    service_id=sid,
                    control_url=urljoin(base_url, ctrl),
                    scpd_url=urljoin(base_url, scpd),
                ))
        dlist = dev.find(f"{UPNP_NS}deviceList")
        if dlist is not None:
            for sub in dlist.findall(f"{UPNP_NS}device"):
                walk(sub)

    walk(device)
    return rd


WAN_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


def find_wan_service(rd: RootDesc) -> ServiceInfo:
    """Return the first WAN*Connection service in the rootDesc."""
    for wanted in WAN_SERVICE_TYPES:
        for svc in rd.services:
            if svc.service_type == wanted:
                return svc
    raise LookupError(
        "no WAN*Connection service found; have: "
        + ", ".join(s.service_type for s in rd.services)
    )


def fetch_url(url: str, timeout: float = 5.0, data: bytes | None = None,
              headers: dict[str, str] | None = None,
              method: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    """Fetch a URL via http.client. Returns (status, body, headers).
    Does not raise on HTTP errors — caller decides what to do with 4xx/5xx.

    Uses http.client rather than urllib.request because the latter
    unconditionally injects `Connection: close` and a Python-urllib
    User-Agent into the request, and some carrier UPnP IGDs (observed:
    "Zhiyun-IGD") respond with HTTP 400 to such requests even though
    the SOAP body is well-formed. http.client only adds Host,
    Content-Length, and Accept-Encoding — every other header is ours.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")

    cls = (http.client.HTTPSConnection if parsed.scheme == "https"
           else http.client.HTTPConnection)
    conn = cls(parsed.netloc, timeout=timeout)

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if method is None:
        method = "POST" if data is not None else "GET"

    try:
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body, dict(resp.getheaders())
    finally:
        conn.close()


def fetch_root_desc(url: str, timeout: float = 5.0) -> RootDesc:
    """Fetch a rootDesc XML document and parse it."""
    status, body, _hdrs = fetch_url(url, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"rootDesc fetch failed: HTTP {status} from {url}")
    return parse_root_desc(body.decode("utf-8", errors="replace"), base_url=url)


def build_soap_envelope(service_type: str, action: str,
                        args: dict[str, str]) -> str:
    """Build a SOAP 1.1 envelope for a UPnP action.

    Elements are CRLF-separated. miniupnpd accepts compact envelopes,
    but some carrier IGDs (observed: "Zhiyun-IGD") return HTTP 400
    when the body is one long line. CRLF between elements matches
    miniupnpc's wire format and is accepted everywhere we have tested.
    """
    arg_xml = "".join(
        f"<{name}>{xml_escape(str(value))}</{name}>\r\n"
        for name, value in args.items()
    )
    return (
        '<?xml version="1.0"?>\r\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\r\n'
        "<s:Body>\r\n"
        f'<u:{action} xmlns:u="{service_type}">\r\n'
        f"{arg_xml}"
        f"</u:{action}>\r\n"
        "</s:Body>\r\n"
        "</s:Envelope>\r\n"
    )


SOAP_NS = "{http://schemas.xmlsoap.org/soap/envelope/}"
UPNP_CONTROL_NS = "{urn:schemas-upnp-org:control-1-0}"


class SoapFault(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"UPnPError {code}: {description}")
        self.code = code
        self.description = description


def parse_soap_response(xml_text: str, action: str) -> dict[str, str]:
    """Parse a SOAP response. Returns child element name/text pairs from
    the <u:{action}Response> element. Raises SoapFault on a SOAP fault."""
    root = ET.fromstring(xml_text)
    body = root.find(f"{SOAP_NS}Body")
    if body is None:
        raise ValueError("SOAP response has no <Body>")

    fault = body.find(f"{SOAP_NS}Fault")
    if fault is not None:
        upnp_err = fault.find(f".//{UPNP_CONTROL_NS}UPnPError")
        if upnp_err is not None:
            code = int(upnp_err.findtext(f"{UPNP_CONTROL_NS}errorCode") or "0")
            desc = upnp_err.findtext(f"{UPNP_CONTROL_NS}errorDescription") or ""
            raise SoapFault(code, desc)
        fc = fault.findtext("faultcode") or ""
        fs = fault.findtext("faultstring") or ""
        raise SoapFault(0, f"{fc}: {fs}")

    suffix = f"{action}Response"
    for child in body:
        tag = child.tag.split("}", 1)[-1]
        if tag == suffix:
            return {
                c.tag.split("}", 1)[-1]: (c.text or "")
                for c in child
            }
    raise ValueError(f"no <{suffix}> element found in SOAP body")


def soap_call(service: ServiceInfo, action: str,
              args: dict[str, str] | None = None,
              timeout: float = 5.0,
              verbose: bool = False) -> dict[str, str]:
    """Invoke a SOAP action on a service. Returns the response args dict.
    Raises SoapFault on UPnPError, RuntimeError on HTTP/transport errors."""
    envelope = build_soap_envelope(service.service_type, action, args or {})
    soap_action = f'"{service.service_type}#{action}"'
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": soap_action,
    }
    if verbose:
        sys.stderr.write(f"--> POST {service.control_url}\n{envelope}\n")
    status, body, _hdrs = fetch_url(
        service.control_url, timeout=timeout,
        data=envelope.encode("utf-8"), headers=headers, method="POST",
    )
    body_text = body.decode("utf-8", errors="replace")
    if verbose:
        sys.stderr.write(f"<-- {status}\n{body_text}\n")

    if status == 200:
        return parse_soap_response(body_text, action)
    if status == 500:
        parse_soap_response(body_text, action)  # raises SoapFault
        raise RuntimeError(f"HTTP 500 without parseable fault from {service.control_url}")
    raise RuntimeError(f"SOAP HTTP {status} from {service.control_url}: {body_text[:200]}")


CACHE_VERSION = 1


def cache_load(path: Path) -> dict[str, dict]:
    """Load the cache file. Returns the 'entries' dict, or {} if missing.
    A corrupt file is renamed to .bad and an empty dict returned."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            raise ValueError("cache schema mismatch")
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            raise ValueError("entries is not a dict")
        return entries
    except (json.JSONDecodeError, ValueError, OSError) as e:
        bad = path.with_suffix(path.suffix + ".bad")
        try:
            path.rename(bad)
        except OSError:
            pass
        sys.stderr.write(f"warning: corrupt cache moved to {bad}: {e}\n")
        return {}


def cache_save(path: Path, entries: dict[str, dict]) -> None:
    payload = {"version": CACHE_VERSION, "entries": entries}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def cache_get(path: Path, host: str, now: float | None = None) -> dict | None:
    entries = cache_load(path)
    entry = entries.get(host)
    if not entry:
        return None
    fetched_at = entry.get("fetched_at", 0)
    ttl = entry.get("ttl_seconds", 0)
    t = time.time() if now is None else now
    if t - fetched_at > ttl:
        return None
    return entry


def cache_put(path: Path, host: str, *, root_desc_url: str,
              control_url: str, service_type: str,
              ttl_seconds: int = 86400,
              fetched_at: float | None = None) -> None:
    entries = cache_load(path)
    entries[host] = {
        "root_desc_url": root_desc_url,
        "control_url": control_url,
        "service_type": service_type,
        "fetched_at": int(time.time() if fetched_at is None else fetched_at),
        "ttl_seconds": ttl_seconds,
    }
    cache_save(path, entries)


def cache_invalidate(path: Path, host: str) -> None:
    entries = cache_load(path)
    if host in entries:
        del entries[host]
        cache_save(path, entries)


def default_gateway() -> str:
    """Return the default-route gateway IP by parsing `ip route show default`."""
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError(f"could not run 'ip route show default': {e}")
    for line in out.splitlines():
        # "default via 10.0.0.1 dev eth0 ..."
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "default" and parts[1] == "via":
            return parts[2]
    raise RuntimeError("no default route found in `ip route show default`")


def local_ip_for(host: str) -> str:
    """Return the local source IP the kernel would use to reach host.
    Uses a connected UDP socket — does not actually send any packets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 1))
        return sock.getsockname()[0]
    finally:
        sock.close()


def resolve_service(args: argparse.Namespace) -> tuple[ServiceInfo, str]:
    """Return (service, host). Honors --url, --host, --no-cache.
    Discovers via SSDP if necessary and updates the cache."""
    if getattr(args, "url", None):
        rd = fetch_root_desc(args.url, timeout=args.timeout)
        svc = find_wan_service(rd)
        host = urlparse(args.url).hostname or ""
        return svc, host

    host = getattr(args, "host", None) or default_gateway()

    if not getattr(args, "no_cache", False):
        entry = cache_get(CACHE_PATH, host)
        if entry:
            svc = ServiceInfo(
                service_type=entry["service_type"],
                service_id="",
                control_url=entry["control_url"],
                scpd_url="",
            )
            return svc, host

    responses = ssdp_msearch(host, timeout=args.timeout, mx=2)
    if not responses:
        responses = ssdp_msearch(host, timeout=args.timeout * 2, mx=5)
    if not responses:
        raise RuntimeError(
            f"no SSDP response from {host}:1900 after retries"
        )
    location = responses[0]["location"]
    rd = fetch_root_desc(location, timeout=args.timeout)
    svc = find_wan_service(rd)
    cache_put(CACHE_PATH, host,
              root_desc_url=location,
              control_url=svc.control_url,
              service_type=svc.service_type)
    return svc, host


def cmd_discover(args: argparse.Namespace) -> int:
    host = args.host or default_gateway()
    responses = ssdp_msearch(host, timeout=args.timeout, mx=2)
    if not responses:
        responses = ssdp_msearch(host, timeout=args.timeout * 2, mx=5)
    if not responses:
        sys.stderr.write(
            f"error: no SSDP response from {host}:1900 "
            f"after {args.timeout}s (tried MX=2 then MX=5)\n"
        )
        return 2

    location = responses[0]["location"]
    try:
        rd = fetch_root_desc(location, timeout=args.timeout)
        svc = find_wan_service(rd)
    except Exception as e:
        sys.stderr.write(f"error: rootDesc fetch/parse failed: {e}\n")
        return 3

    cache_put(CACHE_PATH, host,
              root_desc_url=location,
              control_url=svc.control_url,
              service_type=svc.service_type)

    if args.json:
        print(json.dumps({
            "host": host,
            "root_desc_url": location,
            "friendly_name": rd.friendly_name,
            "manufacturer": rd.manufacturer,
            "model_name": rd.model_name,
            "service_type": svc.service_type,
            "control_url": svc.control_url,
        }, indent=2))
    else:
        print(f"Host:         {host}")
        print(f"RootDesc URL: {location}")
        print(f"Friendly:     {rd.friendly_name}")
        print(f"Manufacturer: {rd.manufacturer}")
        print(f"Model:        {rd.model_name}")
        print(f"Service:      {svc.service_type}")
        print(f"Control URL:  {svc.control_url}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    host = args.host or default_gateway()
    cmd = ["nmap", "-sU", "-p", "1900", "--script=upnp-info", host]
    if os.geteuid() != 0:
        if subprocess.run(["which", "sudo"], capture_output=True).returncode != 0:
            sys.stderr.write(
                "error: not root and sudo not available. Try `discover` instead.\n"
            )
            return 1
        cmd = ["sudo"] + cmd
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        sys.stderr.write("error: nmap not installed\n")
        return 1


def _soap_call_with_cache_retry(args: argparse.Namespace,
                                svc: ServiceInfo, host: str,
                                action: str, soap_args: dict[str, str]
                                ) -> tuple[ServiceInfo, dict[str, str]]:
    """Run a SOAP call; on HTTP 404 / UPnPError 401, invalidate cache and retry once."""
    try:
        return svc, soap_call(svc, action, soap_args,
                              timeout=args.timeout, verbose=args.verbose)
    except SoapFault as e:
        if e.code != 401:
            raise
    except RuntimeError as e:
        if "404" not in str(e):
            raise

    cache_invalidate(CACHE_PATH, host)
    orig_nc = args.no_cache
    args.no_cache = True
    try:
        svc, _host = resolve_service(args)
    finally:
        args.no_cache = orig_nc
    return svc, soap_call(svc, action, soap_args,
                          timeout=args.timeout, verbose=args.verbose)


def cmd_status(args: argparse.Namespace) -> int:
    try:
        svc, host = resolve_service(args)
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    try:
        svc, ext_resp = _soap_call_with_cache_retry(
            args, svc, host, "GetExternalIPAddress", {})
    except SoapFault as e:
        sys.stderr.write(f"error: GetExternalIPAddress: {e}\n")
        return 1

    external_ip = ext_resp.get("NewExternalIPAddress", "")

    mappings: list[dict] = []
    idx = 0
    while True:
        try:
            _svc, entry = _soap_call_with_cache_retry(
                args, svc, host, "GetGenericPortMappingEntry",
                {"NewPortMappingIndex": str(idx)})
        except SoapFault as e:
            if e.code in (713, 402):  # SpecifiedArrayIndexInvalid / Invalid Args
                break
            if e.code == 401 and idx == 0:
                # Invalid Action: this router doesn't expose
                # GetGenericPortMappingEntry at all. Some carrier IGDs
                # (e.g. Zhiyun-IGD) implement only the Add/Delete/Specific
                # primitives. Treat as "no enumeration available" rather
                # than a hard error; the mapping list will be empty.
                sys.stderr.write(
                    "warning: GetGenericPortMappingEntry not supported by "
                    "this router; mapping list will be empty\n"
                )
                break
            sys.stderr.write(f"error: GetGenericPortMappingEntry: {e}\n")
            return 1
        mappings.append({
            "index": idx,
            "protocol": entry.get("NewProtocol", ""),
            "external_port": int(entry.get("NewExternalPort") or 0),
            "internal_client": entry.get("NewInternalClient", ""),
            "internal_port": int(entry.get("NewInternalPort") or 0),
            "lease_duration": int(entry.get("NewLeaseDuration") or 0),
            "enabled": entry.get("NewEnabled") == "1",
            "description": entry.get("NewPortMappingDescription", ""),
            "remote_host": entry.get("NewRemoteHost", ""),
        })
        idx += 1

    if args.json:
        print(json.dumps({
            "host": host,
            "external_ip": external_ip,
            "mappings": mappings,
        }, indent=2))
    else:
        print(f"Host:        {host}")
        print(f"External IP: {external_ip}")
        print()
        print(f"{'Idx':<4} {'Proto':<5} {'Ext':<6} {'Internal':<22} {'Lease':<7} Description")
        for m in mappings:
            internal = f"{m['internal_client']}:{m['internal_port']}"
            print(f"{m['index']:<4} {m['protocol']:<5} {m['external_port']:<6} "
                  f"{internal:<22} {m['lease_duration']:<7} {m['description']}")
    return 0


def cmd_forward(args: argparse.Namespace) -> int:
    if args.preset and args.ports:
        sys.stderr.write("error: --preset and explicit PORTS are mutually exclusive\n")
        return 2
    if args.preset:
        if args.preset not in PRESETS:
            sys.stderr.write(f"error: unknown preset {args.preset!r}; "
                             f"have: {', '.join(PRESETS)}\n")
            return 2
        specs = PRESETS[args.preset]
    elif args.ports:
        specs = args.ports
    else:
        specs = PRESETS["default"]

    try:
        pairs = expand_port_specs(specs, default_proto=args.proto)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    try:
        svc, host = resolve_service(args)
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    internal = args.internal or local_ip_for(host)
    desc = args.desc or f"upnp.sh:{socket.gethostname()}"

    results: list[dict] = []
    overall_ok = True
    for ext_port, proto in pairs:
        soap_args = {
            "NewRemoteHost": "",
            "NewExternalPort": str(ext_port),
            "NewProtocol": proto,
            "NewInternalPort": str(ext_port),
            "NewInternalClient": internal,
            "NewEnabled": "1",
            "NewPortMappingDescription": desc,
            "NewLeaseDuration": str(args.lease),
        }
        try:
            svc, _ = _soap_call_with_cache_retry(
                args, svc, host, "AddPortMapping", soap_args)
            results.append({
                "protocol": proto,
                "external_port": ext_port,
                "internal_port": ext_port,
                "status": "ok",
            })
        except SoapFault as e:
            overall_ok = False
            results.append({
                "protocol": proto,
                "external_port": ext_port,
                "internal_port": ext_port,
                "status": "fail",
                "error_code": e.code,
                "error_description": e.description,
            })
        except Exception as e:
            overall_ok = False
            results.append({
                "protocol": proto,
                "external_port": ext_port,
                "internal_port": ext_port,
                "status": "fail",
                "error_code": 0,
                "error_description": str(e),
            })

    if args.json:
        print(json.dumps({
            "host": host,
            "internal_client": internal,
            "results": results,
        }, indent=2))
    else:
        for r in results:
            if r["status"] == "ok":
                print(f"ok    {r['protocol']:<3} {r['external_port']} "
                      f"-> {internal}:{r['internal_port']}   "
                      f"lease={args.lease}   \"{desc}\"")
            else:
                print(f"fail  {r['protocol']:<3} {r['external_port']} "
                      f"-> {internal}:{r['internal_port']}   "
                      f"error: {r['error_code']} {r['error_description']}")
    return 0 if overall_ok else 1


def cmd_unforward(args: argparse.Namespace) -> int:
    if not args.ports:
        sys.stderr.write("error: nothing to unforward (no PORTS given)\n")
        return 2

    try:
        pairs = expand_port_specs(args.ports, default_proto=args.proto)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    try:
        svc, host = resolve_service(args)
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        return 2

    results: list[dict] = []
    overall_ok = True
    for ext_port, proto in pairs:
        soap_args = {
            "NewRemoteHost": "",
            "NewExternalPort": str(ext_port),
            "NewProtocol": proto,
        }
        try:
            svc, _ = _soap_call_with_cache_retry(
                args, svc, host, "DeletePortMapping", soap_args)
            results.append({
                "protocol": proto,
                "external_port": ext_port,
                "status": "ok",
            })
        except SoapFault as e:
            if e.code == 714:  # NoSuchEntryInArray — already absent
                results.append({
                    "protocol": proto,
                    "external_port": ext_port,
                    "status": "gone",
                })
            else:
                overall_ok = False
                results.append({
                    "protocol": proto,
                    "external_port": ext_port,
                    "status": "fail",
                    "error_code": e.code,
                    "error_description": e.description,
                })
        except Exception as e:
            overall_ok = False
            results.append({
                "protocol": proto,
                "external_port": ext_port,
                "status": "fail",
                "error_code": 0,
                "error_description": str(e),
            })

    if args.json:
        print(json.dumps({"host": host, "results": results}, indent=2))
    else:
        for r in results:
            if r["status"] == "ok":
                print(f"ok    {r['protocol']:<3} {r['external_port']}")
            elif r["status"] == "gone":
                print(f"gone  {r['protocol']:<3} {r['external_port']}   (no such entry)")
            else:
                print(f"fail  {r['protocol']:<3} {r['external_port']}   "
                      f"error: {r['error_code']} {r['error_description']}")
    return 0 if overall_ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upnp.py",
        description="UPnP IGD discovery and port-mapping client",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="SSDP unicast discovery")
    p_disc.add_argument("host", nargs="?", default=None,
                        help="target router IP (default: local default gateway)")
    p_disc.add_argument("--timeout", type=float, default=3.0)
    p_disc.add_argument("--json", action="store_true")
    p_disc.add_argument("-v", "--verbose", action="store_true")
    p_disc.set_defaults(func=cmd_discover)

    p_scan = sub.add_parser("scan", help="nmap upnp-info (re-execs sudo)")
    p_scan.add_argument("host", nargs="?", default=None)
    p_scan.set_defaults(func=cmd_scan)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-u", "--url", help="explicit rootDesc URL")
    common.add_argument("--host", help="SSDP target")
    common.add_argument("--no-cache", action="store_true")
    common.add_argument("--timeout", type=float, default=3.0)
    common.add_argument("--json", action="store_true")
    common.add_argument("-v", "--verbose", action="store_true")

    p_stat = sub.add_parser("status", parents=[common],
                            help="show external IP and current port mappings")
    p_stat.set_defaults(func=cmd_status)

    p_fwd = sub.add_parser("forward", parents=[common],
                           help="add port mappings (AddPortMapping)")
    p_fwd.add_argument("--proto", choices=["tcp", "udp", "both"], default="both")
    p_fwd.add_argument("--lease", type=int, default=0)
    p_fwd.add_argument("--desc", default=None)
    p_fwd.add_argument("--internal", default=None)
    p_fwd.add_argument("--preset", default=None)
    p_fwd.add_argument("ports", nargs="*")
    p_fwd.set_defaults(func=cmd_forward)

    p_unf = sub.add_parser("unforward", parents=[common],
                           help="remove port mappings (DeletePortMapping)")
    p_unf.add_argument("--proto", choices=["tcp", "udp", "both"], default="both")
    p_unf.add_argument("ports", nargs="*")
    p_unf.set_defaults(func=cmd_unforward)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
