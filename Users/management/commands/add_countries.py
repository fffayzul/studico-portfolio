"""
Management command to seed supported countries.

Usage:
    python manage.py add_countries
    python manage.py add_countries --dry-run
"""

from django.core.management.base import BaseCommand
from Users.models import Country


COUNTRIES = [
    {"name": "United Kingdom", "code": "GB", "allowed_email_domains": [".ac.uk"]},
    {"name": "United States", "code": "US", "allowed_email_domains": [".edu"]},
    {"name": "Canada", "code": "CA", "allowed_email_domains": [".ca", ".edu"]},
]


class Command(BaseCommand):
    help = "Add supported countries to the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be created without saving to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        created_count = 0
        skipped_count = 0

        for entry in COUNTRIES:
            exists = Country.objects.filter(code=entry["code"]).exists()
            if exists:
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(f"  [NEW] {entry['name']} ({entry['code']}) — domains: {entry['allowed_email_domains']}")
            else:
                Country.objects.create(
                    name=entry["name"],
                    code=entry["code"],
                    allowed_email_domains=entry["allowed_email_domains"],
                )
                self.stdout.write(f"  + {entry['name']} ({entry['code']})")
            created_count += 1

        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — {created_count} new countries added, "
                f"{skipped_count} already existed (skipped)"
            )
        )
