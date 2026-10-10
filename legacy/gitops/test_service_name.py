import re
import unittest

from service_name import service_name


class ServiceNameTests(unittest.TestCase):
    def test_stable_scoped_names_and_dns_boundary(self):
        first = service_name('My Chat', 'demo', 'production', 'railshot.io')
        self.assertEqual(first, service_name('My Chat', 'demo', 'production', 'railshot.io'))
        self.assertNotEqual(first['label'], service_name('My Chat', 'other', 'production', 'railshot.io')['label'])
        self.assertNotEqual(first['label'], service_name('My Chat', 'demo', 'preview', 'railshot.io')['label'])
        for name in ('My Chat', '채팅 앱', '../bad.$(name)', 'a' * 256):
            label = service_name(name, 'demo', 'production', 'railshot.io')['label']
            self.assertLessEqual(len(label), 63)
            self.assertRegex(label, r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])$')
        for domain in ('evil.example', 'railshot.io/path', 'railshot.io.evil', '*.railshot.io'):
            with self.assertRaises(ValueError):
                service_name('app', 'demo', 'production', domain)


if __name__ == '__main__':
    unittest.main()
