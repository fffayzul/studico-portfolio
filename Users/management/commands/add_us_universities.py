"""
Management command to add US universities to the database.
Requires the 'US' country to exist — run add_countries first.

Usage:
    python manage.py add_us_universities
    python manage.py add_us_universities --dry-run
"""

from django.core.management.base import BaseCommand
from Users.models import Country, University


UNIVERSITIES = [
    # ── Ivy League (8) ────────────────────────────────────────────────────
    "Harvard University",
    "Yale University",
    "Princeton University",
    "Columbia University",
    "University of Pennsylvania",
    "Brown University",
    "Dartmouth College",
    "Cornell University",

    # ── Other top privates ────────────────────────────────────────────────
    "Stanford University",
    "Massachusetts Institute of Technology",
    "California Institute of Technology",
    "Duke University",
    "Northwestern University",
    "University of Chicago",
    "Johns Hopkins University",
    "Vanderbilt University",
    "Rice University",
    "Emory University",
    "Georgetown University",
    "Carnegie Mellon University",
    "Washington University in St. Louis",
    "University of Notre Dame",
    "University of Southern California",
    "New York University",
    "Boston University",
    "Boston College",
    "Tufts University",
    "Wake Forest University",
    "Lehigh University",
    "Tulane University",
    "Brandeis University",
    "Case Western Reserve University",
    "Northeastern University",
    "George Washington University",
    "American University",
    "Syracuse University",
    "Fordham University",
    "Villanova University",
    "Santa Clara University",
    "Gonzaga University",
    "Drexel University",
    "Howard University",
    "Spelman College",
    "Morehouse College",

    # ── State flagships / major publics ───────────────────────────────────
    "University of California, Berkeley",
    "University of California, Los Angeles",
    "University of California, San Diego",
    "University of California, Davis",
    "University of California, Santa Barbara",
    "University of California, Irvine",
    "University of California, Santa Cruz",
    "University of California, Riverside",
    "University of Michigan",
    "University of Virginia",
    "University of North Carolina at Chapel Hill",
    "University of Wisconsin-Madison",
    "University of Texas at Austin",
    "University of Florida",
    "University of Georgia",
    "University of Illinois Urbana-Champaign",
    "University of Washington",
    "University of Maryland, College Park",
    "University of Minnesota",
    "University of Iowa",
    "University of Colorado Boulder",
    "University of Oregon",
    "University of Arizona",
    "University of Pittsburgh",
    "University of Connecticut",
    "University of Massachusetts Amherst",
    "University of Delaware",
    "University of Kansas",
    "University of Kentucky",
    "University of Missouri",
    "University of Nebraska-Lincoln",
    "University of Oklahoma",
    "University of South Carolina",
    "University of Tennessee",
    "University of Alabama",
    "University of Arkansas",
    "University of Mississippi",
    "University of Hawaii at Manoa",
    "University of New Mexico",
    "University of Nevada, Las Vegas",
    "Rutgers University",
    "Penn State University",
    "Ohio State University",
    "Michigan State University",
    "Indiana University Bloomington",
    "Purdue University",
    "University of Utah",
    "Virginia Tech",
    "Georgia Tech",
    "North Carolina State University",
    "Texas A&M University",
    "Florida State University",
    "Iowa State University",
    "Oregon State University",
    "Washington State University",
    "Colorado State University",
    "Arizona State University",
    "San Diego State University",
    "San Jose State University",
    "University of Central Florida",
    "University of South Florida",
    "University of Houston",
    "Temple University",
    "University at Buffalo",
    "Stony Brook University",
    "University of Cincinnati",
    "University of Louisville",
    "Clemson University",
    "Auburn University",
    "Louisiana State University",
    "University of Vermont",
    "University of Wyoming",
    "University of Montana",
    "University of Idaho",
    "University of Rhode Island",
    "University of Maine",
    "University of New Hampshire",
    "West Virginia University",
]


class Command(BaseCommand):
    help = "Add US universities to the database"

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

        created_count = 0
        skipped_count = 0

        for name in UNIVERSITIES:
            exists = University.objects.filter(university=name, country=us).exists()
            if exists:
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(f"  [NEW] {name}")
            else:
                University.objects.create(university=name, country=us)
                self.stdout.write(f"  + {name}")
            created_count += 1

        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — {created_count} new US universities added, "
                f"{skipped_count} already existed (skipped). "
                f"Total in list: {len(UNIVERSITIES)}"
            )
        )
