from django.contrib import admin
from django import forms
from django.shortcuts import render, redirect
from django.contrib import messages
from django.urls import path
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.db.models import Count
from django.core.cache import cache
from .models import (
    Country,
    Courses,
    University,
    Interests,
    Student,
    Student_Events,
    Communities,
    Community_Events,
    Community_Posts,
    Community_Posts_Comment,
    Community_Events_Discussion,
    Student_Events_Discussion,
    Posts,
    PostLike,
    PostComment,
    PostImages,
    PostVideos,
    Friendship,
    Membership,
    DirectMessage,
    Notification,
    CommunityChatMessage,
    SavedPost,
    SavedCommunityPost,
    SavedStudentEvents,
    EventRSVP,
    CommunityEventRSVP,
    Location,
    Region,
    notificationType,
    LikeEvent,
    LikeCommunityPost,
    LikeCommunityEvent,
    EmailVerification,
    BookmarkedCommunityEvents,
    BookmarkedStudentEvents,
    BookmarkedPosts,
    BookmarkedCommunityPosts,
    Student_Events_Image,
    Student_Events_Video,
    Community_Events_Image,
    Community_Events_Video,
    Community_Posts_Image,
    Community_Posts_Video,
    DeviceToken,
    MutedStudents,
    MutedCommunities,
    Block,
    BlockedByCommunities,
    Advertisements,
    DataDeletionRequest,
    QRScan,
    Report,
    BannedStudents,
)
# Group chat models (may not exist in older migrations)
try:
    from .models import GroupChat, GroupChatMembership, GroupChatMessage
    HAS_GROUP_CHAT = True
except ImportError:
    HAS_GROUP_CHAT = False


# ----- Form: Send email to verified users -----

class SendEmailToVerifiedForm(forms.Form):
    subject = forms.CharField(
        max_length=200,
        required=True,
        widget=forms.TextInput(attrs={'size': 80, 'placeholder': 'Email subject'}),
    )
    message = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={'rows': 12, 'cols': 80, 'placeholder': 'Plain text message body'}),
        help_text='Plain text only. All verified users will receive this email.',
    )


# ----- Inlines for Student (view student's related data in one place) -----

class PostInline(admin.TabularInline):
    model = Posts
    fk_name = 'student'
    extra = 0
    show_change_link = True
    fields = ('id', 'context_text', 'post_date')
    readonly_fields = ('post_date',)
    ordering = ('-post_date',)
    max_num = 50


class PostCommentInline(admin.TabularInline):
    model = PostComment
    fk_name = 'student'
    extra = 0
    show_change_link = True
    fields = ('id', 'post', 'comment', 'commented_at')
    readonly_fields = ('commented_at',)
    ordering = ('-commented_at',)
    max_num = 50


class FriendshipSentInline(admin.TabularInline):
    model = Friendship
    fk_name = 'sender'
    extra = 0
    show_change_link = True
    verbose_name = 'Friend request sent'
    verbose_name_plural = 'Friend requests sent'
    fields = ('receiver', 'status', 'created_at', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)
    max_num = 100


class FriendshipReceivedInline(admin.TabularInline):
    model = Friendship
    fk_name = 'receiver'
    extra = 0
    show_change_link = True
    verbose_name = 'Friend request received'
    verbose_name_plural = 'Friend requests received'
    fields = ('sender', 'status', 'created_at', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)
    max_num = 100


class StudentEventInline(admin.TabularInline):
    model = Student_Events
    fk_name = 'student'
    extra = 0
    show_change_link = True
    fields = ('id', 'event_name', 'date', 'dateposted', 'RSVP')
    readonly_fields = ('dateposted',)
    ordering = ('-dateposted',)
    max_num = 30


class MembershipInline(admin.TabularInline):
    model = Membership
    fk_name = 'user'
    extra = 0
    show_change_link = True
    verbose_name = 'Community membership'
    verbose_name_plural = 'Community memberships'
    fields = ('community', 'role', 'date_joined')
    readonly_fields = ('date_joined',)
    max_num = 50


class NotificationReceivedInline(admin.TabularInline):
    model = Notification
    fk_name = 'recipient'
    extra = 0
    show_change_link = True
    verbose_name = 'Notification received'
    verbose_name_plural = 'Notifications received'
    fields = ('sender', 'content', 'is_read', 'created_at')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)
    max_num = 30


if HAS_GROUP_CHAT:
    class GroupChatMembershipInline(admin.TabularInline):
        model = GroupChatMembership
        fk_name = 'member'
        extra = 0
        show_change_link = True
        verbose_name = 'Group chat membership'
        verbose_name_plural = 'Group chat memberships'
        fields = ('group', 'role', 'joined_at', 'is_muted')
        readonly_fields = ('joined_at',)
        max_num = 30


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    change_list_template = 'admin/Users/student/change_list.html'
    list_display = (
        'id',
        'name',
        'username',
        'email',
        'university',
        'course',
        'is_verified',
        'kinde_short',
        'post_count',
        'friend_count',
        'comment_count',
    )
    list_filter = ('university', 'is_verified', 'course')
    search_fields = ('name', 'username', 'email', 'kinde_user_id', 'student_email')
    readonly_fields = ('kinde_user_id', 'otp_created_at')
    filter_horizontal = ('student_interest', 'student_mentions', 'community_mentions')
    inlines = [
        PostInline,
        PostCommentInline,
        FriendshipSentInline,
        FriendshipReceivedInline,
        StudentEventInline,
        MembershipInline,
        NotificationReceivedInline,
    ]
    fieldsets = (
        (None, {
            'fields': ('name', 'username', 'email', 'student_email', 'bio', 'kinde_user_id')
        }),
        ('Verification', {
            'fields': ('is_verified', 'otp_code', 'otp_created_at')
        }),
        ('Raffle referral', {
            'fields': ('referral_code', 'referral_code_used', 'verified_referrals_count'),
            'description': 'referral_code: set manually for testing, or leave blank (generated on verification). Must be unique.',
        }),
        ('Profile', {
            'fields': ('university', 'course', 'student_location', 'profile_image', 'student_interest', 'student_mentions', 'community_mentions')
        }),
    )

    def kinde_short(self, obj):
        if obj.kinde_user_id:
            return obj.kinde_user_id[:20] + '…' if len(obj.kinde_user_id) > 20 else obj.kinde_user_id
        return '-'
    kinde_short.short_description = 'Kinde ID'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        try:
            return qs.annotate(
                _post_count=Count('posts', distinct=True),
                _comment_count=Count('postcomment', distinct=True),
            )
        except Exception:
            return qs

    def post_count(self, obj):
        if getattr(obj, '_post_count', None) is not None:
            return obj._post_count
        return Posts.objects.filter(student=obj).count()
    post_count.short_description = 'Posts'

    def comment_count(self, obj):
        if getattr(obj, '_comment_count', None) is not None:
            return obj._comment_count
        return PostComment.objects.filter(student=obj).count()
    comment_count.short_description = 'Comments'

    def friend_count(self, obj):
        from django.db.models import Q
        return Friendship.objects.filter(
            Q(sender=obj) | Q(receiver=obj),
            status='accepted'
        ).count()
    friend_count.short_description = 'Friends'

    def get_inlines(self, request, obj=None):
        inlines = list(super().get_inlines(request, obj))
        if HAS_GROUP_CHAT:
            inlines.append(GroupChatMembershipInline)
        return inlines

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                'email-verified-users/',
                self.admin_site.admin_view(self.send_email_to_verified_view),
                name='Users_student_email_verified_users',
            ),
        ]
        return custom + urls

    def send_email_to_verified_view(self, request):
        """Form to queue an email to all verified users (sent via Celery)."""
        verified_count = Student.objects.filter(is_verified=True).count()

        if request.method == 'POST':
            form = SendEmailToVerifiedForm(request.POST)
            if form.is_valid():
                subject = form.cleaned_data['subject']
                message = form.cleaned_data['message']
                from .tasks import send_email_to_verified_users_task
                send_email_to_verified_users_task.delay(subject, message)
                messages.success(
                    request,
                    f'Email has been queued and will be sent to {verified_count} verified user(s) shortly.',
                )
                return redirect(request.path)
        else:
            form = SendEmailToVerifiedForm()

        context = {
            **self.admin_site.each_context(request),
            'form': form,
            'verified_count': verified_count,
            'title': 'Send email to verified users',
            'opts': self.model._meta,
        }
        return render(request, 'admin/Users/student/send_email_verified.html', context)


# ----- Posts & engagement -----

@admin.register(Posts)
class PostsAdmin(admin.ModelAdmin):
    list_display = ('id', 'student', 'context_preview', 'post_date', 'like_count', 'comment_count')
    list_filter = ('student__university',)
    search_fields = ('context_text', 'student__name', 'student__username')
    readonly_fields = ('post_date',)
    date_hierarchy = 'post_date'

    def context_preview(self, obj):
        text = (obj.context_text or '')[:60]
        return text + '…' if len(obj.context_text or '') > 60 else text
    context_preview.short_description = 'Content'

    def like_count(self, obj):
        return obj.likes.count()
    like_count.short_description = 'Likes'

    def comment_count(self, obj):
        return obj.comments.count()
    comment_count.short_description = 'Comments'


class PostCommentReplyInline(admin.TabularInline):
    model = PostComment
    fk_name = 'parent'
    extra = 0
    show_change_link = True
    fields = ('student', 'comment', 'commented_at')
    readonly_fields = ('commented_at',)
    max_num = 20


@admin.register(PostComment)
class PostCommentAdmin(admin.ModelAdmin):
    list_display = ('id', 'student', 'post', 'comment_preview', 'commented_at', 'parent')
    list_filter = ('student__university',)
    search_fields = ('comment', 'student__name', 'post__context_text')
    readonly_fields = ('commented_at',)
    date_hierarchy = 'commented_at'
    inlines = [PostCommentReplyInline]

    def comment_preview(self, obj):
        text = (obj.comment or '')[:50]
        return text + '…' if len(obj.comment or '') > 50 else text
    comment_preview.short_description = 'Comment'


@admin.register(PostLike)
class PostLikeAdmin(admin.ModelAdmin):
    list_display = ('id', 'student', 'post', 'liked_at')
    list_filter = ('student__university',)
    search_fields = ('student__name', 'post__context_text')
    date_hierarchy = 'liked_at'


# ----- Friendships -----

@admin.register(Friendship)
class FriendshipAdmin(admin.ModelAdmin):
    list_display = ('id', 'sender', 'receiver', 'status', 'created_at', 'updated_at')
    list_filter = ('status',)
    search_fields = ('sender__name', 'sender__username', 'receiver__name', 'receiver__username')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'


# ----- Student & Community Events -----

@admin.register(Student_Events)
class StudentEventsAdmin(admin.ModelAdmin):
    list_display = ('id', 'event_name', 'student', 'date', 'dateposted', 'RSVP')
    list_filter = ('student__university',)
    search_fields = ('event_name', 'description', 'student__name')
    readonly_fields = ('dateposted',)
    date_hierarchy = 'dateposted'


@admin.register(Community_Events)
class CommunityEventsAdmin(admin.ModelAdmin):
    list_display = ('id', 'event_name', 'community', 'poster', 'date', 'dateposted', 'RSVP')
    list_filter = ('community',)
    search_fields = ('event_name', 'description', 'poster__name')
    readonly_fields = ('dateposted',)
    date_hierarchy = 'dateposted'


# ----- Communities & community posts -----

class CommunityMembershipInline(admin.TabularInline):
    model = Membership
    fk_name = 'community'
    extra = 0
    show_change_link = True
    verbose_name = 'Member'
    verbose_name_plural = 'Members'
    fields = ('user', 'role', 'date_joined')
    readonly_fields = ('date_joined',)
    ordering = ('-date_joined',)
    max_num = 500
    autocomplete_fields = ('user',)


class CommunityEventsInline(admin.TabularInline):
    model = Community_Events
    fk_name = 'community'
    extra = 0
    show_change_link = True
    verbose_name = 'Community event'
    verbose_name_plural = 'Community events'
    fields = ('event_name', 'poster', 'date', 'dateposted', 'RSVP')
    readonly_fields = ('dateposted',)
    ordering = ('-dateposted',)
    max_num = 100
    autocomplete_fields = ('poster',)


class CommunityPostsInline(admin.TabularInline):
    model = Community_Posts
    fk_name = 'community'
    extra = 0
    show_change_link = True
    verbose_name = 'Community post'
    verbose_name_plural = 'Community posts'
    fields = ('post_text', 'poster', 'post_date', 'post_time')
    readonly_fields = ('post_date', 'post_time',)
    ordering = ('-post_date', '-post_time')
    max_num = 100
    autocomplete_fields = ('poster',)


@admin.register(Communities)
class CommunitiesAdmin(admin.ModelAdmin):
    list_display = ('id', 'community_name', 'community_tag', 'member_count', 'location')
    list_filter = ('location',)
    search_fields = ('community_name', 'community_tag', 'community_bio')
    filter_horizontal = ('community_interest', 'student_mentions', 'community_mentions')
    inlines = [CommunityMembershipInline, CommunityEventsInline, CommunityPostsInline]

    def member_count(self, obj):
        return Membership.objects.filter(community=obj).count()
    member_count.short_description = 'Members'


class CommunityPostsCommentInline(admin.TabularInline):
    model = Community_Posts_Comment
    fk_name = 'community_post'
    extra = 0
    show_change_link = True
    fields = ('student', 'comment_text', 'sent_at')
    readonly_fields = ('sent_at',)
    max_num = 20


@admin.register(Community_Posts)
class CommunityPostsAdmin(admin.ModelAdmin):
    list_display = ('id', 'poster', 'community', 'post_text_preview', 'post_date', 'post_time')
    list_filter = ('community',)
    search_fields = ('post_text', 'poster__name', 'community__community_name')
    inlines = [CommunityPostsCommentInline]

    def post_text_preview(self, obj):
        text = (obj.post_text or '')[:50]
        return text + '…' if len(obj.post_text or '') > 50 else text
    post_text_preview.short_description = 'Post'


@admin.register(Community_Posts_Comment)
class CommunityPostsCommentAdmin(admin.ModelAdmin):
    list_display = ('id', 'student', 'community_post', 'comment_preview', 'sent_at', 'parent')
    list_filter = ('community_post__community',)
    search_fields = ('comment_text', 'student__name')
    readonly_fields = ('sent_at',)
    date_hierarchy = 'sent_at'

    def comment_preview(self, obj):
        text = (obj.comment_text or '')[:50]
        return text + '…' if len(obj.comment_text or '') > 50 else text
    comment_preview.short_description = 'Comment'


# ----- Messaging -----

@admin.register(DirectMessage)
class DirectMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'sender', 'receiver', 'message_preview', 'timestamp', 'is_read')
    list_filter = ('is_read',)
    search_fields = ('message', 'sender__name', 'receiver__name')
    readonly_fields = ('timestamp',)
    date_hierarchy = 'timestamp'

    def message_preview(self, obj):
        text = (obj.message or '')[:40]
        return text + '…' if len(obj.message or '') > 40 else text
    message_preview.short_description = 'Message'


@admin.register(CommunityChatMessage)
class CommunityChatMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'student', 'community', 'message_preview', 'sent_at')
    list_filter = ('community',)
    search_fields = ('message', 'student__name')
    readonly_fields = ('sent_at',)
    date_hierarchy = 'sent_at'

    def message_preview(self, obj):
        text = (obj.message or '')[:40]
        return text + '…' if len(obj.message or '') > 40 else text
    message_preview.short_description = 'Message'


# ----- Notifications -----

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'recipient', 'sender', 'content_preview', 'is_read', 'created_at')
    list_filter = ('is_read',)
    search_fields = ('content', 'recipient__name', 'sender__name')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'

    def content_preview(self, obj):
        text = (obj.content or '')[:50]
        return text + '…' if len(obj.content or '') > 50 else text
    content_preview.short_description = 'Content'


# ----- Group chats (if available) -----

if HAS_GROUP_CHAT:
    @admin.register(GroupChat)
    class GroupChatAdmin(admin.ModelAdmin):
        list_display = ('id', 'name', 'created_by', 'member_count', 'is_active', 'created_at')
        list_filter = ('is_active',)
        search_fields = ('name', 'description', 'created_by__name')

        def member_count(self, obj):
            return obj.memberships.count()
        member_count.short_description = 'Members'

    @admin.register(GroupChatMembership)
    class GroupChatMembershipAdmin(admin.ModelAdmin):
        list_display = ('id', 'group', 'member', 'role', 'joined_at', 'is_muted')
        list_filter = ('role', 'is_muted')
        search_fields = ('group__name', 'member__name')

    @admin.register(GroupChatMessage)
    class GroupChatMessageAdmin(admin.ModelAdmin):
        list_display = ('id', 'group', 'student', 'message_preview', 'sent_at')
        list_filter = ('group',)
        search_fields = ('message', 'student__name')
        readonly_fields = ('sent_at',)
        date_hierarchy = 'sent_at'

        def message_preview(self, obj):
            text = (obj.message or '')[:40]
            return text + '…' if len(obj.message or '') > 40 else text
        message_preview.short_description = 'Message'


# ----- Membership -----

@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'community', 'role', 'date_joined')
    list_filter = ('role', 'community')
    search_fields = ('user__name', 'community__community_name')
    date_hierarchy = 'date_joined'


# ----- Data deletion & moderation -----

@admin.register(DataDeletionRequest)
class DataDeletionRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'student', 'requested_at', 'is_cancelled', 'deleted_at')
    list_filter = ('is_cancelled',)
    search_fields = ('student__name', 'student__email')
    date_hierarchy = 'requested_at'


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ('id', 'blocker', 'blocked', 'timestamp')
    search_fields = ('blocker__name', 'blocked__name')
    date_hierarchy = 'timestamp'


# ----- Country, Region, Location -----

@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'code', 'allowed_email_domains')
    search_fields = ('name', 'code')


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ('id', 'region', 'country')
    list_filter = ('country',)
    search_fields = ('region',)


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('id', 'location', 'region', 'region_country')
    list_filter = ('region__country',)
    search_fields = ('location', 'region__region')

    def region_country(self, obj):
        return obj.region.country if obj.region else '-'
    region_country.short_description = 'Country'


@admin.register(University)
class UniversityAdmin(admin.ModelAdmin):
    list_display = ('id', 'university', 'country')
    list_filter = ('country',)
    search_fields = ('university',)


# ----- Simple registrations (no custom admin needed) -----

admin.site.register(Courses)
admin.site.register(Interests)
admin.site.register(Community_Events_Discussion)
admin.site.register(Student_Events_Discussion)
admin.site.register(PostImages)
admin.site.register(PostVideos)
admin.site.register(SavedPost)
admin.site.register(SavedCommunityPost)
admin.site.register(SavedStudentEvents)
admin.site.register(EventRSVP)
admin.site.register(CommunityEventRSVP)
admin.site.register(notificationType)
admin.site.register(LikeEvent)
admin.site.register(LikeCommunityPost)
admin.site.register(LikeCommunityEvent)
admin.site.register(EmailVerification)
admin.site.register(BookmarkedCommunityEvents)
admin.site.register(BookmarkedStudentEvents)
admin.site.register(BookmarkedPosts)
admin.site.register(BookmarkedCommunityPosts)
admin.site.register(Student_Events_Image)
admin.site.register(Student_Events_Video)
admin.site.register(Community_Events_Image)
admin.site.register(Community_Events_Video)
admin.site.register(Community_Posts_Image)
admin.site.register(Community_Posts_Video)
admin.site.register(DeviceToken)
admin.site.register(MutedStudents)
admin.site.register(MutedCommunities)
admin.site.register(BlockedByCommunities)
admin.site.register(Advertisements)
admin.site.register(QRScan)


# ----- Report & BannedStudents -----

@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'reporter', 'reported_student_display', 'report_type', 'status',
        'action_taken', 'created_at',
    )
    list_filter = ('status', 'report_type', 'action_taken')
    search_fields = ('reporter__name', 'reporter__email', 'description', 'report_copy')
    readonly_fields = ('reporter', 'content_type', 'object_id', 'created_at', 'reviewed_at', 'report_copy')
    date_hierarchy = 'created_at'
    actions = ['ban_reported_user', 'mark_resolved', 'mark_dismissed']

    def reported_student_display(self, obj):
        try:
            s = obj.reported_student
            return s.name if s else '—'
        except Exception:
            return '—'
    reported_student_display.short_description = 'Reported user'

    @admin.action(description='Ban reported user')
    def ban_reported_user(self, request, queryset):
        from django.utils import timezone
        banned = 0
        skipped = 0
        for report in queryset:
            student = report.reported_student
            if not student:
                skipped += 1
                continue
            if BannedStudents.objects.filter(student=student).exists():
                skipped += 1
                continue
            ban_reason = report.report_type + (f': {report.description[:500]}' if report.description else '')
            BannedStudents.objects.create(
                student=student,
                reason=ban_reason,
                banned_until=None,
            )
            from .kinde_functions import _ban_cache_key, revoke_all_tokens_for_student, USER_BANNED_CACHE_TTL
            kid = (student.kinde_user_id or "").strip()
            if kid:
                cache.delete(_ban_cache_key(kid))
                cache.set(_ban_cache_key(kid), True, USER_BANNED_CACHE_TTL)
            revoke_all_tokens_for_student(student)
            from .tasks import send_banned_notification_email
            send_banned_notification_email.delay(
                student.email or '',
                student.name or 'there',
                reason=ban_reason,
                banned_until_iso=None,
            )
            report.status = 'resolved'
            report.action_taken = 'ban'
            report.reviewed_at = timezone.now()
            report.save(update_fields=['status', 'action_taken', 'reviewed_at'])
            banned += 1
        self.message_user(request, f'Banned {banned} user(s). Skipped {skipped} (no reported user or already banned).')

    @admin.action(description='Mark as Resolved')
    def mark_resolved(self, request, queryset):
        from django.utils import timezone
        n = queryset.update(status='resolved', reviewed_at=timezone.now())
        self.message_user(request, f'Marked {n} report(s) as resolved.')

    @admin.action(description='Mark as Dismissed')
    def mark_dismissed(self, request, queryset):
        from django.utils import timezone
        n = queryset.update(status='dismissed', reviewed_at=timezone.now())
        self.message_user(request, f'Marked {n} report(s) as dismissed.')


@admin.register(BannedStudents)
class BannedStudentsAdmin(admin.ModelAdmin):
    list_display = ('id', 'student', 'reason_short', 'banned_at', 'banned_until')
    list_filter = ('banned_at',)
    search_fields = ('student__name', 'student__email', 'reason')
    readonly_fields = ('banned_at',)
    date_hierarchy = 'banned_at'

    def reason_short(self, obj):
        return (obj.reason or '')[:60] + ('…' if obj.reason and len(obj.reason) > 60 else '')
    reason_short.short_description = 'Reason'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from .kinde_functions import _ban_cache_key, revoke_all_tokens_for_student, USER_BANNED_CACHE_TTL
        kid = (obj.student.kinde_user_id or "").strip()
        if kid:
            cache.delete(_ban_cache_key(kid))
            cache.set(_ban_cache_key(kid), True, USER_BANNED_CACHE_TTL)
        if not change:
            revoke_all_tokens_for_student(obj.student)
            from .tasks import send_banned_notification_email
            send_banned_notification_email.delay(
                obj.student.email or '',
                obj.student.name or 'there',
                reason=obj.reason,
                banned_until_iso=obj.banned_until.isoformat() if obj.banned_until else None,
            )

    def delete_model(self, request, obj):
        kinde_user_id = obj.student.kinde_user_id
        super().delete_model(request, obj)
        from .kinde_functions import _ban_cache_key
        cache.delete(_ban_cache_key(kinde_user_id))

    def delete_queryset(self, request, queryset):
        from .kinde_functions import _ban_cache_key
        for obj in queryset:
            cache.delete(_ban_cache_key(obj.student.kinde_user_id))
        super().delete_queryset(request, queryset)
