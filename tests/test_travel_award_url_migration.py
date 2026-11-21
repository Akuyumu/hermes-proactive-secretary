import unittest
from proactive_pilot.publicfeeds import UNIVERSITY_TRAVEL_AWARD_URL, URLRejected, validate_public_url

BASE = 'https://www.university.example/admissions/undergraduate/fees-and-funding/financial-aid/'
NEW = BASE + 'travel-award-overseas-student-programme-exchange'
OLD = BASE + 'overseas-student-programme-loan-travel-award'


def test_canonical_source_is_exact_owner_approved_replacement():
    assert UNIVERSITY_TRAVEL_AWARD_URL == NEW
    validate_public_url(NEW)


def test_old_redirect_entry_remains_narrowly_allowed():
    validate_public_url(OLD)


class MigrationScopeTests(unittest.TestCase):
    def test_migration_does_not_allow_subtrees_or_other_hosts(self):
        for url in [NEW + '/unreviewed', BASE + 'unreviewed-award',
                    NEW.replace('www.university.example', 'example.com')]:
            with self.subTest(url=url), self.assertRaises(URLRejected):
                validate_public_url(url)
