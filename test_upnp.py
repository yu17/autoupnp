"""Unit tests for upnp.py. Run with: python -m unittest test_upnp.py"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import upnp


class TestParser(unittest.TestCase):
    def test_help_does_not_crash(self):
        parser = upnp.build_parser()
        self.assertIn("discover", parser.format_help())
        self.assertIn("forward", parser.format_help())
        self.assertIn("unforward", parser.format_help())


class TestPortSpec(unittest.TestCase):
    def test_single_port_default_proto(self):
        self.assertEqual(
            upnp.parse_port_spec("60010", default_proto="both"),
            [(60010, "TCP"), (60010, "UDP")],
        )

    def test_single_port_tcp_only(self):
        self.assertEqual(
            upnp.parse_port_spec("60010", default_proto="tcp"),
            [(60010, "TCP")],
        )

    def test_explicit_tcp(self):
        self.assertEqual(
            upnp.parse_port_spec("60010/tcp", default_proto="udp"),
            [(60010, "TCP")],
        )

    def test_explicit_both(self):
        self.assertEqual(
            upnp.parse_port_spec("60010/both", default_proto="tcp"),
            [(60010, "TCP"), (60010, "UDP")],
        )

    def test_range(self):
        self.assertEqual(
            upnp.parse_port_spec("60010-60012/tcp", default_proto="udp"),
            [(60010, "TCP"), (60011, "TCP"), (60012, "TCP")],
        )

    def test_invalid_port_number(self):
        with self.assertRaises(ValueError):
            upnp.parse_port_spec("70000", default_proto="both")

    def test_invalid_protocol(self):
        with self.assertRaises(ValueError):
            upnp.parse_port_spec("60010/sctp", default_proto="both")

    def test_inverted_range_rejected(self):
        with self.assertRaises(ValueError):
            upnp.parse_port_spec("60012-60010", default_proto="tcp")

    def test_expand_multiple_specs_dedupes(self):
        self.assertEqual(
            upnp.expand_port_specs(["60010/tcp", "60010/both"], default_proto="tcp"),
            [(60010, "TCP"), (60010, "UDP")],
        )


SAMPLE_SSDP_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"CACHE-CONTROL: max-age=120\r\n"
    b"DATE: Sat, 01 Jan 2026 00:00:00 GMT\r\n"
    b"EXT:\r\n"
    b"LOCATION: http://192.168.0.1:1900/fqxbs/rootDesc.xml\r\n"
    b"SERVER: Linux/3.4 UPnP/1.0 MiniUPnPd/1.9\r\n"
    b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    b"USN: uuid:1234-5678::urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    b"\r\n"
)


class TestSSDPParse(unittest.TestCase):
    def test_parses_headers(self):
        result = upnp.parse_ssdp_response(SAMPLE_SSDP_RESPONSE)
        self.assertEqual(result["status"], "200")
        self.assertEqual(result["location"],
                         "http://192.168.0.1:1900/fqxbs/rootDesc.xml")
        self.assertEqual(result["st"],
                         "urn:schemas-upnp-org:device:InternetGatewayDevice:1")
        self.assertEqual(result["server"], "Linux/3.4 UPnP/1.0 MiniUPnPd/1.9")

    def test_headers_are_case_insensitive(self):
        raw = b"HTTP/1.1 200 OK\r\nlocation: http://x/y\r\n\r\n"
        result = upnp.parse_ssdp_response(raw)
        self.assertEqual(result["location"], "http://x/y")

    def test_rejects_non_200(self):
        raw = b"HTTP/1.1 404 Not Found\r\n\r\n"
        with self.assertRaises(ValueError):
            upnp.parse_ssdp_response(raw)

    def test_rejects_missing_location(self):
        raw = b"HTTP/1.1 200 OK\r\nST: x\r\n\r\n"
        with self.assertRaises(ValueError):
            upnp.parse_ssdp_response(raw)


class TestSSDPNetwork(unittest.TestCase):
    """Integration tests — skipped unless UPNP_TEST_HOST is set."""

    def test_msearch_real_router(self):
        host = os.environ.get("UPNP_TEST_HOST")
        if not host:
            self.skipTest("UPNP_TEST_HOST not set")
        responses = upnp.ssdp_msearch(host, timeout=3.0)
        self.assertTrue(responses, f"no SSDP response from {host}")
        self.assertIn("location", responses[0])


SAMPLE_ROOT_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>Test Router</friendlyName>
    <manufacturer>TestCo</manufacturer>
    <modelName>TR-1000</modelName>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <serviceId>urn:upnp-org:serviceId:WANIPConn1</serviceId>
                <SCPDURL>/scpd/WANIPConn.xml</SCPDURL>
                <controlURL>/ctl/IPConn</controlURL>
                <eventSubURL>/evt/IPConn</eventSubURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>"""


class TestRootDesc(unittest.TestCase):
    def test_parses_device_info(self):
        rd = upnp.parse_root_desc(SAMPLE_ROOT_DESC,
                                  base_url="http://192.168.0.1:1900/fqxbs/rootDesc.xml")
        self.assertEqual(rd.friendly_name, "Test Router")
        self.assertEqual(rd.manufacturer, "TestCo")
        self.assertEqual(rd.model_name, "TR-1000")

    def test_finds_wan_ip_connection(self):
        rd = upnp.parse_root_desc(SAMPLE_ROOT_DESC,
                                  base_url="http://192.168.0.1:1900/fqxbs/rootDesc.xml")
        svc = upnp.find_wan_service(rd)
        self.assertEqual(svc.service_type,
                         "urn:schemas-upnp-org:service:WANIPConnection:1")
        self.assertEqual(svc.control_url,
                         "http://192.168.0.1:1900/ctl/IPConn")

    def test_missing_wan_service_raises(self):
        no_wan = SAMPLE_ROOT_DESC.replace(
            "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:Other:1",
        )
        rd = upnp.parse_root_desc(no_wan,
                                  base_url="http://192.168.0.1:1900/fqxbs/rootDesc.xml")
        with self.assertRaises(LookupError):
            upnp.find_wan_service(rd)


SAMPLE_SOAP_OK = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetExternalIPAddressResponse
        xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
      <NewExternalIPAddress>1.2.3.4</NewExternalIPAddress>
    </u:GetExternalIPAddressResponse>
  </s:Body>
</s:Envelope>"""

SAMPLE_SOAP_FAULT = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <s:Fault>
      <faultcode>s:Client</faultcode>
      <faultstring>UPnPError</faultstring>
      <detail>
        <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
          <errorCode>718</errorCode>
          <errorDescription>ConflictInMappingEntry</errorDescription>
        </UPnPError>
      </detail>
    </s:Fault>
  </s:Body>
</s:Envelope>"""


class TestSoapEnvelope(unittest.TestCase):
    def test_envelope_structure(self):
        env = upnp.build_soap_envelope(
            service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
            action="GetExternalIPAddress",
            args={},
        )
        self.assertIn("<s:Envelope", env)
        self.assertIn("<u:GetExternalIPAddress", env)
        self.assertIn(
            'xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1"',
            env,
        )

    def test_envelope_with_args_preserves_order(self):
        env = upnp.build_soap_envelope(
            service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
            action="AddPortMapping",
            args={
                "NewRemoteHost": "",
                "NewExternalPort": "60010",
                "NewProtocol": "TCP",
                "NewInternalPort": "60010",
                "NewInternalClient": "10.0.0.10",
                "NewEnabled": "1",
                "NewPortMappingDescription": "test",
                "NewLeaseDuration": "0",
            },
        )
        i_remote = env.index("<NewRemoteHost>")
        i_ext = env.index("<NewExternalPort>")
        i_proto = env.index("<NewProtocol>")
        self.assertLess(i_remote, i_ext)
        self.assertLess(i_ext, i_proto)

    def test_envelope_escapes_xml(self):
        env = upnp.build_soap_envelope(
            service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
            action="AddPortMapping",
            args={"NewPortMappingDescription": "a & b <c>"},
        )
        self.assertIn("a &amp; b &lt;c&gt;", env)
        self.assertNotIn("a & b <c>", env)


class TestSoapResponse(unittest.TestCase):
    def test_parses_success(self):
        result = upnp.parse_soap_response(SAMPLE_SOAP_OK, "GetExternalIPAddress")
        self.assertEqual(result, {"NewExternalIPAddress": "1.2.3.4"})

    def test_parses_fault(self):
        with self.assertRaises(upnp.SoapFault) as cm:
            upnp.parse_soap_response(SAMPLE_SOAP_FAULT, "AddPortMapping")
        self.assertEqual(cm.exception.code, 718)
        self.assertEqual(cm.exception.description, "ConflictInMappingEntry")


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / ".upnp-cache.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_load_missing_returns_empty(self):
        self.assertEqual(upnp.cache_load(self.path), {})

    def test_put_then_get(self):
        upnp.cache_put(self.path, "192.168.0.1",
                       root_desc_url="http://x/rootDesc.xml",
                       control_url="http://x/ctl",
                       service_type="svc:1",
                       ttl_seconds=3600)
        entry = upnp.cache_get(self.path, "192.168.0.1", now=0)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["control_url"], "http://x/ctl")

    def test_stale_entry_returns_none(self):
        upnp.cache_put(self.path, "192.168.0.1",
                       root_desc_url="u", control_url="c",
                       service_type="s", ttl_seconds=60,
                       fetched_at=1000)
        self.assertIsNone(upnp.cache_get(self.path, "192.168.0.1", now=2000))

    def test_invalidate_removes_entry(self):
        upnp.cache_put(self.path, "192.168.0.1",
                       root_desc_url="u", control_url="c",
                       service_type="s", ttl_seconds=3600)
        upnp.cache_invalidate(self.path, "192.168.0.1")
        self.assertIsNone(upnp.cache_get(self.path, "192.168.0.1", now=0))

    def test_corrupt_cache_is_renamed(self):
        self.path.write_text("not json{{{", encoding="utf-8")
        self.assertEqual(upnp.cache_load(self.path), {})
        self.assertTrue((self.path.parent / ".upnp-cache.json.bad").exists())


if __name__ == "__main__":
    unittest.main()
