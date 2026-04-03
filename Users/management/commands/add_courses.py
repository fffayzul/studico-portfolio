"""
Management command to add courses to the database.
Skips any courses that already exist (matched by name).

Usage:
    python manage.py add_courses
    python manage.py add_courses --dry-run   # preview without saving
"""

from django.core.management.base import BaseCommand
from Users.models import Courses


COURSES = [
    # ── STEM ─────────────────────────────────────────────────────────────
    "Computer Science",
    "Software Engineering",
    "Computing",
    "Cyber Security",
    "Data Science",
    "Artificial Intelligence",
    "Information Technology",
    "Mathematics",
    "Physics",
    "Chemistry",
    "Biology",
    "Biomedical Science",
    "Biomedical Engineering",
    "Biochemistry",
    "Biotechnology",
    "Environmental Science",
    "Geography",
    "Geology",
    "Marine Biology",
    "Forensic Science",
    "Neuroscience",
    "Genetics",
    "Microbiology",
    "Zoology",
    "Astronomy",
    "Statistics",

    # ── Engineering ──────────────────────────────────────────────────────
    "Mechanical Engineering",
    "Electrical Engineering",
    "Electronic Engineering",
    "Civil Engineering",
    "Chemical Engineering",
    "Aerospace Engineering",
    "Automotive Engineering",
    "Robotics",
    "Engineering",

    # ── Health & Medicine ────────────────────────────────────────────────
    "Medicine",
    "Dentistry",
    "Pharmacy",
    "Nursing",
    "Midwifery",
    "Physiotherapy",
    "Occupational Therapy",
    "Radiography",
    "Optometry",
    "Veterinary Science",
    "Paramedic Science",
    "Public Health",
    "Health Sciences",
    "Speech and Language Therapy",
    "Sports Science",
    "Nutrition",
    "Psychology",

    # ── Business & Economics ─────────────────────────────────────────────
    "Business Management",
    "Business Administration",
    "Accounting",
    "Finance",
    "Economics",
    "Marketing",
    "Human Resources",
    "International Business",
    "Entrepreneurship",
    "Real Estate",
    "Hospitality Management",
    "Tourism Management",
    "Event Management",
    "Supply Chain Management",
    "Retail Management",

    # ── Law & Politics ───────────────────────────────────────────────────
    "Law",
    "Commercial Law",
    "Criminal Justice",
    "Criminology",
    "Politics",
    "International Relations",
    "Public Policy",

    # ── Arts & Humanities ────────────────────────────────────────────────
    "English Literature",
    "English Language",
    "History",
    "Philosophy",
    "Theology",
    "Classics",
    "Linguistics",
    "Creative Writing",
    "Liberal Arts",

    # ── Social Sciences ──────────────────────────────────────────────────
    "Sociology",
    "Anthropology",
    "Social Work",
    "Education",
    "Early Childhood Studies",
    "Youth Work",

    # ── Languages ────────────────────────────────────────────────────────
    "Modern Languages",
    "French",
    "Spanish",
    "German",
    "Arabic",
    "Chinese",
    "Japanese",
    "Translation Studies",

    # ── Creative & Design ────────────────────────────────────────────────
    "Fine Art",
    "Graphic Design",
    "Interior Design",
    "Fashion Design",
    "Product Design",
    "Illustration",
    "Animation",
    "Photography",
    "Film Studies",
    "Film Production",
    "Game Design",
    "Architecture",
    "Landscape Architecture",
    "Urban Planning",

    # ── Media & Communication ────────────────────────────────────────────
    "Media Studies",
    "Journalism",
    "Mass Communications",
    "Public Relations",
    "Advertising",
    "Digital Media",

    # ── Performing Arts ──────────────────────────────────────────────────
    "Music",
    "Music Production",
    "Drama",
    "Dance",
    "Performing Arts",
    "Theatre Studies",

    # ── Agriculture & Environment ────────────────────────────────────────
    "Agriculture",
    "Food Science",
    "Animal Science",
    "Ecology",
    "Sustainability",
]


class Command(BaseCommand):
    help = "Add courses to the database"

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

        for name in COURSES:
            exists = Courses.objects.filter(course=name).exists()
            if exists:
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(f"  [NEW] {name}")
            else:
                Courses.objects.create(course=name)
                self.stdout.write(f"  + {name}")
            created_count += 1

        # Summary
        self.stdout.write("")
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done — "
                f"{created_count} new courses added, "
                f"{skipped_count} already existed (skipped). "
                f"Total in list: {len(COURSES)}"
            )
        )
