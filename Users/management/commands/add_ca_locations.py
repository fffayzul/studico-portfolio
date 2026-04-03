"""
Management command to add Canadian locations (provinces as regions, major university cities as locations).
Requires the 'CA' country to exist — run add_countries first.

Usage:
    python manage.py add_ca_locations
    python manage.py add_ca_locations --dry-run
"""

from django.core.management.base import BaseCommand
from Users.models import Country, Region, Location


# Province/Territory → major university cities
LOCATIONS = {
    "Ontario": [
        "Toronto", "Ottawa", "Hamilton", "Waterloo", "London",
        "Kingston", "Guelph", "Windsor", "Thunder Bay",
        "St. Catharines", "Oshawa", "Peterborough", "Sudbury",
        "North Bay", "Mississauga",
    ],
    "Quebec": [
        "Montreal", "Quebec City", "Sherbrooke", "Trois-Rivières",
        "Gatineau",
    ],
    "British Columbia": [
        "Vancouver", "Victoria", "Burnaby", "Kelowna",
        "Kamloops", "Prince George", "Surrey",
    ],
    "Alberta": [
        "Edmonton", "Calgary", "Lethbridge", "Red Deer",
    ],
    "Manitoba": [
        "Winnipeg", "Brandon",
    ],
    "Saskatchewan": [
        "Saskatoon", "Regina",
    ],
    "Nova Scotia": [
        "Halifax", "Wolfville", "Antigonish", "Sydney",
    ],
    "New Brunswick": [
        "Fredericton", "Moncton", "Saint John",
    ],
    "Newfoundland and Labrador": [
        "St. John's", "Corner Brook",
    ],
    "Prince Edward Island": [
        "Charlottetown",
    ],
    "Northwest Territories": [
        "Yellowknife",
    ],
    "Yukon": [
        "Whitehorse",
    ],
    "Nunavut": [
        "Iqaluit",
    ],
}


class Command(BaseCommand):
    help = "Add Canadian provinces (as regions) and university cities (as locations)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be created without saving to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        try:
            ca = Country.objects.get(code="CA")
        except Country.DoesNotExist:
            self.stderr.write(self.style.ERROR(
                "Country 'CA' not found. Run 'python manage.py add_countries' first."
            ))
            return

        regions_created = 0
        locations_created = 0
        locations_skipped = 0

        for province_name, city_names in LOCATIONS.items():
            if dry_run:
                region = Region.objects.filter(region=province_name, country=ca).first()
                if region is None:
                    self.stdout.write(f"  [NEW REGION] {province_name}")
                    regions_created += 1
            else:
                region, created = Region.objects.get_or_create(
                    region=province_name, country=ca,
                )
                if created:
                    regions_created += 1
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Created region: {province_name}"))

            for city in city_names:
                exists = Location.objects.filter(location=city, region=region).exists()
                if exists:
                    locations_skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f"  [NEW] {city} → {province_name}")
                else:
                    Location.objects.create(location=city, region=region)
                    self.stdout.write(f"    + {city}")
                locations_created += 1

        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — {regions_created} new regions (provinces), "
                f"{locations_created} new locations added, "
                f"{locations_skipped} already existed (skipped)"
            )
        )
