"""The Host must offer addresses the phone can actually reach."""
import ipaddress
import unittest
from unittest.mock import patch

from remotevoice import network_addresses


def adapter(description='Realtek Ethernet', type=6, gateway='10.20.30.1',
            addresses=('10.20.30.2',)):
    return dict(description=description, type=type, gateway=gateway,
                addresses=list(addresses))


class PrivateAddressTests(unittest.TestCase):
    def test_physical_adapter_address_is_offered(self):
        rows = [adapter(description='MediaTek Wi-Fi 6E', type=71,
                        addresses=['10.20.30.2', '0.0.0.0'])]
        with patch.object(network_addresses, '_adapter_rows', return_value=rows):
            self.assertEqual(network_addresses.private_ipv4_addresses(), ['10.20.30.2'])

    def test_vmware_and_bluetooth_adapters_are_skipped(self):
        rows = [
            adapter(description='VMware Virtual Ethernet Adapter for VMnet1',
                    gateway='0.0.0.0', addresses=['10.10.0.1']),
            adapter(description='VMware Virtual Ethernet Adapter for VMnet8',
                    gateway='0.0.0.0', addresses=['10.20.0.1']),
            adapter(description='Bluetooth Device (Personal Area Network)',
                    gateway='0.0.0.0', addresses=['192.168.1.5']),
            adapter(description='MediaTek Wi-Fi 6E', type=71,
                    addresses=['10.20.30.2']),
        ]
        with patch.object(network_addresses, '_adapter_rows', return_value=rows):
            self.assertEqual(network_addresses.private_ipv4_addresses(), ['10.20.30.2'])

    def test_gateway_adapter_is_listed_before_hotspot_adapter(self):
        rows = [
            adapter(description='Microsoft Wi-Fi Direct Virtual Adapter', type=71,
                    gateway='0.0.0.0', addresses=['192.168.137.1']),
            adapter(description='MediaTek Wi-Fi 6E', type=71,
                    addresses=['10.20.30.2']),
        ]
        with patch.object(network_addresses, '_adapter_rows', return_value=rows):
            self.assertEqual(network_addresses.private_ipv4_addresses(),
                             ['10.20.30.2', '192.168.137.1'])

    def test_unrelated_types_and_public_addresses_are_ignored(self):
        rows = [
            adapter(description='TAP-Windows Adapter V9', type=53, addresses=['10.8.0.2']),
            adapter(description='Realtek Ethernet', gateway='0.0.0.0', addresses=['8.8.8.8']),
            adapter(description='Realtek Ethernet', gateway='0.0.0.0', addresses=['0.0.0.0']),
        ]
        with patch.object(network_addresses, '_adapter_rows', return_value=rows), \
             patch.object(network_addresses, 'hostname_private_ipv4_addresses',
                          return_value=['10.20.30.2']):
            self.assertEqual(network_addresses.private_ipv4_addresses(), ['10.20.30.2'])

    def test_api_failure_falls_back_to_hostname_list(self):
        with patch.object(network_addresses, '_adapter_rows', side_effect=OSError('helper')), \
             patch.object(network_addresses, 'hostname_private_ipv4_addresses',
                          return_value=['10.0.0.5']):
            self.assertEqual(network_addresses.private_ipv4_addresses(), ['10.0.0.5'])

    def test_windows_adapter_table_is_readable(self):
        try:
            rows = network_addresses._adapter_rows()
        except (AttributeError, OSError):
            self.skipTest('Windows adapter API unavailable')
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertIn('description', row)
            self.assertIn('type', row)
            self.assertIsInstance(row['addresses'], list)

    def test_live_addresses_are_private_ipv4(self):
        for text in network_addresses.private_ipv4_addresses():
            address = ipaddress.ip_address(text)
            self.assertTrue(address.is_private)


if __name__ == '__main__':
    unittest.main()
