"""Parent regression: reviewed scholarship paths must not be prefixes."""
import unittest
from proactive_pilot.publicfeeds import validate_public_url, URLRejected


class ReviewedUniversityPaths(unittest.TestCase):
    def test_reviewed_pages_exact_only(self):
        base = 'https://www.university.example/admissions/undergraduate/scholarships-and-awards/'
        for name in ('scholarship-application-process', 'scholarship-opportunities'):
            self.assertEqual(validate_public_url(base + name).path, '/admissions/undergraduate/scholarships-and-awards/' + name)
            for suffix in ('-unreviewed', '/unreviewed'):
                with self.subTest(name=name, suffix=suffix):
                    with self.assertRaises(URLRejected):
                        validate_public_url(base + name + suffix)

    def test_existing_news_prefix_unchanged(self):
        self.assertEqual(validate_public_url('https://www.university.example/news/news-detail/example').path, '/news/news-detail/example')
