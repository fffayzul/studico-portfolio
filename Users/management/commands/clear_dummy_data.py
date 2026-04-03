# Users/management/commands/clear_dummy_data.py
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from Users.models import (
    # Feed posts + media + interactions
    Posts,
    PostImages,
    PostVideos,
    PostLike,
    PostComment,
    SavedPost,
    BookmarkedPosts,

    # Community posts + media + interactions
    Communities,
    Community_Posts,
    Community_Posts_Image,
    Community_Posts_Video,
    Community_Posts_Comment,
    SavedCommunityPost,
    BookmarkedCommunityPosts,
    LikeCommunityPost,

    # Student / Community events + discussions + interactions
    Student_Events,
    Student_Events_Discussion,
    SavedStudentEvents,
    EventRSVP,
    LikeEvent,

    Community_Events,
    Community_Events_Discussion,
    CommunityEventRSVP,
    LikeCommunityEvent,
    BookmarkedCommunityEvents,

    # Messaging + notifications
    DirectMessage,
    CommunityChatMessage,
    GroupChatMessage,
    Notification,

    # Memberships & misc user‑generated
    Membership,
    Report,
)


class Command(BaseCommand):
    help = "Clear dummy user-generated data before launch (posts, messages, likes, bookmarks, etc)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting anything.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required to run without dry-run (safety guard).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        confirm = options["confirm"]

        if not dry_run and not confirm:
            raise CommandError(
                "Refusing to run destructive clear_dummy_data without --confirm. "
                "Use --dry-run first to inspect counts."
            )

        # Order matters where there are FK dependencies; children first, then parents.
        delete_plan = [
            # Messaging & notifications
            ("DirectMessage", DirectMessage.objects.all()),
            ("CommunityChatMessage", CommunityChatMessage.objects.all()),
            ("GroupChatMessage", GroupChatMessage.objects.all()),
            ("Notification", Notification.objects.all()),

            # Post interactions
            ("PostComment", PostComment.objects.all()),
            ("PostLike", PostLike.objects.all()),
            ("SavedPost", SavedPost.objects.all()),
            ("BookmarkedPosts", BookmarkedPosts.objects.all()),
            ("PostImages", PostImages.objects.all()),
            ("PostVideos", PostVideos.objects.all()),
            ("Posts", Posts.objects.all()),

            # Community post interactions
            ("Community_Posts_Comment", Community_Posts_Comment.objects.all()),
            ("SavedCommunityPost", SavedCommunityPost.objects.all()),
            ("BookmarkedCommunityPosts", BookmarkedCommunityPosts.objects.all()),
            ("LikeCommunityPost", LikeCommunityPost.objects.all()),
            ("Community_Posts_Image", Community_Posts_Image.objects.all()),
            ("Community_Posts_Video", Community_Posts_Video.objects.all()),
            ("Community_Posts", Community_Posts.objects.all()),

            # Student events
            ("Student_Events_Discussion", Student_Events_Discussion.objects.all()),
            ("SavedStudentEvents", SavedStudentEvents.objects.all()),
            ("EventRSVP", EventRSVP.objects.all()),
            ("LikeEvent", LikeEvent.objects.all()),
            ("Student_Events", Student_Events.objects.all()),

            # Community events
            ("Community_Events_Discussion", Community_Events_Discussion.objects.all()),
            ("CommunityEventRSVP", CommunityEventRSVP.objects.all()),
            ("LikeCommunityEvent", LikeCommunityEvent.objects.all()),
            ("BookmarkedCommunityEvents", BookmarkedCommunityEvents.objects.all()),
            ("Community_Events", Community_Events.objects.all()),

            # Memberships, communities & reports
            ("Membership", Membership.objects.all()),
            ("Communities", Communities.objects.all()),
            ("Report", Report.objects.all()),
        ]

        total_to_delete = 0
        for label, qs in delete_plan:
            count = qs.count()
            total_to_delete += count
            if dry_run:
                self.stdout.write(self.style.WARNING(f"[DRY-RUN] {label}: would delete {count} rows"))
            else:
                self.stdout.write(self.style.WARNING(f"Deleting {count} {label} rows..."))
                qs.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"{'DRY-RUN complete' if dry_run else 'Deletion complete'} "
                f"({total_to_delete} total rows {'would be ' if dry_run else ''}affected)."
            )
        )

        # In dry-run, force rollback so nothing is committed
        if dry_run:
            raise CommandError("Dry-run requested; transaction rolled back intentionally.")