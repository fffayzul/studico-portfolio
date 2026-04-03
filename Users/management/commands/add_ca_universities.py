"""
Management command to add Canadian universities to the database.
Requires the 'CA' country to exist — run add_countries first.

Usage:
    python manage.py add_ca_universities
    python manage.py add_ca_universities --dry-run
"""

from django.core.management.base import BaseCommand
from Users.models import Country, University


UNIVERSITIES = [
    # ── U15 Group (research-intensive) ────────────────────────────────────
    "University of Toronto",
    "University of British Columbia",
    "McGill University",
    "University of Alberta",
    "Université de Montréal",
    "University of Calgary",
    "University of Ottawa",
    "University of Waterloo",
    "Western University",
    "Queen's University",
    "McMaster University",
    "Dalhousie University",
    "University of Manitoba",
    "University of Saskatchewan",
    "Université Laval",

    # ── Other major universities ──────────────────────────────────────────
    "Simon Fraser University",
    "University of Victoria",
    "York University",
    "Carleton University",
    "Concordia University",
    "Toronto Metropolitan University",
    "University of Guelph",
    "University of Windsor",
    "Wilfrid Laurier University",
    "Brock University",
    "University of Regina",
    "University of New Brunswick",
    "Memorial University of Newfoundland",
    "University of Prince Edward Island",
    "Acadia University",
    "Saint Mary's University",
    "Mount Allison University",
    "St. Francis Xavier University",
    "Lakehead University",
    "Laurentian University",
    "Trent University",
    "University of Lethbridge",
    "University of Northern British Columbia",
    "Thompson Rivers University",
    "Kwantlen Polytechnic University",
    "University of the Fraser Valley",
    "Cape Breton University",
    "Brandon University",
    "Athabasca University",
    "Royal Roads University",
    "Ontario Tech University",
    "MacEwan University",
    "Mount Royal University",
    "University of Winnipeg",
    "Nipissing University",
    "University of Ontario Institute of Technology",

    # ── Colleges with university status ───────────────────────────────────
    "OCAD University",
    "Emily Carr University of Art + Design",
    "University of the Arts",
    "Sheridan College",
    "Humber College",
    "George Brown College",
    "Seneca Polytechnic",
    "British Columbia Institute of Technology",
]


class Command(BaseCommand):
    help = "Add Canadian universities to the database"

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

        created_count = 0
        skipped_count = 0

        for name in UNIVERSITIES:
            exists = University.objects.filter(university=name, country=ca).exists()
            if exists:
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(f"  [NEW] {name}")
            else:
                University.objects.create(university=name, country=ca)
                self.stdout.write(f"  + {name}")
            created_count += 1

        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — {created_count} new Canadian universities added, "
                f"{skipped_count} already existed (skipped). "
                f"Total in list: {len(UNIVERSITIES)}"
            )
        )
