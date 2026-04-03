"""
Management command to add UK universities to the database.
Skips any universities that already exist (matched by name).

Usage:
    python manage.py add_universities
    python manage.py add_universities --dry-run   # preview without saving
"""

from django.core.management.base import BaseCommand
from Users.models import Country, University


UNIVERSITIES = [
    # ── Russell Group (24) ────────────────────────────────────────────────
    "University of Birmingham",
    "University of Bristol",
    "University of Cambridge",
    "Cardiff University",
    "Durham University",
    "University of Edinburgh",
    "University of Exeter",
    "University of Glasgow",
    "Imperial College London",
    "King's College London",
    "University of Leeds",
    "University of Liverpool",
    "London School of Economics and Political Science",
    "University of Manchester",
    "Newcastle University",
    "University of Nottingham",
    "University of Oxford",
    "Queen Mary University of London",
    "Queen's University Belfast",
    "University of Sheffield",
    "University of Southampton",
    "University College London",
    "University of Warwick",
    "University of York",

    # ── Other major universities ──────────────────────────────────────────
    "Aberystwyth University",
    "Anglia Ruskin University",
    "Arts University Bournemouth",
    "Aston University",
    "Bangor University",
    "Bath Spa University",
    "University of Bath",
    "University of Bedfordshire",
    "Birmingham City University",
    "University of Bolton",
    "University of Bournemouth",
    "University of Bradford",
    "University of Brighton",
    "Brunel University London",
    "University of Buckingham",
    "Buckinghamshire New University",
    "Canterbury Christ Church University",
    "University of Central Lancashire",
    "University of Chester",
    "University of Chichester",
    "City, University of London",
    "Coventry University",
    "Cranfield University",
    "University of Cumbria",
    "De Montfort University",
    "University of Derby",
    "University of Dundee",
    "University of East Anglia",
    "University of East London",
    "Edge Hill University",
    "Edinburgh Napier University",
    "University of Essex",
    "Falmouth University",
    "University of Gloucestershire",
    "Goldsmiths, University of London",
    "University of Greenwich",
    "Harper Adams University",
    "Heriot-Watt University",
    "University of Hertfordshire",
    "University of the Highlands and Islands",
    "University of Huddersfield",
    "University of Hull",
    "Keele University",
    "University of Kent",
    "Kingston University",
    "Lancaster University",
    "University of Law",
    "Leeds Beckett University",
    "Leeds Trinity University",
    "University of Leicester",
    "University of Lincoln",
    "Liverpool Hope University",
    "Liverpool John Moores University",
    "London Metropolitan University",
    "London South Bank University",
    "Loughborough University",
    "Manchester Metropolitan University",
    "Middlesex University",
    "Newcastle University London",
    "University of Northampton",
    "Northumbria University",
    "Norwich University of the Arts",
    "Nottingham Trent University",
    "Open University",
    "Oxford Brookes University",
    "University of Plymouth",
    "University of Portsmouth",
    "University of Reading",
    "Robert Gordon University",
    "University of Roehampton",
    "Royal Holloway, University of London",
    "University of Salford",
    "Sheffield Hallam University",
    "SOAS University of London",
    "Solent University",
    "University of South Wales",
    "University of St Andrews",
    "St George's, University of London",
    "St Mary's University, Twickenham",
    "Staffordshire University",
    "University of Stirling",
    "University of Strathclyde",
    "University of Suffolk",
    "University of Sunderland",
    "University of Surrey",
    "University of Sussex",
    "Swansea University",
    "Teesside University",
    "Ulster University",
    "University of the Arts London",
    "University of the West of England",
    "University of the West of Scotland",
    "University of Wales Trinity Saint David",
    "University of West London",
    "University of Westminster",
    "University of Winchester",
    "University of Wolverhampton",
    "University of Worcester",
    "Wrexham University",
    "University of York St John",

    # ── Specialist / smaller institutions ─────────────────────────────────
    "Birkbeck, University of London",
    "Glasgow Caledonian University",
    "Queen Margaret University",
    "Ravensbourne University London",
    "University for the Creative Arts",
    "University of Aberdeen",
]


class Command(BaseCommand):
    help = "Add UK universities to the database"

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

        gb = Country.objects.filter(code="GB").first()

        for name in UNIVERSITIES:
            exists = University.objects.filter(university=name).exists()
            if exists:
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(f"  [NEW] {name}")
            else:
                University.objects.create(university=name, country=gb)
                self.stdout.write(f"  + {name}")
            created_count += 1

        # Summary
        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — "
                f"{created_count} new universities added, "
                f"{skipped_count} already existed (skipped). "
                f"Total in list: {len(UNIVERSITIES)}"
            )
        )
