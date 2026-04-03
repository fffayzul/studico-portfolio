"""
Management command to add US locations (states as regions, major university cities as locations).
Requires the 'US' country to exist — run add_countries first.

Usage:
    python manage.py add_us_locations
    python manage.py add_us_locations --dry-run
"""

from django.core.management.base import BaseCommand
from Users.models import Country, Region, Location


# State → major university cities
LOCATIONS = {
    "Alabama": ["Tuscaloosa", "Birmingham", "Auburn", "Huntsville"],
    "Alaska": ["Anchorage", "Fairbanks"],
    "Arizona": ["Tempe", "Tucson", "Phoenix", "Flagstaff"],
    "Arkansas": ["Fayetteville", "Little Rock"],
    "California": [
        "Los Angeles", "Berkeley", "San Diego", "San Francisco",
        "Stanford", "Davis", "Santa Barbara", "Irvine",
        "Santa Cruz", "Riverside", "San Jose", "Sacramento",
        "Pasadena", "Claremont", "Malibu",
    ],
    "Colorado": ["Boulder", "Denver", "Fort Collins", "Colorado Springs"],
    "Connecticut": ["New Haven", "Hartford", "Storrs"],
    "Delaware": ["Newark", "Wilmington"],
    "Florida": [
        "Gainesville", "Miami", "Tallahassee", "Orlando",
        "Tampa", "Jacksonville", "Boca Raton",
    ],
    "Georgia": ["Atlanta", "Athens", "Savannah", "Augusta"],
    "Hawaii": ["Honolulu"],
    "Idaho": ["Moscow", "Boise"],
    "Illinois": ["Chicago", "Champaign", "Evanston", "Urbana", "Springfield"],
    "Indiana": ["Bloomington", "West Lafayette", "Indianapolis", "Notre Dame"],
    "Iowa": ["Iowa City", "Ames", "Des Moines"],
    "Kansas": ["Lawrence", "Manhattan", "Wichita"],
    "Kentucky": ["Lexington", "Louisville"],
    "Louisiana": ["Baton Rouge", "New Orleans"],
    "Maine": ["Orono", "Portland"],
    "Maryland": ["College Park", "Baltimore", "Annapolis"],
    "Massachusetts": [
        "Boston", "Cambridge", "Amherst", "Worcester",
        "Medford", "Waltham",
    ],
    "Michigan": ["Ann Arbor", "East Lansing", "Detroit"],
    "Minnesota": ["Minneapolis", "Saint Paul"],
    "Mississippi": ["Oxford", "Starkville", "Jackson"],
    "Missouri": ["Columbia", "St. Louis", "Kansas City"],
    "Montana": ["Missoula", "Bozeman"],
    "Nebraska": ["Lincoln", "Omaha"],
    "Nevada": ["Las Vegas", "Reno"],
    "New Hampshire": ["Durham", "Hanover"],
    "New Jersey": ["New Brunswick", "Princeton", "Newark"],
    "New Mexico": ["Albuquerque", "Las Cruces", "Santa Fe"],
    "New York": [
        "New York City", "Ithaca", "Buffalo", "Syracuse",
        "Albany", "Stony Brook", "Rochester",
    ],
    "North Carolina": [
        "Chapel Hill", "Durham", "Raleigh", "Charlotte",
        "Greensboro", "Winston-Salem",
    ],
    "North Dakota": ["Grand Forks", "Fargo"],
    "Ohio": ["Columbus", "Cleveland", "Cincinnati", "Athens"],
    "Oklahoma": ["Norman", "Stillwater", "Oklahoma City", "Tulsa"],
    "Oregon": ["Eugene", "Portland", "Corvallis"],
    "Pennsylvania": [
        "Philadelphia", "Pittsburgh", "State College",
        "University Park", "Lancaster",
    ],
    "Rhode Island": ["Providence"],
    "South Carolina": ["Columbia", "Charleston", "Clemson", "Greenville"],
    "South Dakota": ["Vermillion", "Brookings"],
    "Tennessee": ["Nashville", "Knoxville", "Memphis"],
    "Texas": [
        "Austin", "Houston", "Dallas", "San Antonio",
        "College Station", "Fort Worth", "El Paso", "Lubbock",
    ],
    "Utah": ["Salt Lake City", "Provo"],
    "Vermont": ["Burlington"],
    "Virginia": [
        "Charlottesville", "Blacksburg", "Richmond",
        "Williamsburg", "Norfolk",
    ],
    "Washington": ["Seattle", "Pullman", "Tacoma"],
    "West Virginia": ["Morgantown", "Charleston"],
    "Wisconsin": ["Madison", "Milwaukee"],
    "Wyoming": ["Laramie"],
    "District of Columbia": ["Washington, D.C."],
}


class Command(BaseCommand):
    help = "Add US states (as regions) and university cities (as locations)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be created without saving to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        try:
            us = Country.objects.get(code="US")
        except Country.DoesNotExist:
            self.stderr.write(self.style.ERROR(
                "Country 'US' not found. Run 'python manage.py add_countries' first."
            ))
            return

        regions_created = 0
        locations_created = 0
        locations_skipped = 0

        for state_name, city_names in LOCATIONS.items():
            if dry_run:
                region = Region.objects.filter(region=state_name, country=us).first()
                if region is None:
                    self.stdout.write(f"  [NEW REGION] {state_name}")
                    regions_created += 1
            else:
                region, created = Region.objects.get_or_create(
                    region=state_name, country=us,
                )
                if created:
                    regions_created += 1
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Created region: {state_name}"))

            for city in city_names:
                exists = Location.objects.filter(location=city, region=region).exists()
                if exists:
                    locations_skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f"  [NEW] {city} → {state_name}")
                else:
                    Location.objects.create(location=city, region=region)
                    self.stdout.write(f"    + {city}")
                locations_created += 1

        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — {regions_created} new regions (states), "
                f"{locations_created} new locations added, "
                f"{locations_skipped} already existed (skipped)"
            )
        )
