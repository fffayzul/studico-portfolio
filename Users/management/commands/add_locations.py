"""
Management command to add UK university city locations with their regions.
Skips any locations/regions that already exist.

Usage:
    python manage.py add_locations
    python manage.py add_locations --dry-run   # preview without saving
"""

from django.core.management.base import BaseCommand
from Users.models import Country, Region, Location


# ── Region → Locations mapping ──────────────────────────────────────────
# Existing regions: "The South", "The Midlands", "The North"
# New regions added: "Wales", "Scotland", "Northern Ireland", "East of England",
#                    "South West", "East Midlands", "West Midlands",
#                    "Yorkshire and the Humber", "North West", "North East"

LOCATIONS = {
    # ── The South (existing) ────────────────────────────────────────────
    "The South": [
        # Already in DB
        "Southampton", "Canterbury", "Oxford", "Brighton", "Portsmouth",
        "Bournemouth", "Bath", "Milton Keynes",
        "North London", "South London", "East London", "West London",
        "Central London",
        # New
        "Guildford", "Reading", "Winchester", "Chichester",
        "St Albans", "Hatfield", "Egham", "Kingston upon Thames",
        "Uxbridge", "Greenwich", "Bloomsbury", "Kensington",
        "Roehampton", "Twickenham",
    ],

    # ── The Midlands (existing) ─────────────────────────────────────────
    "The Midlands": [
        # Already in DB
        "Birmingham", "Coventry", "Wolverhampton", "Derby",
        "Leicester", "Nottingham",
        # New
        "Stoke-on-Trent", "Loughborough", "Warwick", "Lincoln",
        "Worcester", "Stafford", "Northampton", "Keele",
        "Telford", "Hereford",
    ],

    # ── The North (existing) ────────────────────────────────────────────
    "The North": [
        # Already in DB
        "Liverpool", "Leeds", "York",
        # New
        "Manchester", "Sheffield", "Newcastle upon Tyne", "Durham",
        "Lancaster", "Bradford", "Huddersfield", "Hull",
        "Sunderland", "Middlesbrough", "Preston", "Blackburn",
        "Chester", "Salford", "Bolton", "Carlisle",
        "Northumbria", "Teesside",
    ],

    # ── Wales ───────────────────────────────────────────────────────────
    "Wales": [
        # Already in DB
        "Cardiff",
        # New
        "Swansea", "Bangor", "Aberystwyth", "Newport",
        "Wrexham", "Lampeter", "Carmarthen", "Pontypridd",
    ],

    # ── Scotland ────────────────────────────────────────────────────────
    "Scotland": [
        "Edinburgh", "Glasgow", "Aberdeen", "Dundee",
        "St Andrews", "Stirling", "Inverness",
    ],

    # ── Northern Ireland ────────────────────────────────────────────────
    "Northern Ireland": [
        # Already in DB
        "Belfast",
        # New
        "Derry", "Coleraine", "Jordanstown",
    ],

    # ── South West ──────────────────────────────────────────────────────
    "South West": [
        # Already in DB
        "Plymouth",
        # New
        "Bristol", "Exeter", "Gloucester", "Cheltenham",
        "Falmouth", "Swindon", "Taunton", "Torquay",
    ],

    # ── East of England ─────────────────────────────────────────────────
    "East of England": [
        "Cambridge", "Norwich", "Colchester", "Ipswich",
        "Chelmsford", "Luton", "Bedford", "Peterborough",
        "Southend-on-Sea",
    ],
}


class Command(BaseCommand):
    help = "Add UK university city locations and regions to the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be created without saving to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        regions_created = 0
        locations_created = 0
        locations_skipped = 0

        gb = Country.objects.filter(code="GB").first()

        for region_name, location_names in LOCATIONS.items():
            # Get or create region
            if dry_run:
                region = Region.objects.filter(region=region_name).first()
                if region is None:
                    self.stdout.write(f"  [NEW REGION] {region_name}")
                    regions_created += 1
            else:
                region, created = Region.objects.get_or_create(
                    region=region_name,
                    defaults={"country": gb},
                )
                if created:
                    regions_created += 1
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Created region: {region_name}"))

            for loc_name in location_names:
                exists = Location.objects.filter(location=loc_name).exists()
                if exists:
                    locations_skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f"  [NEW] {loc_name} → {region_name}")
                else:
                    Location.objects.create(location=loc_name, region=region)
                    self.stdout.write(f"    + {loc_name}")
                locations_created += 1

        # Summary
        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — "
                f"{regions_created} new regions, "
                f"{locations_created} new locations added, "
                f"{locations_skipped} already existed (skipped)"
            )
        )
