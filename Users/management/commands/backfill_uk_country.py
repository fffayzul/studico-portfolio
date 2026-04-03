"""
Management command to backfill existing University, Region, and Student records
with the UK (GB) country. Run add_countries first.

Usage:
    python manage.py backfill_uk_country
    python manage.py backfill_uk_country --dry-run
"""

from django.core.management.base import BaseCommand
from Users.models import Country, University, Region, Student


class Command(BaseCommand):
    help = "Set country=GB on all existing University, Region, and Student records that have no country"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be updated without saving",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        try:
            gb = Country.objects.get(code="GB")
        except Country.DoesNotExist:
            self.stderr.write(self.style.ERROR(
                "Country 'GB' not found. Run 'python manage.py add_countries' first."
            ))
            return

        unis = University.objects.filter(country__isnull=True)
        regions = Region.objects.filter(country__isnull=True)
        students = Student.objects.filter(country__isnull=True)

        uni_count = unis.count()
        region_count = regions.count()
        student_count = students.count()

        if dry_run:
            self.stdout.write(f"  [DRY RUN] Would set country=GB on {uni_count} universities")
            self.stdout.write(f"  [DRY RUN] Would set country=GB on {region_count} regions")
            self.stdout.write(f"  [DRY RUN] Would set country=GB on {student_count} students")
        else:
            unis.update(country=gb)
            self.stdout.write(f"  + Updated {uni_count} universities → GB")
            regions.update(country=gb)
            self.stdout.write(f"  + Updated {region_count} regions → GB")
            students.update(country=gb)
            self.stdout.write(f"  + Updated {student_count} students → GB")

        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — backfilled {uni_count} universities, "
                f"{region_count} regions, {student_count} students with country=GB"
            )
        )
