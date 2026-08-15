import unittest

from proxy_utils import ProxyFormatError, normalize_proxy, proxy_mapping


class NormalizeProxyTests(unittest.TestCase):
    def test_host_and_port(self):
        self.assertEqual(normalize_proxy("127.0.0.1:8080"), "http://127.0.0.1:8080")

    def test_colon_credentials(self):
        self.assertEqual(
            normalize_proxy("proxy.example:3128:user:p@ss"),
            "http://user:p%40ss@proxy.example:3128",
        )

    def test_url_credentials(self):
        self.assertEqual(
            normalize_proxy("socks5://user:pass@proxy.example:1080"),
            "socks5://user:pass@proxy.example:1080",
        )

    def test_mapping_uses_proxy_for_both_protocols(self):
        proxy = "http://proxy.example:8080"
        self.assertEqual(proxy_mapping(proxy), {"http": proxy, "https": proxy})

    def test_rejects_invalid_port(self):
        with self.assertRaises(ProxyFormatError):
            normalize_proxy("proxy.example:99999")

    def test_rejects_path(self):
        with self.assertRaises(ProxyFormatError):
            normalize_proxy("http://proxy.example:8080/path")

    def test_rejects_oversized_value(self):
        with self.assertRaises(ProxyFormatError):
            normalize_proxy("a" * 2049)


if __name__ == "__main__":
    unittest.main()
