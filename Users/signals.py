from django.db.models.signals import post_save, post_delete, m2m_changed
from django.db.models.fields.files import FileField
from django.dispatch import receiver
from .models import *
from django.core.cache import cache
from .cache_utils import *
import logging
from .firebase_utils import send_push_notification
from django.utils import timezone
from django.db.models import Q
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .serializers import NotificationSerializer
from django.db import transaction
from .tasks import (
    send_direct_message_notification_task,
    send_community_message_notification_task,
    send_student_event_notification_task,
    send_post_like_notification_task,
    send_post_comment_notification_task,
    send_friend_request_notification_task,
    send_community_event_notification_task,
    send_community_post_like_notification_task,
    send_community_post_comment_notification_task,
    send_community_new_member_notification_task,
    process_mentions_task,
    send_student_event_rsvp_notification_task,
    send_community_event_rsvp_notification_task,
    send_student_event_discussion_notification_task,
    send_community_event_discussion_notification_task,
)

logger = logging.getLogger(__name__)


def _delete_storage_files_on_instance_delete(sender, instance, **kwargs):
    """Enqueue deletion of file/image from GCS on the Celery worker (helper server), not on the main server."""
    paths = []
    for field in sender._meta.fields:
        if isinstance(field, FileField):
            file_attr = getattr(instance, field.name, None)
            if file_attr and file_attr.name:
                paths.append(file_attr.name)
    if paths:
        from .tasks import delete_storage_files_task
        delete_storage_files_task.delay(paths)


# Models that have FileField/ImageField — delete file from bucket when instance is deleted
_STORAGE_MODELS = (
    Posts,
    PostImages,
    PostVideos,
    Student,
    Student_Events_Image,
    Student_Events_Video,
    Communities,
    Community_Events_Image,
    Community_Events_Video,
    Community_Posts_Image,
    Community_Posts_Video,
    DirectMessage,
    CommunityChatMessage,
    GroupChatMessage,
    TempImage,
    Advertisements,
)
for _model in _STORAGE_MODELS:
    post_delete.connect(_delete_storage_files_on_instance_delete, sender=_model, dispatch_uid=f"delete_storage_files_{_model.__name__}")


def send_websocket_notification(notification_instance, recipient_kinde_id):
    """
    Send a WebSocket notification to a user when a notification is created.
    This handles notifications for posts, community posts, student events, and community events.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning("Channel layer not available for WebSocket notification")
            return
        
        # Serialize the notification
        serializer = NotificationSerializer(notification_instance)
        notification_data = serializer.data
        
        # Send to the user's notification group
        async_to_sync(channel_layer.group_send)(
            f'notifications_{recipient_kinde_id}',
            {
                'type': 'send_notification',
                'notification_data': notification_data
            }
        )
    except Exception as e:
        logger.error(f"Error sending WebSocket notification: {e}")


@receiver(post_save, sender=PostLike)
def notify_post_like(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(
            lambda: send_post_like_notification_task.delay(instance.id)
        )


@receiver(post_save, sender=PostComment)
def notify_post_comment(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(
            lambda: send_post_comment_notification_task.delay(instance.id)
        )

@receiver(post_save, sender=Friendship)
def notify_friend_request(sender, instance, created, **kwargs):
    """
    Send notifications for friend request events
    """
    logger.info(f"=== FRIEND REQUEST SIGNAL TRIGGERED ===")
    logger.info(f"Created: {created}, Status: {instance.status}, Sender: {instance.sender.name}, Receiver: {instance.receiver.name}")
    
    try:
        # Handle new friend request
        if created and instance.status == 'pending':
            transaction.on_commit(
                lambda: send_friend_request_notification_task.delay(instance.id, 'pending')
            )
            
        # Handle friend request acceptance (both created=True and updated status)
        elif (created and instance.status == 'accepted') or (not created and instance.status == 'accepted'):
            
            transaction.on_commit(
                lambda: send_friend_request_notification_task.delay(instance.id, 'accepted')
            )
                    
    except Exception as e:
        logger.error(f"Error in friend request notification signal: {e}")



@receiver(post_save, sender=Notification)
def cache_notification(sender, instance, **kwargs):
    key = f'notifications:{instance.recipient.id}'
    notifications = get_cache(key) or []

    # Store only the latest 50 notifications in Redis
    notifications.insert(0, {
        'id': instance.id,
        'content': instance.content,  # Ensure this matches the field name in your Notification model
        'created_at': instance.created_at.isoformat(),  # Ensure this matches the field name in your Notification model
    })
    set_cache(key, notifications[:50], timeout=3600)  # Cache for 1 hour


@receiver(post_save, sender=Notification)
def invalidate_notifications_cache_on_save(sender, instance, created, **kwargs):
    if created:
        key = f'notifications:{instance.recipient.id}'
        invalidate_cache(key)


@receiver(post_save, sender=GroupChatMessage)
def notify_group_chat_message(sender, instance, created, **kwargs):
    """
    Trigger background notification processing when a new group chat message is created.
    """
    if created:
        try:
            transaction.on_commit(
                lambda: send_group_message_notification_task.delay(instance.id)
            )
        except Exception as e:
            logger.error(f"Error scheduling group chat notification task: {e}")


@receiver(post_save, sender=Student)
def invalidate_student_cache_on_save(sender, instance, **kwargs):
    key = f'student_info:{instance.id}'
    invalidate_cache(key)

@receiver(post_delete, sender=Student)
def invalidate_student_cache_on_delete(sender, instance, **kwargs):
    key = f'student_info:{instance.id}'
    invalidate_cache(key)

@receiver(post_save, sender=Posts)
def invalidate_student_posts_cache_on_save(sender, instance, **kwargs):
    key = f'student_posts:{instance.student.id}'
    invalidate_cache(key)

@receiver(post_delete, sender=Posts)
def invalidate_student_posts_cache_on_delete(sender, instance, **kwargs):
    key = f'student_posts:{instance.student.id}'
    invalidate_cache(key)


@receiver(post_save, sender=Student_Events)
def invalidate_student_event_cache_on_save(sender, instance, **kwargs):
    key = f'student event:{instance.student.id}'
    invalidate_cache(key)

@receiver(post_delete, sender=Student_Events)
def invalidate_student_event_cache_on_delete(sender, instance, **kwargs):
    key = f'student event:{instance.student.id}'
    invalidate_cache(key)


@receiver(post_save, sender=PostComment)
@receiver(post_delete, sender=PostComment)
def invalidate_post_comments_cache(sender, instance, **kwargs):
    # Some legacy comments may have no associated post; skip cache invalidation safely in that case.
    if not instance.post_id:
        return
    key = f'post comments:{instance.post_id}'
    invalidate_cache(key)

# Invalidate cache when a new community post is created or deleted
@receiver(post_save, sender=Community_Posts)
@receiver(post_delete, sender=Community_Posts)
def invalidate_community_posts_cache(sender, instance, **kwargs):
    key = f'community post:{instance.community.id}'
    invalidate_cache(key)

@receiver(post_save, sender=Community_Posts_Comment)
@receiver(post_delete, sender=Community_Posts_Comment)
def invalidate_community_post_comments_cache(sender, instance, **kwargs):
    # Some legacy comments may have no associated community_post; skip safely in that case.
    if not instance.community_post_id:
        return
    key = f'communities post comments:{instance.community_post_id}'
    invalidate_cache(key)

# Invalidate cache when a new student event is created or deleted
@receiver(post_save, sender=Student_Events)
@receiver(post_delete, sender=Student_Events)
def invalidate_student_events_cache(sender, instance, **kwargs):
    key = f'student event:{instance.student.id}'
    invalidate_cache(key)

# Invalidate cache when a new community event is created or deleted
@receiver(post_save, sender=Community_Events)
@receiver(post_delete, sender=Community_Events)
def invalidate_community_events_cache(sender, instance, **kwargs):
    key = f'community event:{instance.community.id}'
    invalidate_cache(key)

# Invalidate cache when a new student event discussion is created or deleted


@receiver(post_delete, sender=Student_Events_Discussion)
@receiver(post_save, sender=Student_Events_Discussion)
def invalidate_student_event_discussions_cache(sender, instance, **kwargs):
    student_event = instance.student_event.id
    pattern = f'student event discussions:{student_event}:requester:*'
    cache.delete_pattern(pattern)


@receiver(post_delete, sender=Community_Events_Discussion)
@receiver(post_save, sender=Community_Events_Discussion)
def invalidate_community_event_discussions_cache(sender, instance, **kwargs):
    community_event = instance.community_event.id
    pattern = f'communities event discussions:{community_event}:requester:*'
    cache.delete_pattern(pattern)


@receiver(post_delete, sender=Communities)
@receiver(post_save, sender=Communities)
def invalidate_community_info_cache(sender, instance, **kwargs):
    community_id = instance.id
    pattern = f'community_info:{community_id}:requester:*'
    cache.delete_pattern(pattern)

@receiver(post_delete, sender=Student)
@receiver(post_save, sender=Student)
def invalidate_student_info_cache(sender, instance, **kwargs):
    student_id = instance.id
    pattern = f'student_info:{student_id}:requester:*'
    cache.delete_pattern(pattern)






#mute
@receiver(post_save, sender=Community_Events)
def notify_community_event(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(
            lambda: send_community_event_notification_task.delay(instance.id)
        )

#mute
@receiver(post_save, sender=Student_Events)
def notify_student_event(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(
            lambda: send_student_event_notification_task.delay(instance.id)
        )

@receiver(post_save, sender=LikeCommunityPost)
def notify_community_members_post_like(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(
            lambda: send_community_post_like_notification_task.delay(instance.id)
        )

@receiver(post_save, sender=Community_Posts_Comment)
def notify_community_members_post_comment(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(
            lambda: send_community_post_comment_notification_task.delay(instance.id)
        )
        
        # Broadcast comment update via WebSocket
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from .serializers import CommunityPostCommentSerializer
        
        channel_layer = get_channel_layer()
        if channel_layer:
            comment_data = CommunityPostCommentSerializer(instance, context={'kinde_user_id': None}).data
            async_to_sync(channel_layer.group_send)(
                f'community_post_comments_{instance.community_post.id}',
                {
                    'type': 'comment_added',
                    'comment_data': comment_data
                }
            )

@receiver(post_save, sender=Membership)
def notify_community_admins_new_member(sender, instance, created, **kwargs):
    if created and instance.role == 'member':  # Only notify when membership is approved
        transaction.on_commit(
            lambda: send_community_new_member_notification_task.delay(instance.id)
        )

# Optional: Batch notification function for better performance
def send_bulk_push_notifications(users, title, body, data):
    """
    Send notifications to multiple users efficiently
    """
    try:
        # Get all active tokens for these users
        device_tokens = DeviceToken.objects.filter(
            user__in=users,
            is_active=True
        ).values_list('token', flat=True)
        
        if not device_tokens:
            return
            
        # Send in batches if you have a bulk send function
        # send_bulk_push_notification(list(device_tokens), title, body, data)
        
        # Or send individually
        for token in device_tokens:
            try:
                send_push_notification(token=token, title=title, body=body, data=data)
            except Exception as e:
                logger.error(f"Failed to send notification to token {token[:10]}...: {e}")
                
    except Exception as e:
        logger.error(f"Error sending bulk push notifications: {e}")


def handle_mention_notifications(instance, pk_set, mention_type, content_type):
    """
    Queue mention notifications to be processed asynchronously.
    """
    logger.info(
        "Mention notification triggered: instance=%s, pk_set=%s, mention_type=%s, content_type=%s",
        instance,
        pk_set,
        mention_type,
        content_type,
    )
    
    if not pk_set:
        return
    
    instance_id = getattr(instance, "id", None)
    if not instance_id:
        logger.warning("Instance has no primary key; skipping mention notification.")
        return

    model_label = instance._meta.label_lower
    id_list = list(pk_set)

    transaction.on_commit(
        lambda: process_mentions_task.delay(
            model_label,
            instance_id,
            mention_type,
            content_type,
            id_list,
        )
                )

def _get_parent_info(instance, content_type):
    """Get parent ID and type for deep linking based on content type"""
    if content_type == 'post':
        # For posts, the parent_id is the post itself
        return instance.id, 'post'
    elif content_type == 'comment':
        # For post comments, the parent_id is the post ID
        return instance.post.id if instance.post else None, 'post'
    elif content_type == 'community_post':
        # For community posts, the parent_id is the community post itself
        return instance.id, 'community_post'
    elif content_type == 'community_post_comment':
        # For community post comments, the parent_id is the community post ID
        return instance.community_post.id if instance.community_post else None, 'community_post'
    elif content_type == 'student_event':
        # For student events, the parent_id is the event itself
        return instance.id, 'student_event'
    elif content_type == 'community_event':
        # For community events, the parent_id is the event itself
        return instance.id, 'community_event'
    elif content_type == 'student_event_discussion':
        # For student event discussions, the parent_id is the student event ID
        return instance.student_event.id if instance.student_event else None, 'student_event'
    elif content_type == 'community_event_discussion':
        # For community event discussions, the parent_id is the community event ID
        return instance.community_event.id if instance.community_event else None, 'community_event'
    return None, None

def _get_parent_id(instance, content_type):
    """Get parent ID for deep linking based on content type (legacy function)"""
    parent_id, _ = _get_parent_info(instance, content_type)
    return parent_id

def _get_content_preview(instance, content_type):
    """Get preview text based on content type"""
    if content_type == 'post':
        return instance.context_text[:100]
    elif content_type == 'comment':
        return instance.comment[:100]
    elif content_type == 'community_post':
        return instance.post_text[:100]
    elif content_type == 'community_post_comment':
        return instance.comment_text[:100]
    elif content_type in ['community_event_discussion', 'student_event_discussion']:
        return instance.discussion_text[:100]
    elif content_type in ['student_event', 'community_event']:
        # Check if instance has event_name attribute before accessing it
        if hasattr(instance, 'event_name'):
            return f"{instance.event_name}: {instance.description[:80]}"
        else:
            # Fallback for instances that don't have event_name (like Community_Posts_Comment)
            return str(instance)[:100]
    return ""

def _get_student_mention_content(instance, creator, mentioned_student, content_type):
    """Generate notification content for student mentions"""
    content_preview = _get_content_preview(instance, content_type)
    
    if content_type == 'post':
        title = f"You were mentioned by {creator.name}"
        body = f"in their post: \"{content_preview[:50]}{'...' if len(content_preview) > 50 else ''}\""
        notification_content = f"You were mentioned in a post by {creator.name}"
    elif content_type == 'comment':
        title = f"You were mentioned by {creator.name}"
        body = f"in a comment: \"{content_preview[:50]}{'...' if len(content_preview) > 50 else ''}\""
        notification_content = f"You were mentioned in a comment by {creator.name}"
    elif content_type == 'community_post':
        title = f"You were mentioned by {creator.name}"
        body = f"in a community post: \"{content_preview[:50]}{'...' if len(content_preview) > 50 else ''}\""
        notification_content = f"You were mentioned in a community post by {creator.name}"
    elif content_type == 'community_post_comment':
        title = f"You were mentioned by {creator.name}"
        body = f"in a community post comment: \"{content_preview[:50]}{'...' if len(content_preview) > 50 else ''}\""
        notification_content = f"You were mentioned in a community post comment by {creator.name}"
    elif content_type == 'community_event_discussion':
        title = f"You were mentioned by {creator.name}"
        body = f"in event discussion: \"{content_preview[:50]}{'...' if len(content_preview) > 50 else ''}\""
        notification_content = f"You were mentioned in a community event discussion by {creator.name}"
    elif content_type == 'student_event_discussion':
        title = f"You were mentioned by {creator.name}"
        body = f"in event discussion: \"{content_preview[:50]}{'...' if len(content_preview) > 50 else ''}\""
        notification_content = f"You were mentioned in a student event discussion by {creator.name}"
    elif content_type == 'student_event':
        title = f"You were mentioned by {creator.name}"
        if hasattr(instance, 'event_name'):
            body = f"in their event: {instance.event_name}"
            notification_content = f"You were mentioned in the event '{instance.event_name}' by {creator.name}"
        else:
            body = f"in their event"
            notification_content = f"You were mentioned in an event by {creator.name}"
    elif content_type == 'community_event':
        title = f"You were mentioned by {creator.name}"
        if hasattr(instance, 'event_name'):
            body = f"in a community event: {instance.event_name}"
            notification_content = f"You were mentioned in the community event '{instance.event_name}' by {creator.name}"
        else:
            body = f"in a community event"
            notification_content = f"You were mentioned in a community event by {creator.name}"
    
    return title, body, notification_content

def _get_community_mention_content(instance, creator, community, content_type):
    """Generate notification content for community mentions"""
    if content_type == 'post':
        title = f"{community.community_name} was mentioned"
        body = f"by {creator.name} in their post"
        notification_content = f"Your community {community.community_name} was mentioned by {creator.name} in a post"
    elif content_type == 'comment':
        title = f"{community.community_name} was mentioned"
        body = f"by {creator.name} in a comment"
        notification_content = f"Your community {community.community_name} was mentioned by {creator.name} in a comment"
    elif content_type == 'community_post':
        title = f"{community.community_name} was mentioned"
        body = f"by {creator.name} in a community post"
        notification_content = f"Your community {community.community_name} was mentioned by {creator.name} in a community post"
    elif content_type == 'community_post_comment':
        title = f"{community.community_name} was mentioned"
        body = f"by {creator.name} in a community post comment"
        notification_content = f"Your community {community.community_name} was mentioned by {creator.name} in a community post comment"
    elif content_type == 'community_event_discussion':
        title = f"{community.community_name} was mentioned"
        body = f"by {creator.name} in event discussion"
        notification_content = f"Your community {community.community_name} was mentioned by {creator.name} in a community event discussion"
    elif content_type == 'student_event_discussion':
        title = f"{community.community_name} was mentioned"
        body = f"by {creator.name} in event discussion"
        notification_content = f"Your community {community.community_name} was mentioned by {creator.name} in a student event discussion"
    elif content_type == 'student_event':
        title = f"{community.community_name} was mentioned"
        if hasattr(instance, 'event_name'):
            body = f"by {creator.name} in their event: {instance.event_name}"
            notification_content = f"Your community {community.community_name} was mentioned in the event '{instance.event_name}' by {creator.name}"
        else:
            body = f"by {creator.name} in their event"
            notification_content = f"Your community {community.community_name} was mentioned in an event by {creator.name}"
    elif content_type == 'community_event':
        title = f"{community.community_name} was mentioned"
        if hasattr(instance, 'event_name'):
            body = f"by {creator.name} in a community event: {instance.event_name}"
            notification_content = f"Your community {community.community_name} was mentioned in the community event '{instance.event_name}' by {creator.name}"
        else:
            body = f"by {creator.name} in a community event"
            notification_content = f"Your community {community.community_name} was mentioned in a community event by {creator.name}"
    
    return title, body, notification_content

def _get_notification_type_id(content_type, mention_type):
    """Get notification type ID based on content and mention type"""
    type_mapping = {
        ('post', 'student'): 4,
        ('post', 'community'): 5,
        ('comment', 'student'): 6,
        ('comment', 'community'): 7,
        ('student_event', 'student'): 8,
        ('student_event', 'community'): 9,
        ('community_event', 'student'): 10,
        ('community_event', 'community'): 11,
        ('community_post', 'student'): 12,
        ('community_post', 'community'): 13,
        ('community_post_comment', 'student'): 14,
        ('community_post_comment', 'community'): 15,
        ('community_event_discussion', 'student'): 16,
        ('community_event_discussion', 'community'): 17,
        ('student_event_discussion', 'student'): 18,
        ('student_event_discussion', 'community'): 19,
    }
    return type_mapping.get((content_type, mention_type), 4)  # Default to 4

def _get_notification_foreign_key(instance, content_type):
    """Get the foreign key field for the notification based on content type"""
    # You may need to add these fields to your Notification model
    if content_type == 'post':
        return {'post': instance} if hasattr(Notification, 'post') else {}
    elif content_type == 'comment':
        return {'post_comment': instance} if hasattr(Notification, 'post_comment') else {}
    elif content_type == 'community_post':
        return {'community_post': instance} if hasattr(Notification, 'community_post') else {}
    elif content_type == 'community_post_comment':
        return {'community_post_comment': instance} if hasattr(Notification, 'community_post_comment') else {}
    elif content_type == 'community_event_discussion':
        return {'community_event_discussion': instance} if hasattr(Notification, 'community_event_discussion') else {}
    elif content_type == 'student_event_discussion':
        return {'student_event_discussion': instance} if hasattr(Notification, 'student_event_discussion') else {}
    elif content_type == 'student_event':
        return {'student_event': instance} if hasattr(Notification, 'student_event') else {}
    elif content_type == 'community_event':
        return {'community_event': instance} if hasattr(Notification, 'community_event') else {}
    return {}

# =============================================================================
# SIGNAL HANDLERS FOR EACH MODEL
# =============================================================================

# Posts mentions
@receiver(m2m_changed, sender=Posts.student_mentions.through)
def notify_post_student_mentions(sender, instance, action, pk_set, **kwargs):
    logger.info(f"Post student mention signal triggered: action={action}, pk_set={pk_set}")
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'post')

@receiver(m2m_changed, sender=Posts.community_mentions.through)
def notify_post_community_mentions(sender, instance, action, pk_set, **kwargs):
    logger.info(f"Post community mention signal triggered: action={action}, pk_set={pk_set}")
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'post')

# PostComment mentions
@receiver(m2m_changed, sender=PostComment.student_mentions.through)
def notify_comment_student_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'comment')

@receiver(m2m_changed, sender=PostComment.community_mentions.through)
def notify_comment_community_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'comment')

# Student_Events mentions
@receiver(m2m_changed, sender=Student_Events.student_mentions.through)
def notify_student_event_student_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'student_event')

@receiver(m2m_changed, sender=Student_Events.community_mentions.through)
def notify_student_event_community_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'student_event')

# Community_Events mentions
@receiver(m2m_changed, sender=Community_Events.student_mentions.through)
def notify_community_event_student_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'community_event')

@receiver(m2m_changed, sender=Community_Events.community_mentions.through)
def notify_community_event_community_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'community_event')

# Community_Posts mentions
@receiver(m2m_changed, sender=Community_Posts.student_mentions.through)
def notify_community_post_student_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'community_post')

@receiver(m2m_changed, sender=Community_Posts.community_mentions.through)
def notify_community_post_community_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'community_post')

# Community_Posts_Comment mentions
@receiver(m2m_changed, sender=Community_Posts_Comment.student_mentions.through)
def notify_community_post_comment_student_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'community_post_comment')

@receiver(m2m_changed, sender=Community_Posts_Comment.community_mentions.through)
def notify_community_post_comment_community_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'community_post_comment')

# Community_Events_Discussion mentions
@receiver(m2m_changed, sender=Community_Events_Discussion.student_mentions.through)
def notify_community_event_discussion_student_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'community_event_discussion')

@receiver(m2m_changed, sender=Community_Events_Discussion.community_mentions.through)
def notify_community_event_discussion_community_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'community_event_discussion')

# Student_Events_Discussion mentions
@receiver(m2m_changed, sender=Student_Events_Discussion.student_mentions.through)
def notify_student_event_discussion_student_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'student', 'student_event_discussion')

@receiver(m2m_changed, sender=Student_Events_Discussion.community_mentions.through)
def notify_student_event_discussion_community_mentions(sender, instance, action, pk_set, **kwargs):
    if action == "post_add":
        handle_mention_notifications(instance, pk_set, 'community', 'student_event_discussion')


# =============================================================================
# MESSAGE NOTIFICATION SIGNALS
# =============================================================================

# @receiver(post_save, sender=DirectMessage)
# def notify_direct_message(sender, instance, created, **kwargs):
#     """
#     Send push notification when a direct message is received
#     """
#     if created:
#         sender_user = instance.sender
#         receiver_user = instance.receiver
        
#         # Don't send notification if user sends message to themselves
#         if sender_user == receiver_user:
#             return
        
#         # Check if receiver has muted the sender
#         is_muted = MutedStudents.objects.filter(
#             student=receiver_user,
#             muted_student=sender_user
#         ).exists()
        
#         if is_muted:
#             return
        
#         # Prepare notification content
#         message_preview = instance.message[:100] + "..." if len(instance.message) > 100 else instance.message
#         title = f"New message from {sender_user.name}"
#         body = message_preview
        
#         notification_data = {
#             "type": "direct_message",
#             "message_id": str(instance.id),
#             "sender_id": str(sender_user.id),
#             "sender_name": sender_user.name,
#             "sender_username": getattr(sender_user, 'username', ''),
#             "sender_profile_picture": sender_user.profile_image.url if sender_user.profile_image else None,
#             "message_preview": message_preview,
#             "timestamp": timezone.now().isoformat(),
#         }
        
#         # Send push notification only (no database storage)
#         send_push_notifications_to_user(
#             user=receiver_user,
#             title=title,
#             body=body,
#             data=notification_data
#         )
        
#         # Broadcast unified chats update to both sender and receiver
#         from channels.layers import get_channel_layer
#         from asgiref.sync import async_to_sync
        
#         channel_layer = get_channel_layer()
#         if channel_layer:
#             update_data = {
#                 'conversation_type': 'direct_chat',
#                 'conversation_target_id': sender_user.kinde_user_id,
#                 'last_message_text': message_preview,
#                 'last_message_sender_name': sender_user.name,
#                 'last_message_timestamp': instance.timestamp.isoformat()
#             }
            
#             # Update unified chats for receiver
#             async_to_sync(channel_layer.group_send)(
#                 f'unified_chats_{receiver_user.kinde_user_id}',
#                 {
#                     'type': 'unified_chats_update',
#                     'update_data': update_data
#                 }
#             )
            
#             # Update unified chats for sender
#             async_to_sync(channel_layer.group_send)(
#                 f'unified_chats_{receiver_user.kinde_user_id}',  # ✓ Correct
#                 {
#                     'type': 'unified_chats_update',
#                     'update_data': update_data
#                 }
#             )

@receiver(post_save, sender=DirectMessage)
def notify_direct_message(sender, instance, created, **kwargs):
    """
    Send push notification when a direct message is received
    """
    if not created:
            return
        
    transaction.on_commit(lambda: send_direct_message_notification_task.delay(instance.id))


@receiver(post_save, sender=CommunityChatMessage)
def notify_community_chat_message(sender, instance, created, **kwargs):
    """
    Send push notification when a community chat message is posted
    """
    if not created:
            return
        
    transaction.on_commit(lambda: send_community_message_notification_task.delay(instance.id))


# =============================================================================
# EVENT RSVP NOTIFICATION SIGNALS
# =============================================================================

@receiver(post_save, sender=EventRSVP)
def notify_student_event_rsvp(sender, instance, created, **kwargs):
    """
    Notify student event creator when someone RSVPs to their event
    """
    logger.info(f"Student Event RSVP signal triggered: created={created}, event_id={instance.event.id if instance.event else 'None'}, student_id={instance.student.id if instance.student else 'None'}")
    if created:
        transaction.on_commit(
            lambda: send_student_event_rsvp_notification_task.delay(instance.id)
        )


@receiver(post_save, sender=CommunityEventRSVP)
def notify_community_event_rsvp(sender, instance, created, **kwargs):
    """
    Notify community event creator and community admins when someone RSVPs to their event
    """
    logger.info(f"Community Event RSVP signal triggered: created={created}, event_id={instance.event.id if instance.event else 'None'}, student_id={instance.student.id if instance.student else 'None'}")
    if created:
        transaction.on_commit(
            lambda: send_community_event_rsvp_notification_task.delay(instance.id)
        )


# =============================================================================
# EVENT DISCUSSION NOTIFICATION SIGNALS
# =============================================================================

@receiver(post_save, sender=Student_Events_Discussion)
def notify_student_event_discussion(sender, instance, created, **kwargs):
    """
    Notify student event creator when someone discusses their event
    """
    logger.info(f"Student Event Discussion signal triggered: created={created}, event_id={instance.student_event.id if instance.student_event else 'None'}, student_id={instance.student.id if instance.student else 'None'}")
    if created:
        transaction.on_commit(
            lambda: send_student_event_discussion_notification_task.delay(instance.id)
        )


@receiver(post_save, sender=Community_Events_Discussion)
def notify_community_event_discussion(sender, instance, created, **kwargs):
    """
    Notify community event creator and community admins when someone discusses their event
    """
    logger.info(f"Community Event Discussion signal triggered: created={created}, event_id={instance.community_event.id if instance.community_event else 'None'}, student_id={instance.student.id if instance.student else 'None'}")
    if created:
        transaction.on_commit(
            lambda: send_community_event_discussion_notification_task.delay(instance.id)
        )