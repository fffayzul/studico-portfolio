from django.db import models
from django.db.models import Q
from django.utils.timezone import now
from django.utils import timezone
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

# Create your models here.
class Country(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=5, unique=True)
    allowed_email_domains = models.JSONField(default=list)

    def __str__(self):
        return self.name


class Courses(models.Model):
    course = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.course

class University(models.Model):
    university = models.CharField(max_length=50)
    country = models.ForeignKey(Country, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        unique_together = [('university', 'country')]

    def __str__(self):
        return self.university

class Interests(models.Model):
    interest = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.interest


class Region(models.Model):
    region = models.CharField(max_length=50)
    country = models.ForeignKey(Country, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        unique_together = [('region', 'country')]

    def __str__(self):
        return self.region
    

class Location(models.Model):
    location = models.CharField(max_length=50)
    region = models.ForeignKey(Region, on_delete=models.CASCADE, default=1)

    class Meta:
        unique_together = [('location', 'region')]

    def __str__(self):
        return self.location

class Student(models.Model):
    email = models.CharField(max_length=50, unique=True)
    student_email = models.CharField(max_length=50, null=True, blank=True)
    name = models.CharField(max_length=40, null=True, blank=True)
    username = models.CharField(max_length=25, unique=True, null=True, blank=True)
    bio = models.CharField(max_length=200, null=True, blank=True)
    university = models.ForeignKey(University, on_delete=models.SET_NULL, null=True, blank=True)
    student_interest = models.ManyToManyField(Interests, related_name="Student", null=True, blank=True)
    student_location = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True)
    country = models.ForeignKey(Country, on_delete=models.SET_NULL, null=True, blank=True)
    kinde_user_id = models.CharField(max_length=500)
    profile_image = models.ImageField(upload_to='profileimages/', null=True, blank=True)
    course = models.ForeignKey(Courses, on_delete=models.SET_NULL, null=True, blank=True)
    otp_code = models.CharField(max_length=6, null=True, blank=True)
    otp_created_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    student_mentions = models.ManyToManyField('self', related_name="Student_Mentions_In_Bio", blank=True, symmetrical=False)
    community_mentions = models.ManyToManyField('Communities', related_name="Community_Mentions_In_Bio", blank=True)
    # Raffle referral: code for sharing; code they used from link (resolved at verification); cached count of verified referrals
    referral_code = models.CharField(max_length=20, unique=True, null=True, blank=True)
    referral_code_used = models.CharField(max_length=20, null=True, blank=True)
    verified_referrals_count = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['student_email'],
                condition=Q(is_verified=True),
                name='unique_verified_student_email'
            )
        ]

    def __str__(self):
        return f"{self.name} | {self.student_email} | {self.email}"
    
class EmailVerification(models.Model):
    student = models.OneToOneField(Student, on_delete=models.CASCADE)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    email = models.CharField(max_length=50)


class VerifiedReferral(models.Model):
    """Records a verified referral: referrer gets credit when referred user verifies (OTP)."""
    referrer = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='verified_referrals', db_index=True)
    referred = models.OneToOneField(Student, on_delete=models.CASCADE, related_name='referred_by_verified')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['referrer', 'referred'], name='unique_referrer_referred'),
        ]


class Raffle(models.Model):
    """Raffle campaign; winner chosen by weighted draw (weight = verified referral count in scope)."""
    name = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    drawn_at = models.DateTimeField(null=True, blank=True)
    winner = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True, related_name='raffles_won')


class Student_Events(models.Model):
    event_name = models.CharField(max_length=100)
    description = models.CharField(max_length=2000)
    RSVP = models.IntegerField()
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    date = models.DateTimeField(null=True, blank=True)
    dateposted = models.DateTimeField(default=now, null=True, blank=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Student_Events", blank=True)
    community_mentions = models.ManyToManyField('Communities', related_name="Community_Mentions_In_Student_Events", blank=True)


    def __str__(self):
        return f"{self.student.name} is hosted a student event called: {self.event_name} at {self.date}"
    
class Student_Events_Image(models.Model):
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='student_event_images/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Image for student event: {self.student_event.event_name} by {self.student_event.student.name}"

class Student_Events_Video(models.Model):
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE, related_name='videos')
    video = models.FileField(upload_to='student_event_videos/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Video for student event: {self.student_event.event_name} by {self.student_event.student.name}"


class Communities(models.Model):
    community_name = models.CharField(max_length=50)
    community_bio = models.CharField(max_length=200, null=True, blank=True)
    description = models.CharField(max_length=1000, null=True, blank=True)
    community_interest = models.ManyToManyField(Interests, related_name="Community", null=True, blank=True)
    location = models.ForeignKey(Location, related_name="Location", null=True, blank=True, on_delete=models.DO_NOTHING)
    community_tag = models.CharField(max_length=30, unique=True, blank=True, null=True)
    community_image = models.ImageField(upload_to='communityimages/', null=True, blank=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Community_Bio", blank=True)
    community_mentions = models.ManyToManyField('self', related_name="Community_Mentions_In_Community_Bio", blank=True)



    def __str__(self):
        return f"{self.community_name}"


class Community_Events(models.Model):
    event_name = models.CharField(max_length=100)
    description = models.CharField(max_length=2000)
    RSVP = models.IntegerField()
    date = models.DateTimeField(null=True, blank=True)
    dateposted = models.DateTimeField(default=now, null=True, blank=True)
    community = models.ForeignKey(Communities, on_delete=models.CASCADE)
    poster = models.ForeignKey(Student, on_delete=models.CASCADE, null=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Community_Events", blank=True)
    community_mentions = models.ManyToManyField(Communities, related_name="Community_Mentions_In_Community_Events", blank=True)

    def __str__(self):
        return f"{self.poster.name} posted an event:  {self.event_name} at {self.date}"
    

class Community_Events_Image(models.Model):
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='community_event_images/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Image for community event: {self.community_event.event_name} by {self.community_event.poster.name}"

class Community_Events_Video(models.Model):
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE, related_name='videos')
    video = models.FileField(upload_to='community_event_videos/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Video for community event: {self.community_event.event_name} by {self.community_event.poster.name}"
    
    



class Community_Posts(models.Model):
    post_text = models.CharField(max_length=3000)
    post_date = models.DateField(auto_now_add=True)
    post_time = models.TimeField(auto_now_add=True)
    community = models.ForeignKey(Communities, on_delete=models.CASCADE)
    poster = models.ForeignKey(Student, on_delete=models.CASCADE)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Community_Posts", blank=True)
    community_mentions = models.ManyToManyField(Communities, related_name="Community_Mentions_In_Community_Posts", blank=True)



    def __str__(self):
        return f"{self.poster.name} of {self.community.community_name} posted:  {self.post_text} at {self.post_date} {self.post_time}"
    

class Community_Posts_Image(models.Model):
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='community_post_images/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Image for community post: {self.community_post.post_text} by {self.community_post.poster.name}"

class Community_Posts_Video(models.Model):
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE, related_name='videos')
    video = models.FileField(upload_to='community_post_videos/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Video for community post: {self.community_post.post_text} by {self.community_post.poster.name}"
    

    
    
class Community_Posts_Comment(models.Model):
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE, null=True)
    comment_text = models.CharField(max_length=2000)
    student = models.ForeignKey(Student, on_delete=models.CASCADE, null=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Community_Posts_Comment", blank=True)
    community_mentions = models.ManyToManyField(Communities, related_name="Community_Mentions_In_Community_Posts_Comment", blank=True)
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL, # If a parent is deleted, replies become top-level
        null=True,
        blank=True,
        related_name='replies' # Name for the reverse relation
    )

    def __str__(self):
        post_text = self.community_post.post_text if self.community_post else "Deleted Post"
        student_name = self.student.name if self.student else "Unknown"
        comment_preview = self.comment_text[:50] if self.comment_text else ""
        return f"On {post_text} {student_name} said: {comment_preview} at {self.sent_at}"

class Community_Events_Discussion(models.Model):
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE)
    discussion_text = models.CharField(max_length=2000)
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    sent_at = models.DateTimeField(auto_now_add=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Community_Events_Discussion", blank=True)
    community_mentions = models.ManyToManyField(Communities, related_name="Community_Mentions_In_Community_Events_Discussion", blank=True)
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies' # Use 'replies' for consistency
    )

    def __str__(self):
        return f"On {self.community_event.event_name} {self.student.name} said:  {self.discussion_text} at {self.sent_at}"


class Student_Events_Discussion(models.Model):
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE)
    discussion_text = models.CharField(max_length=2000)
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    sent_at = models.DateTimeField(auto_now_add=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Student_Events_Discussion", blank=True)
    community_mentions = models.ManyToManyField(Communities, related_name="Community_Mentions_In_Student_Events_Discussion", blank=True)
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies' # Use 'replies' for consistency
    )

    def __str__(self):
        return f"On {self.student_event.event_name} {self.student.name} said:  {self.discussion_text} at {self.sent_at}"


class Posts(models.Model):
    context_text = models.CharField(max_length=2000)
    post_date = models.DateTimeField(auto_now_add=True)
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    image = models.ImageField(upload_to='postimages/', null=True, blank=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Posts", blank=True)
    community_mentions = models.ManyToManyField(Communities, related_name="Community_Mentions_In_Posts", blank=True)

    def __str__(self):
        return f"{self.student.name} posted:  {self.context_text}"
    

class PostImages(models.Model):
    post = models.ForeignKey(Posts, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='postimages/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Image for post: {self.post.context_text} by {self.post.student.name}"

class PostVideos(models.Model):
    post = models.ForeignKey(Posts, on_delete=models.CASCADE, related_name='videos')
    video = models.FileField(upload_to='postvideos/')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Video for post: {self.post.context_text} by {self.post.student.name}"

class PostLike(models.Model):
    post = models.ForeignKey('Posts', related_name='likes', on_delete=models.CASCADE)
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    liked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} liked post:  {self.post.context_text}"

class PostComment(models.Model):
    post = models.ForeignKey('Posts', related_name='comments', on_delete=models.CASCADE, null=True)
    student = models.ForeignKey('Student', on_delete=models.CASCADE, null=True)
    comment = models.CharField(max_length=2000)
    commented_at = models.DateTimeField(auto_now_add=True)
    student_mentions = models.ManyToManyField(Student, related_name="Student_Mentions_In_Post_Comments", blank=True)
    community_mentions = models.ManyToManyField(Communities, related_name="Community_Mentions_In_Post_Comments", blank=True)
    parent = models.ForeignKey(
        'self',              # This points to the PostComment model itself
        on_delete=models.SET_NULL, # If a parent comment is deleted, its replies become top-level (null parent)
        null=True,           # Allows a comment to not have a parent (it's a root comment)
        blank=True,          # Allows the field to be empty in forms/admin
        related_name='replies' # Name for the reverse relation: comment.replies.all()
    )

    def __str__(self):
        student_name = self.student.name if self.student else "Unknown"
        post_text = self.post.context_text if self.post else "Deleted Post"
        comment_preview = self.comment[:20] if self.comment else ""
        return f"{student_name} commented on post: {post_text}, saying: {comment_preview}..."


class Friendship(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('ignored', 'Ignored'),
    )

    sender = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='sent_requests')
    receiver = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='received_requests')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['sender', 'receiver']
        db_table = 'friendship'

    def __str__(self):
        return f"{self.sender.name} friend request to {self.receiver.name} is {self.status} "

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .cache_utils import invalidate_friend_snapshot
        invalidate_friend_snapshot(self.sender_id)
        invalidate_friend_snapshot(self.receiver_id)
        from .cache_utils import invalidate_relationship_snapshot
        invalidate_relationship_snapshot(self.sender_id)
        invalidate_relationship_snapshot(self.receiver_id)

    def delete(self, *args, **kwargs):
        sender_id = self.sender_id
        receiver_id = self.receiver_id
        super().delete(*args, **kwargs)
        from .cache_utils import invalidate_friend_snapshot
        invalidate_friend_snapshot(sender_id)
        invalidate_friend_snapshot(receiver_id)
        from .cache_utils import invalidate_relationship_snapshot
        invalidate_relationship_snapshot(sender_id)
        invalidate_relationship_snapshot(receiver_id)


class Membership(models.Model):
    user = models.ForeignKey(Student, on_delete=models.CASCADE)
    community = models.ForeignKey(Communities, on_delete=models.CASCADE)
    date_joined = models.DateTimeField(auto_now_add=True)
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('secondary_admin', 'Secondary Admin'),
        ('member', 'Member')
    )
    role = models.CharField(max_length=15, choices=ROLE_CHOICES, default='member')

    class Meta:
        unique_together = ('user', 'community')

    def __str__(self):
        return f"{self.user.name} in {self.community.community_name} as {self.role}"
#make it so it returns acc words not objects desc. make the membership return name in community as role choices

class DirectMessage(models.Model):
    sender = models.ForeignKey('Student', related_name='sent_messages', on_delete=models.CASCADE)
    receiver = models.ForeignKey('Student', related_name='received_messages', on_delete=models.CASCADE)
    message = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    image = models.ImageField(upload_to='direct_messages_images/', null=True, blank=True)
    post = models.ForeignKey(Posts, on_delete=models.CASCADE, null=True, blank=True)
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE, null=True, blank=True)
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE, null=True, blank=True)
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE, null=True, blank=True)
    student_profile = models.ForeignKey(Student, on_delete=models.CASCADE, null=True, blank=True, related_name='dm_student_profile')
    community_profile = models.ForeignKey(Communities, on_delete=models.CASCADE, null=True, blank=True, related_name='dm_community_profile')
    reply = models.ForeignKey(
        'self', # This allows a message to be a reply to another message
        on_delete=models.SET_NULL, # If the original message is deleted, this becomes null      
        null=True,
        blank=True,
        related_name='replies' # Allows access to replies from the original message
    )

    @property
    def image_url(self):
        return self.image.url if self.image else None

    def __str__(self):
        return f"{self.sender.name} to {self.receiver.name}: {self.message[:50]}..."


class Notification(models.Model):
    recipient = models.ForeignKey('Student', on_delete=models.CASCADE)
    sender = models.ForeignKey('Student', on_delete=models.CASCADE, related_name='notifications_sent', null=True, blank=True)
    content = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    is_read = models.BooleanField(default=False)
    notificationtype = models.ForeignKey('notificationType', on_delete=models.CASCADE, null=True, blank=True)
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE, null=True, blank=True)  # Add this line
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE, null=True, blank=True)  # Add this line
    post = models.ForeignKey(Posts, on_delete=models.CASCADE, null=True, blank=True)  # Add this line
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE, null=True, blank=True)  # Add this line
    # Added FKs for comments/discussions so signals can link them
    post_comment = models.ForeignKey('PostComment', on_delete=models.CASCADE, null=True, blank=True)
    community_post_comment = models.ForeignKey('Community_Posts_Comment', on_delete=models.CASCADE, null=True, blank=True)
    community_event_discussion = models.ForeignKey('Community_Events_Discussion', on_delete=models.CASCADE, null=True, blank=True)
    student_event_discussion = models.ForeignKey('Student_Events_Discussion', on_delete=models.CASCADE, null=True, blank=True)
    
    def __str__(self):
        return f"{self.recipient.name} to received notification"
    
class notificationType(models.Model):
    notification_type = models.CharField(max_length=255)

    def __str__(self):
        return self.notification_type




class CommunityChatMessage(models.Model):

    class Meta:
        indexes = [
                models.Index(fields=['community', '-sent_at', '-id']),  # For pagination
                models.Index(fields=['community', 'student']),  # For filtering by community and sender
            ]


    community = models.ForeignKey('Communities', on_delete=models.CASCADE)
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    image = models.ImageField(upload_to='community_chat_images/', null=True, blank=True)
    post = models.ForeignKey(Posts, on_delete=models.CASCADE, null=True, blank=True)
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE, null=True, blank=True)
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE, null=True, blank=True)
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE, null=True, blank=True)
    student_profile = models.ForeignKey(Student, on_delete=models.CASCADE, null=True, blank=True, related_name='cm_student_profile')
    community_profile = models.ForeignKey(Communities, on_delete=models.CASCADE, null=True, blank=True, related_name='cm_community_profile')
    reply = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies'
    )
    read_by = models.ManyToManyField(
        'Student',
        related_name='read_community_messages',
        blank=True,
        help_text="Users who have read this message"
    )
    
    @property
    def image_url(self):
        return self.image.url if self.image else None

    def __str__(self):
        return f"{self.student.name} in {self.community.community_name}: {self.message[:50]}..."


class GroupChat(models.Model):
    """
    Standalone group chat (WhatsApp-style), independent of Communities.
    """
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True, null=True)
    image = models.ImageField(upload_to='group_chat_images/', null=True, blank=True)
    created_by = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='created_group_chats')
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"GroupChat {self.name} (created by {self.created_by.name})"


class GroupChatMembership(models.Model):
    """
    Membership and role within a GroupChat.
    """
    ROLE_CHOICES = (
        ('owner', 'Owner'),
        ('admin', 'Admin'),
        ('member', 'Member'),
    )

    group = models.ForeignKey(GroupChat, on_delete=models.CASCADE, related_name='memberships')
    member = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='group_memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='member')
    joined_at = models.DateTimeField(auto_now_add=True)
    is_muted = models.BooleanField(default=False)

    class Meta:
        unique_together = ('group', 'member')

    def __str__(self):
        return f"{self.member.name} in {self.group.name} as {self.role}"


class GroupChatMessage(models.Model):
    """
    Messages sent inside a GroupChat.
    Mirrors CommunityChatMessage structure for parity.
    """

    class Meta:
        indexes = [
            models.Index(fields=['group', '-sent_at', '-id']),  # For pagination
            models.Index(fields=['group', 'student']),          # For filtering by group and sender
        ]

    group = models.ForeignKey(GroupChat, on_delete=models.CASCADE, related_name='messages')
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    image = models.ImageField(upload_to='group_chat_images/', null=True, blank=True)
    post = models.ForeignKey(Posts, on_delete=models.CASCADE, null=True, blank=True)
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE, null=True, blank=True)
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE, null=True, blank=True)
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE, null=True, blank=True)
    student_profile = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='gm_student_profile',
    )
    community_profile = models.ForeignKey(
        Communities,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='gm_community_profile',
    )
    reply = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='replies',
    )
    read_by = models.ManyToManyField(
        'Student',
        related_name='read_group_messages',
        blank=True,
        help_text="Users who have read this group message",
    )

    @property
    def image_url(self):
        return self.image.url if self.image else None

    def __str__(self):
        return f"{self.student.name} in group {self.group.name}: {self.message[:50]}..."


class SavedPost(models.Model):
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    post = models.ForeignKey('Posts', on_delete=models.CASCADE)
    saved_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} saved post:  {self.post.context_text}"


class SavedCommunityPost(models.Model):
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    community_post = models.ForeignKey('Community_Posts', on_delete=models.CASCADE)
    saved_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} saved community post:  {self.community_post.post_text}"


class SavedStudentEvents(models.Model):
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    studentevent = models.ForeignKey('Student_Events', on_delete=models.CASCADE)
    saved_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} saved student event:  {self.studentevent.event_name} from {self.studentevent.student}"


class EventRSVP(models.Model):
    event = models.ForeignKey('Student_Events', on_delete=models.CASCADE, related_name='eventrsvp')
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=[('going', 'Going'), ('interested', 'Interested'), ('not_going', 'Not Going')])
    rsvp_at = models.DateTimeField(auto_now_add=True)

class LikeEvent(models.Model):
    event = models.ForeignKey('Student_Events', on_delete=models.CASCADE)
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    liked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.event_name} liked by {self.student.name}"



class CommunityEventRSVP(models.Model):
    event = models.ForeignKey('Community_Events', on_delete=models.CASCADE, related_name='communityeventrsvp')
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=[('going', 'Going'), ('interested', 'Interested'), ('not_going', 'Not Going')])
    rsvp_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.event_name} RSVPed by {self.student.name}"


class LikeCommunityEvent(models.Model):
    event = models.ForeignKey('Community_Events', on_delete=models.CASCADE)
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    liked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.event_name} liked by {self.student.name}"


class LikeCommunityPost(models.Model):
    event = models.ForeignKey('Community_Posts', on_delete=models.CASCADE)
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    liked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event.post_text} liked by {self.student.name}"
    

class Block(models.Model):
    blocker = models.ForeignKey(Student, related_name='blocker', on_delete=models.CASCADE)
    blocked = models.ForeignKey(Student, related_name='blocked', on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('blocker', 'blocked')

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
            invalidate_pair_block_cache,
        )
        # Only invalidate snapshot cache (individual caches removed)
        invalidate_relationship_snapshot(self.blocker_id)
        invalidate_relationship_snapshot(self.blocked_id)
        invalidate_pair_block_cache(self.blocker_id, self.blocked_id)

    def delete(self, *args, **kwargs):
        blocker_id = self.blocker_id
        blocked_id = self.blocked_id
        super().delete(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
            invalidate_pair_block_cache,
        )
        # Only invalidate snapshot cache (individual caches removed)
        invalidate_relationship_snapshot(blocker_id)
        invalidate_relationship_snapshot(blocked_id)
        invalidate_pair_block_cache(blocker_id, blocked_id)

class BookmarkedPosts(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    post = models.ForeignKey(Posts, on_delete=models.CASCADE)
    bookmarked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} bookmarked post: {self.post.context_text}"
    
class BookmarkedCommunityPosts(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    community_post = models.ForeignKey(Community_Posts, on_delete=models.CASCADE)
    bookmarked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} bookmarked community post: {self.community_post.post_text}"
    
class BookmarkedStudentEvents(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    student_event = models.ForeignKey(Student_Events, on_delete=models.CASCADE)
    bookmarked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} bookmarked student event: {self.student_event.event_name}"
    
class BookmarkedCommunityEvents(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    community_event = models.ForeignKey(Community_Events, on_delete=models.CASCADE)
    bookmarked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} bookmarked community event: {self.community_event.event_name}"
    

class Report(models.Model):
    # The user who is making the report
    reporter = models.ForeignKey(
        'Student',  # Or settings.AUTH_USER_MODEL if Student is your custom user model
        on_delete=models.CASCADE,
        related_name='sent_reports'
    )

    # The content or user being reported
    # Using GenericForeignKey for flexibility to report different types of content
    # For this, you'll need `django.contrib.contenttypes`
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        null=True, blank=True # Can be null if reporting a user directly, not specific content
    )
    object_id = models.PositiveIntegerField(
        null=True, blank=True # Can be null if reporting a user directly
    )
    content_object = GenericForeignKey('content_type', 'object_id')
    REPORT_TYPE_CHOICES = (
        ('harassment', 'Harassment / Bullying'),
        ('spam', 'Spam / Unsolicited Content'),
        ('hate_speech', 'Hate Speech / Discrimination'),
        ('nudity', 'Nudity / Sexual Content'),
        ('violence', 'Violence / Threats'),
        ('impersonation', 'Impersonation'),
        ('private_info', 'Sharing Private Information'),
        ('other', 'Other (Please specify)'),
    )
    report_type = models.CharField(max_length=50, choices=REPORT_TYPE_CHOICES)
    description = models.TextField(
        blank=True,
        null=True,
        help_text="Optional: Provide more details about the report."
    )
    report_copy = models.CharField(max_length=2000, null=True, blank=True)

    # Status of the report (e.g., pending, reviewed, resolved, dismissed)
    STATUS_CHOICES = (
        ('pending', 'Pending Review'),
        ('reviewed', 'Reviewed'),
        ('resolved', 'Resolved (Action Taken)'),
        ('dismissed', 'Dismissed (No Action Needed)'),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    ACTION_CHOICES = (
        ('none', 'No Action'),
        ('warn', 'Warn User'),
        ('suspend_1d', 'Suspend User (1 Day)'),
        ('suspend_7d', 'Suspend User (7 Days)'),
        ('ban', 'Ban User'),
        ('content_removed', 'Content Removed'),
        ('content_edited', 'Content Edited'),
        ('report_forwarded', 'Report Forwarded to Law Enforcement'),
    )
    action_taken = models.CharField(
        max_length=50,
        choices=ACTION_CHOICES,
        null=True, blank=True,
        default='none',
    )

    class Meta:
        verbose_name = "Report"
        verbose_name_plural = "Reports"
        # Optional: Prevent duplicate reports by the same user on the exact same content
        # unique_together = ('reporter', 'content_type', 'object_id')
        # However, it might be useful to allow multiple reports from different users on the same content.

    @property
    def reported_student(self):
        """Return the Student who is the subject of the report (for banning), or None."""
        if not self.content_type_id or not self.object_id:
            return None
        try:
            obj = self.content_object
        except Exception:
            return None
        if obj is None:
            return None
        model_name = (self.content_type.model or '').lower()
        if model_name == 'student':
            return obj
        for attr in ('student', 'poster', 'sender'):
            if hasattr(obj, attr):
                s = getattr(obj, attr, None)
                if s is not None and isinstance(s, Student):
                    return s
        return None

    def __str__(self):
        if self.content_type_id and self.object_id:
            try:
                student = self.reported_student
                if student:
                    return f"Report by {self.reporter.name} on user {student.name} - {self.report_type}"
            except Exception:
                pass
            return f"Report by {self.reporter.name} on {self.content_type.model} (ID: {self.object_id}) - {self.report_type}"
        return f"Report by {self.reporter.name} (No specific content/user linked) - {self.report_type}"


class BannedStudents(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    banned_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=2000, null=True, blank=True)

    BAN_REASON_CHOICES = (
        ('harassment', 'Harassment / Bullying'),
        ('spam', 'Spam / Unsolicited Content'),
        ('hate_speech', 'Hate Speech / Discrimination'),
        ('nudity', 'Nudity / Sexual Content'),
        ('violence', 'Violence / Threats'),
        ('impersonation', 'Impersonation'),
        ('private_info', 'Sharing Private Information'),
        ('other', 'Other (Please specify)'),

    )
    banned_until = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        reason = (self.reason or 'No reason given')[:50]
        return f"{self.student.name} — {reason}"


class KindeRefreshToken(models.Model):
    """Stores Kinde refresh tokens so we can revoke them on ban (force re-login)."""
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    refresh_token = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['student'])]

    def __str__(self):
        return f"Kinde session for {self.student.name}"

        

class TempImage(models.Model):
    image = models.ImageField(upload_to='temp_test_uploads/') # Will upload to a 'temp_test_uploads' folder in your GCS bucket

    def __str__(self):
        return self.image.name


class DeviceToken(models.Model):
    user = models.ForeignKey(Student, on_delete=models.CASCADE, null=True, blank=True)
    token = models.CharField(max_length=255)  # Token itself should be unique across all users
    device_type = models.CharField(max_length=50, choices=[('android', 'Android'), ('ios', 'iOS')], default='android')
    device_id = models.CharField(max_length=255, blank=True, null=True)  # Unique device identifier
    device_name = models.CharField(max_length=100, blank=True, null=True)  # e.g., "John's iPhone"
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Remove unique_together constraint - allow multiple tokens per user
        indexes = [
            models.Index(fields=['user', 'is_active']),  # For efficient querying
            models.Index(fields=['token']),
        ]

    def __str__(self):  # Fixed: was _str_
        device_info = self.device_name or f"{self.device_type} device"
        return f"{self.user} - {device_info} - {self.token[:20]}..."
    

class MutedCommunities(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    community = models.ForeignKey(Communities, on_delete=models.CASCADE)
    muted_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
        )
        # Only invalidate snapshot cache (individual caches removed)
        invalidate_relationship_snapshot(self.student_id)

    def delete(self, *args, **kwargs):
        student_id = self.student_id
        super().delete(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
        )
        # Only invalidate snapshot cache (individual caches removed)
        invalidate_relationship_snapshot(student_id)

    def __str__(self):
        return f"{self.student.name} muted community: {self.community.community_name}"
    
class MutedStudents(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='muter')
    muted_student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='muted')
    muted_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
        )
        # Only invalidate snapshot cache (individual caches removed)
        invalidate_relationship_snapshot(self.student_id)

    def delete(self, *args, **kwargs):
        student_id = self.student_id
        super().delete(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
        )
        # Only invalidate snapshot cache (individual caches removed)
        invalidate_relationship_snapshot(student_id)

    def __str__(self):
        return f"{self.student.name} muted student: {self.muted_student.name}"
    

class BlockedByCommunities(models.Model):
    blocked_student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True)
    community = models.ForeignKey(Communities, on_delete=models.SET_NULL, null=True)
    blocked_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
        )
        # Only invalidate snapshot cache (individual caches removed)
        if self.blocked_student_id:
            invalidate_relationship_snapshot(self.blocked_student_id)

    def delete(self, *args, **kwargs):
        blocked_student_id = self.blocked_student_id
        super().delete(*args, **kwargs)
        from .cache_utils import (
            invalidate_relationship_snapshot,
        )
        # Only invalidate snapshot cache (individual caches removed)
        if blocked_student_id:
            invalidate_relationship_snapshot(blocked_student_id)

    def __str__(self):
        return f"{self.blocked_student.name} blocked by community: {self.community.community_name}"
    
class Advertisements(models.Model):
    company_name = models.CharField()
    company_contact = models.CharField()
    ad_header = models.CharField()
    ad_body = models.CharField()
    ad_media = models.ImageField(upload_to='ad_media/', null=True, blank=True)
    ad_locations = models.ManyToManyField(Location, null=True, blank=True)
    ad_regions = models.ManyToManyField(Region, null=True, blank=True)
    date_posted = models.DateTimeField(auto_now_add=True)
    date_taken_off = models.DateTimeField(null=True,blank=True)
    is_active = models.BooleanField(default=True)
    ad_link = models.CharField(null=True, blank=True)

    def __str__(self):
        return f"{self.company_name} - {self.ad_header} till {self.date_taken_off}"


class DataDeletionRequest(models.Model):
    """
    Tracks user data deletion requests. Data is retained for 30 days before permanent deletion.
    """
    student = models.OneToOneField(Student, on_delete=models.CASCADE, related_name='deletion_request')
    requested_at = models.DateTimeField(auto_now_add=True)
    scheduled_deletion_date = models.DateTimeField()
    is_cancelled = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)  # When actual deletion occurred
    
    class Meta:
        ordering = ['-requested_at']
    
    def __str__(self):
        status = "Cancelled" if self.is_cancelled else "Pending"
        return f"Deletion request for {self.student.name} - {status} (scheduled: {self.scheduled_deletion_date})"


class QRScan(models.Model):
    """Tracks smart download / QR code page visits for analytics."""
    timestamp = models.DateTimeField(auto_now_add=True)
    user_agent = models.TextField(blank=True)
    device_type = models.CharField(max_length=20)  # 'ios', 'android', 'desktop'
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    referer = models.URLField(blank=True, max_length=500)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.device_type} @ {self.timestamp}"
