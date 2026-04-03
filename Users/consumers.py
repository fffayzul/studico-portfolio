import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.shortcuts import get_object_or_404
from .models import *
from .serializers import *
from django.db import models
from django.db.models import Q
import logging
import asyncio
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.conf import settings
from django.utils import timezone as django_timezone
from .firebase_utils import send_push_notifications_to_user


logger = logging.getLogger(__name__)




async def get_student_from_kinde_id(kinde_user_id):
    from Users.models import Student # Still deferred import
    try:
        # Use .aget() for single object retrieval
        return await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return None

# Create a direct message
async def create_direct_message(sender_obj, receiver_obj, message_text=None, image_file=None, reply=None, post=None, community_post=None, student_event=None, community_event=None, student_profile=None, community_profile=None):
    from Users.models import DirectMessage # Still deferred import
    if not message_text and not image_file:
        raise ValueError("Cannot create empty message (no text or image).")
    # Use .acreate() for creating objects
    return await DirectMessage.objects.acreate(
        sender=sender_obj,
        receiver=receiver_obj,
        message=message_text,
        image=image_file,
        reply=reply,
        post=post,
        community_post=community_post,
        student_event=student_event,
        community_event=community_event,
        student_profile=student_profile,
        community_profile=community_profile
    )

# Create a community message



async def create_community_message(community_obj, student_obj, message_text=None, image_file=None, reply=None, post=None, community_post=None, student_event=None, community_event=None, student_profile=None, community_profile=None):
    from Users.models import CommunityChatMessage  # Deferred import to avoid circular deps

    if not message_text and not image_file:
        raise ValueError("Cannot create empty message (no text or image).")

    # Create the message asynchronously
    message = await CommunityChatMessage.objects.acreate(
        community=community_obj,
        student=student_obj,
        message=message_text,
        image=image_file,
        reply=reply,
        post=post,
        community_post=community_post,
        student_event=student_event,
        community_event=community_event,
        student_profile=student_profile,
        community_profile=community_profile
    )

    # ✅ Re-fetch with all needed relations preloaded
    message_with_relations = await CommunityChatMessage.objects.select_related(
        'student', 
        'community', 
        'reply', 
        'reply__student'
    ).aget(pk=message.pk)

    return message_with_relations


# Create a direct message
async def create_direct_message_for_sharable(sender_obj, receiver_obj, message_text=None, image_file=None, reply=None, post=None, community_post=None, student_event=None, community_event=None, student_profile=None, community_profile=None):
    from Users.models import DirectMessage # Still deferred import
    
    # Use .acreate() for creating objects
    return await DirectMessage.objects.acreate(
        sender=sender_obj,
        receiver=receiver_obj,
        message=message_text,
        image=image_file,
        reply=reply,
        post=post,
        community_post=community_post,
        student_event=student_event,
        community_event=community_event,
        student_profile=student_profile,
        community_profile=community_profile
    )

# Create a community message



async def create_community_message_for_sharable(community_obj, student_obj, message_text=None, image_file=None, reply=None, post=None, community_post=None, student_event=None, community_event=None, student_profile=None, community_profile=None):
    from Users.models import CommunityChatMessage  # Deferred import to avoid circular deps

    # Create the message asynchronously
    message = await CommunityChatMessage.objects.acreate(
        community=community_obj,
        student=student_obj,
        message=message_text,
        image=image_file,
        reply=reply,
        post=post,
        community_post=community_post,
        student_event=student_event,
        community_event=community_event,
        student_profile=student_profile,
        community_profile=community_profile
    )

    # ✅ Re-fetch with all needed relations preloaded
    message_with_relations = await CommunityChatMessage.objects.select_related(
        'student', 
        'community', 
        'reply', 
        'reply__student'
    ).aget(pk=message.pk)

    return message_with_relations


async def get_recent_community_messages(community, count=50, kinde_user_id=None):
    """
    Fetch recent community messages with all necessary relations preloaded 
    to avoid sync DB calls inside async context.
    """
    def get_serialized_messages():
        messages_queryset = CommunityChatMessage.objects.filter(
            community=community
        ).select_related(
            'student', 'community', 'reply', 'reply__student', 
            'post', 'community_post', 'student_event', 'community_event',
            'student_profile', 'community_profile'
        ).prefetch_related('read_by').order_by('-sent_at')[:count]
        
        serializer = CommunityChatMessageSerializer(
            messages_queryset, 
            many=True, 
            context={'request': None, 'kinde_user_id': kinde_user_id}
        )
        return serializer.data
    
    return await sync_to_async(get_serialized_messages)()


async def get_recent_group_messages(group, count=50, kinde_user_id=None):
    """
    Fetch recent group chat messages with relations preloaded,
    to avoid sync DB calls inside async context.
    """
    from Users.models import GroupChatMessage
    from Users.serializers import GroupChatMessageSerializer

    def get_serialized_messages():
        messages_queryset = GroupChatMessage.objects.filter(
            group=group
        ).select_related(
            'student', 'group', 'reply', 'reply__student',
            'post', 'community_post', 'student_event', 'community_event',
            'student_profile', 'community_profile'
        ).prefetch_related('read_by').order_by('-sent_at', '-id')[:count]
        serializer = GroupChatMessageSerializer(
            list(messages_queryset),
            many=True,
            context={'request': None, 'kinde_user_id': kinde_user_id}
        )
        return serializer.data

    return await sync_to_async(get_serialized_messages)()


# --- You will need to apply the same fix to get_recent_direct_messages ---
async def get_recent_direct_messages(user1, user2, count=50):
    """
    Fetch recent direct messages between two users with all necessary relations preloaded 
    to avoid sync DB calls inside async context.
    """
    def get_serialized_messages():
        messages_queryset = DirectMessage.objects.filter(
            Q(sender=user1, receiver=user2) | Q(sender=user2, receiver=user1)
        ).select_related(
            'sender', 'receiver', 'reply', 'reply__sender', 'reply__receiver',
            'post', 'community_post', 'student_event', 'community_event',
            'student_profile', 'community_profile'
        ).order_by('-timestamp')[:count]
        
        serializer = DirectMessageSerializer(
            messages_queryset, 
            many=True, 
            context={'request': None}
        )
        return serializer.data
    
    return await sync_to_async(get_serialized_messages)()


async def create_community_message_with_rate_limit(community_obj, student_obj, message_text=None, image_file=None, reply=None):
    """Rate-limited version of create_community_message"""
    rate_limit_key = f"community_chat_rate_{student_obj.id}_{community_obj.id}"
    message_count = cache.get(rate_limit_key, 0)
    
    if message_count >= 30:  # 30 messages per minute per community
        raise ValueError("Rate limit exceeded. Please slow down.")
    
    cache.set(rate_limit_key, message_count + 1, 60)  # 60 second window
    
    return await create_community_message(community_obj, student_obj, message_text, image_file, reply)

async def create_direct_message_with_rate_limit(sender_obj, receiver_obj, message_text=None, image_file=None, reply=None):
    """Rate-limited version of create_direct_message"""
    rate_limit_key = f"direct_chat_rate_{sender_obj.id}"
    message_count = cache.get(rate_limit_key, 0)
    
    if message_count >= 60:  # 60 messages per minute for direct chats
        raise ValueError("Rate limit exceeded. Please slow down.")
    
    cache.set(rate_limit_key, message_count + 1, 60)
    
    return await create_direct_message(sender_obj, receiver_obj, message_text, image_file, reply)





#mute?
# Notification Consumer (for sending real-time notifications for posts, community posts, and events)
class NotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # The user_id in the URL path here should be the Kinde user ID for the recipient
        self.kinde_user_id_from_url = self.scope['url_route']['kwargs']['kinde_user_id']
        
        # Check authentication - user should be authenticated via middleware
        from django.contrib.auth.models import AnonymousUser
        if isinstance(self.scope.get('user'), AnonymousUser):
            await self.close(code=4003)  # Unauthorized
            return
            
        self.kinde_user_id_from_token = self.scope['user'].kinde_user_id  # From authentication

        # Ensure the user connecting is actually the user for whom notifications are being fetched
        if self.kinde_user_id_from_url != self.kinde_user_id_from_token:
            await self.close(code=4003)  # Unauthorized
            return

        self.student = await get_student_from_kinde_id(self.kinde_user_id_from_token)
        if not self.student:
            await self.close(code=4004)  # Student not found
            return

        # Use Kinde user ID for the group name to match how we send notifications
        self.room_group_name = f'notifications_{self.kinde_user_id_from_token}'

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()
        
        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to notifications',
            'kinde_user_id': self.kinde_user_id_from_token
        }))

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    # Method to send notification to this user
    async def send_notification(self, event):
        """Handle notification broadcast from signals"""
        notification_data = event.get('notification_data', {})
        await self.send(text_data=json.dumps({
            'type': 'new_notification',
            **notification_data
        }))
    
    async def receive(self, text_data):
        """Handle messages from client (e.g., mark as read)"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'mark_as_read':
                # Mark all unread notifications as read
                notification_ids = data.get('notification_ids', [])
                await self.mark_notifications_as_read(notification_ids)
            elif message_type == 'mark_all_as_read':
                # Mark all unread notifications for this user as read
                await self.mark_all_notifications_as_read()
            elif message_type == 'ping':
                # Rate limit ping responses to prevent excessive traffic
                # Only respond to ping if at least 5 seconds have passed since last pong
                current_time = asyncio.get_event_loop().time()
                if not hasattr(self, '_last_pong_time'):
                    self._last_pong_time = 0
                
                if current_time - self._last_pong_time >= 5.0:  # 5 second minimum between pongs
                    self._last_pong_time = current_time
                    await self.send(text_data=json.dumps({
                        'type': 'pong',
                        'message': 'Connection alive'
                    }))
                # Silently ignore pings that come too frequently
            else:
                await self.send_error(f"Unknown message type: {message_type}")
                
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON format.")
        except Exception as e:
            logger.error(f"Error in notification receive: {e}")
            await self.send_error("Failed to process message.")
    
    async def mark_notifications_as_read(self, notification_ids):
        """Mark specific notifications as read"""
        try:
            if not notification_ids:
                await self.send_error("No notification IDs provided.")
                return
            
            def _mark_as_read():
                updated_count = Notification.objects.filter(
                    id__in=notification_ids,
                    recipient=self.student,
                    is_read=False
                ).update(is_read=True)
                return updated_count
            
            updated_count = await sync_to_async(_mark_as_read)()
            
            await self.send(text_data=json.dumps({
                'type': 'notifications_marked_read',
                'updated_count': updated_count,
                'notification_ids': notification_ids
            }))
            
        except Exception as e:
            logger.error(f"Error marking notifications as read: {e}")
            await self.send_error("Failed to mark notifications as read.")
    
    async def mark_all_notifications_as_read(self):
        """Mark all unread notifications for this user as read"""
        try:
            # Get notifications that would be returned by the specified endpoints
            def _mark_all_as_read():
                from django.db.models import Q
                
                # Mark all unread notifications for posts, community posts, student events, community events
                updated_count = Notification.objects.filter(
                    recipient=self.student,
                    is_read=False
                ).filter(
                    Q(post__isnull=False) | Q(post_comment__isnull=False) |  # Post notifications
                    Q(community_post__isnull=False) | Q(community_post_comment__isnull=False) |  # Community post notifications
                    Q(student_event__isnull=False) | Q(student_event_discussion__isnull=False) |  # Student event notifications
                    Q(community_event__isnull=False) | Q(community_event_discussion__isnull=False)  # Community event notifications
                ).update(is_read=True)
                
                return updated_count
            
            updated_count = await sync_to_async(_mark_all_as_read)()
            
            await self.send(text_data=json.dumps({
                'type': 'all_notifications_marked_read',
                'updated_count': updated_count
            }))
            
        except Exception as e:
            logger.error(f"Error marking all notifications as read: {e}")
            await self.send_error("Failed to mark all notifications as read.")
    
    async def send_error(self, message):
        """Helper to send error messages"""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message
        }))


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = f'chat_{self.room_name}'

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message = text_data_json['message']

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': message
            }
        )

    async def chat_message(self, event):
        message = event['message']

        await self.send(text_data=json.dumps({
            'message': message
        }))


class PostUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):

        self.group_name = 'post_updates_feed' 

        # Add the channel to the group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        await self.accept()


        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'WebSocket connection to post updates established.',
        }))

    async def disconnect(self, close_code):

        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )


    async def receive(self, text_data):

        pass


    async def post_updated(self, event):

        updated_post_data = event['post_data']
        post_type = event['post_type'] # 'posts' or 'cposts'

        await self.send(text_data=json.dumps({
            'type': 'post_update',
            'post_type': post_type,
            'data': updated_post_data
        }))

# You will need to make your PostSerializer and CommunityPostSerializer
# available to your consumer. If they have complex logic (like get_is_member)
# that requires `kinde_user_id`, you'll need to fetch that within the consumer
# or adjust your serializer to handle `None` gracefully if the `kinde_user_id`
# is not always available in this context.
# For simplicity here, we assume the serializer can work without it or you pass it.

class EventUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.event_id = self.scope['url_route']['kwargs']['event_id']
        self.event_group_name = f'event_updates_{self.event_id}'

        await self.channel_layer.group_add(
            self.event_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.event_group_name,
            self.channel_name
        )

    async def event_updated(self, event):
        """
        Handles the 'event_updated' message from the channel layer.
        This is called when a new discussion is added, or event details are updated.
        """
        event_type = event['event_type'] # 'community_event' or 'student_event'
        event_data = event['event_data']  # Serialized event data, including discussions

        await self.send(text_data=json.dumps({
            'type': 'event_update',
            'event_type': event_type,
            'event_data': event_data,
        }))


class FeedUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # The single, general group for all feed updates
        self.group_name = 'global_feed_updates' 

        # Add the channel to the group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        # Remove the channel from the group
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        # This consumer only receives messages from the Channel Layer, not the client.
        # So, the `receive` method can be empty or log a warning.
        pass

    async def feed_update(self, event):
        """
        Receives messages from the Channel Layer and sends them to the client.
        """
        # Send the event data to the WebSocket
        await self.send(text_data=json.dumps(event['data']))


class SinglePostUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.post_id = self.scope['url_route']['kwargs']['post_id']
        self.group_name = f'post_updates_{self.post_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def post_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'post_update',
            'data': event['post_data']
        }))

class SingleEventUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # The event_id will come from the URL route (e.g., /ws/events/123/updates/)
        self.event_id = self.scope['url_route']['kwargs']['event_id']
        self.group_name = f'event_updates_{self.event_id}' # Group name for this specific event

        # Add the current channel to this event's group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        # Remove the channel from the group when the client disconnects
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def event_updated(self, event):
        """
        Receives an 'event_updated' message from the Channel Layer
        and sends it to the connected client.
        """
        # The 'event' dict will contain the serialized event data
        # (including updated RSVP counts, discussions, etc.)
        await self.send(text_data=json.dumps({
            'type': 'event_update',      # Type for Flutter to recognize
            'data': event['event_data']  # The actual updated event data
        }))


class UserUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # 1. Get the user's Kinde ID from the URL.
        # This assumes your routing.py defines the URL with <kinde_user_id>.
        self.kinde_user_id = self.scope['url_route']['kwargs']['kinde_user_id']
        
        # 2. Define the group name for this specific user.
        # This makes the group unique to each user.
        self.group_name = f'user_updates_{self.kinde_user_id}'

        # 3. Add the current WebSocket channel to this user's specific group.
        # This means any message sent to this group will be pushed to this client.
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        
        # 4. Accept the WebSocket connection.
        await self.accept()

        # Optional: Send a confirmation message upon connection
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Personal update channel established.',
        }))

    async def disconnect(self, close_code):
        # 1. Remove the channel from the user's group when the client disconnects.
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        # This consumer is designed to *receive* messages from the Django Channel Layer (server-to-client).
        # It's generally not expected to receive messages *from* the client for personal updates.
        # Therefore, this method is often left empty or logs a warning if client sends data.
        pass

    async def user_updated(self, event):
        """
        Handler for messages sent to this consumer's group by the Channel Layer.
        
        This method's name (`user_updated`) corresponds to the `type` field
        in the message dictionary sent by `channel_layer.group_send`.
        Example: `channel_layer.group_send(group_name, {'type': 'user.updated', 'data': {...}})`
        """
        # The 'event' dictionary contains the data pushed by the backend view.
        # It should contain a 'data' key with the actual update payload.
        
        # Send the received data directly to the connected Flutter client.
        await self.send(text_data=json.dumps(event['data']))


class SingleCommunityPostUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.community_post_id = self.scope['url_route']['kwargs']['community_post_id']
        self.group_name = f'community_post_updates_{self.community_post_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def community_post_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'community_post_update',
            'data': event['post_data']
        }))


class SingleCommunityEventUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.community_event_id = self.scope['url_route']['kwargs']['community_event_id']
        self.group_name = f'community_event_updates_{self.community_event_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def community_event_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'community_event_update',
            'data': event['event_data']
        }))


class StudentPostsUpdateConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time updates to student posts list"""
    async def connect(self):
        self.student_id = self.scope['url_route']['kwargs']['student_id']
        self.group_name = f'student_posts_updates_{self.student_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def post_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'student_post_update',
            'data': event['post_data']
        }))


class CommunityPostsListUpdateConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time updates to community posts list"""
    async def connect(self):
        self.community_id = self.scope['url_route']['kwargs']['community_id']
        self.group_name = f'community_posts_list_updates_{self.community_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def community_post_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'community_posts_list_update',
            'data': event['post_data']
        }))


class StudentEventsListUpdateConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time updates to student events list"""
    async def connect(self):
        self.student_id = self.scope['url_route']['kwargs']['student_id']
        self.group_name = f'student_events_list_updates_{self.student_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def event_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'student_events_list_update',
            'data': event['event_data']
        }))


class CommunityEventsListUpdateConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time updates to community events list"""
    async def connect(self):
        self.community_id = self.scope['url_route']['kwargs']['community_id']
        self.group_name = f'community_events_list_updates_{self.community_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def community_event_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'community_events_list_update',
            'data': event['event_data']
        }))


class PostFeedUpdateConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time updates to post feed"""
    async def connect(self):
        self.kinde_user_id = self.scope['url_route']['kwargs']['kinde_user_id']
        self.group_name = f'post_feed_updates_{self.kinde_user_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def post_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'post_feed_update',
            'data': event['post_data']
        }))

    async def community_post_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'post_feed_update',
            'data': event['post_data']
        }))


class EventsFeedUpdateConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time updates to events feed"""
    async def connect(self):
        self.kinde_user_id = self.scope['url_route']['kwargs']['kinde_user_id']
        self.group_name = f'events_feed_updates_{self.kinde_user_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def event_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'events_feed_update',
            'data': event['event_data']
        }))

    async def community_event_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'events_feed_update',
            'data': event['event_data']
        }))

class CommunityChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        """Optimized connection handling with better error management and caching"""
        try:
            # Extract parameters
            self.community_id = self.scope['url_route']['kwargs']['community_id']
            
            # Check authentication early
            from django.contrib.auth.models import AnonymousUser
            if isinstance(self.scope['user'], AnonymousUser):
                await self.close(code=4003)
                return
            
            self.student = self.scope['user']
            
            # Use your helper function and verify membership concurrently
            try:
                community, is_member = await asyncio.gather(
                    sync_to_async(Communities.objects.get)(pk=self.community_id),
                    sync_to_async(Membership.objects.filter(
                        user=self.student, 
                        community_id=self.community_id
                    ).exists)(),
                    return_exceptions=True
                )
                
                if isinstance(community, Exception):
                    await self.close(code=4004)  # Community not found
                    return
                    
                if isinstance(is_member, Exception) or not is_member:
                    await self.close(code=4005)  # Not a member
                    return
                    
                self.community = community
                
            except Exception as e:
                logger.error(f"Database error in community chat connect: {e}")
                await self.close(code=4000)
                return

            # Set up group
            self.room_group_name = f'community_chat_{self.community_id}'
            
            # Join group and accept connection concurrently
            await asyncio.gather(
                self.channel_layer.group_add(self.room_group_name, self.channel_name),
                self.accept()
            )
            
            # Mark all unread messages as read when connecting
            await self.mark_all_messages_as_read()
            
            # Send recent messages using your helper function
            recent_messages_data = await get_recent_community_messages(self.community, kinde_user_id=self.student.kinde_user_id)
            await self.send(text_data=json.dumps({
                'type': 'chat_history',
                'messages': recent_messages_data
            }))
            
        except Exception as e:
            logger.error(f"Unexpected error in community chat connect: {e}")
            await self.close(code=4000)

    async def check_community_blocking_status(self):
        """Check if user is blocked by this community. Cached to avoid per-message DB hits."""
        cache_key = f"community_block_{self.community_id}_{self.student.id}"
        try:
            cached = await sync_to_async(cache.get)(cache_key)
            if cached is not None:
                return cached
            is_blocked = await BlockedByCommunities.objects.filter(
                community=self.community,
                blocked_student=self.student
            ).aexists()
            await sync_to_async(cache.set)(cache_key, is_blocked, 300)
            return is_blocked
        except Exception as e:
            logger.error(f"Error checking community blocking status: {e}")
            return False  # Allow connection if check fails
    
    async def mark_all_messages_as_read(self):
        """Mark all unread messages in this community as read for the current user. Bulk: 1 SELECT + 1 bulk INSERT."""
        try:
            def _mark_messages_read():
                unread_ids = list(
                    CommunityChatMessage.objects.filter(community=self.community)
                    .exclude(read_by=self.student)
                    .values_list('id', flat=True)
                )
                if not unread_ids:
                    return 0
                Through = CommunityChatMessage.read_by.through
                Through.objects.bulk_create(
                    [
                        Through(communitychatmessage_id=mid, student_id=self.student.id)
                        for mid in unread_ids
                    ],
                    ignore_conflicts=True,
                )
                return len(unread_ids)

            count = await sync_to_async(_mark_messages_read)()
            if count > 0:
                logger.info(f"Marked {count} community message(s) as read for {self.student.name} in {self.community.community_name}")
        except Exception as e:
            logger.error(f"Error marking community messages as read: {e}")

    async def disconnect(self, close_code):
        """Clean disconnection"""
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        """Optimized message handling using your helper functions"""
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON format.")
            return
            
        message_text = data.get('message', '').strip()
        image_id = data.get('image_id')
        parent_message_id = data.get('parent_message_id')
        post_id = data.get('post_id')  # Optional, if messages can be linked to posts
        student_event_id = data.get('student_event_id')  # Optional, if messages can be linked to events
        community_event_id = data.get('community_event_id')  # Optional, if messages can be linked to events
        community_post_id = data.get('community_post_id')  # Optional, if messages can be linked to community posts
        student_profile_id = data.get('student_profile_id')
        community_profile_id = data.get('community_profile_id')


        # Validate input
        if not message_text and not image_id:
            await self.send_error("Message cannot be empty.")
            return
            
        if message_text and len(message_text) > 1000:  # Add reasonable limit
            await self.send_error("Message too long.")
            return
        
        is_blocked_by_community = await self.check_community_blocking_status()
        if is_blocked_by_community:
            await self.send_error("You are not allowed to send messages in this community.")
            return

        try:
            post_obj = None
            if post_id:
                try:
                    post_obj = await Posts.objects.select_related('student').prefetch_related(
                        'images',
                        'student_mentions',
                        'community_mentions'
                    ).aget(pk=post_id)
                except Posts.DoesNotExist:
                    await self.send_error("Linked post not found.")
                    return

            student_event_obj = None
            if student_event_id:
                try:
                    student_event_obj = await Student_Events.objects.select_related('student').aget(pk=student_event_id)
                except Student_Events.DoesNotExist:
                    await self.send_error("Linked student event not found.")
                    return

            community_event_obj = None
            if community_event_id:
                try:
                    community_event_obj = await Community_Events.objects.select_related(
                        'community', 'poster'
                    ).aget(pk=community_event_id)
                except Community_Events.DoesNotExist:
                    await self.send_error("Linked community event not found.")
                    return

            community_post_obj = None
            if community_post_id:
                try:
                    community_post_obj = await Community_Posts.objects.select_related(
                        'poster', 'community'
                    ).prefetch_related('images').aget(pk=community_post_id)
                except Community_Posts.DoesNotExist:
                    await self.send_error("Linked community post not found.")
                    return

            student_profile_obj = None
            if student_profile_id:
                try:
                    student_profile_obj = await Student.objects.select_related('university').aget(pk=student_profile_id)
                except Student.DoesNotExist:
                    await self.send_error("Student profile not found.")
                    return

            community_profile_obj = None
            if community_profile_id:
                try:
                    community_profile_obj = await Communities.objects.select_related('location').aget(pk=community_profile_id)
                except Communities.DoesNotExist:
                    await self.send_error("Community profile not found")
                    return

            parent_message_obj = None
            if parent_message_id:
                try:
                    parent_message_obj = await CommunityChatMessage.objects.select_related(
                        'student', 'reply__student'
                    ).aget(pk=parent_message_id, community=self.community)
                except CommunityChatMessage.DoesNotExist:
                    await self.send_error("Reply message not found.")
                    return

            if image_id:
                try:
                    message_obj = await CommunityChatMessage.objects.select_related(
                        'student', 'reply__student'
                    ).aget(pk=image_id, student=self.student, community=self.community)

                    if message_text:
                        message_obj.message = message_text
                        await sync_to_async(message_obj.save)(update_fields=['message'])

                except CommunityChatMessage.DoesNotExist:
                    await self.send_error("Image message not found.")
                    return
            else:
                message_obj = await create_community_message(
                    self.community,
                    self.student,
                    message_text=message_text,
                    reply=parent_message_obj,
                    post=post_obj,
                    student_event=student_event_obj,
                    community_event=community_event_obj,
                    community_post=community_post_obj,
                    student_profile=student_profile_obj,
                    community_profile=community_profile_obj
                )

            message_obj = await CommunityChatMessage.objects.select_related(
                'student',
                'community',
                'reply__student',
                'post__student',
                'community_post__poster',
                'community_post__community',
                'student_event__student',
                'community_event__community',
                'community_event__poster',
                'student_profile',
                'community_profile'
            ).prefetch_related(
                'post__images',
                'post__student_mentions',
                'post__community_mentions',
                'community_post__images'
            ).aget(pk=message_obj.pk)

            # Attach preloaded optional relations for payload building
            if post_obj:
                message_obj.post = post_obj
            if community_post_obj:
                message_obj.community_post = community_post_obj
            if student_event_obj:
                message_obj.student_event = student_event_obj
            if community_event_obj:
                message_obj.community_event = community_event_obj
            if student_profile_obj:
                message_obj.student_profile = student_profile_obj
            if community_profile_obj:
                message_obj.community_profile = community_profile_obj

            await self.broadcast_message(message_obj)
            
            # Invalidate cache for recent messages
            cache_key = f"community_recent_messages_{self.community_id}"
            cache.delete(cache_key)
            
        except Exception as e:
            logger.error(f"Error in community chat receive: {e}")
            await self.send_error("Failed to process message.")

    async def broadcast_message(self, message_obj):
        """Optimized message broadcasting"""
        serialized_message = await asyncio.to_thread(
            self._build_community_message_payload,
            message_obj
        )
        
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': serialized_message,
                'sender_kinde_id': self.student.kinde_user_id,
                'message_id': message_obj.id
            }
        )

    def _build_community_message_payload(self, message_obj):
        """Build payload with nested objects for community chat without extra DB queries."""
        context = {'request': None, 'kinde_user_id': self.student.kinde_user_id}

        payload = {
            'id': message_obj.id,
            'community': message_obj.community_id,
            'community_name': getattr(message_obj.community, 'community_name', None),
            'student': StudentChatSerializer(message_obj.student, context=context).data if message_obj.student else None,
            'message': message_obj.message,
            'image_url': message_obj.image.url if message_obj.image else None,
            'sent_at': message_obj.sent_at.isoformat() if message_obj.sent_at else None,
            'is_read_by_me': message_obj.read_by.filter(id=self.student.id).exists()
        }

        if getattr(message_obj, 'reply', None):
            payload['reply'] = CommunityChatReplySerializer(message_obj.reply, context=context).data

        if getattr(message_obj, 'post', None):
            payload['post'] = PostNameSerializer(message_obj.post, context=context).data

        if getattr(message_obj, 'community_post', None):
            payload['community_post'] = CommunityPostNameSerializer(message_obj.community_post, context=context).data

        if getattr(message_obj, 'student_event', None):
            payload['student_event'] = StudentEventNameSerializer(message_obj.student_event, context=context).data

        if getattr(message_obj, 'community_event', None):
            payload['community_event'] = CommunityEventsNameSerializer(message_obj.community_event, context=context).data

        if getattr(message_obj, 'student_profile', None):
            payload['student_profile'] = StudentNameSerializer(message_obj.student_profile, context=context).data

        if getattr(message_obj, 'community_profile', None):
            payload['community_profile'] = CommunityNameSerializer(message_obj.community_profile, context=context).data

        return payload

    async def send_error(self, message):
        """Helper to send error messages"""
        await self.send(text_data=json.dumps({"error": message}))

    async def chat_message(self, event):
        """Handle broadcasted messages. Use event payload; only mark as read in DB, no per-recipient re-fetch."""
        message = event.get('message') or {}
        message_id = event.get('message_id')
        sender_kinde_id = event.get('sender_kinde_id')
        is_me = (self.student.kinde_user_id == sender_kinde_id)

        # If not from me and I'm connected, mark as read in DB (one lightweight write)
        if not is_me and message_id:
            try:
                def _mark_as_read():
                    try:
                        msg = CommunityChatMessage.objects.get(id=message_id, community=self.community)
                        if not msg.read_by.filter(id=self.student.id).exists():
                            msg.read_by.add(self.student)
                            return True
                    except CommunityChatMessage.DoesNotExist:
                        pass
                    return False

                marked = await sync_to_async(_mark_as_read)()
                if marked:
                    logger.info(f"Marked community message {message_id} as read by {self.student.name}")
            except Exception as e:
                logger.error(f"Error marking community message as read: {e}")

        # Use payload from sender; only patch is_read_by_me (no per-recipient DB re-fetch)
        message = dict(message)
        message['is_read_by_me'] = True  # sender: already true; recipient: they're connected so reading it
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'message': message,
            'is_me': is_me
        }))


class GroupChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for standalone group chats.
    Very similar to CommunityChatConsumer but uses GroupChat / GroupChatMessage
    and GroupChatMembership for access control.
    """

    async def connect(self):
        try:
            self.group_id = self.scope['url_route']['kwargs']['group_id']

            from django.contrib.auth.models import AnonymousUser
            if isinstance(self.scope['user'], AnonymousUser):
                await self.close(code=4003)
                return

            self.student = self.scope['user']

            # Fetch group and verify membership
            from Users.models import GroupChat, GroupChatMembership  # Deferred import
            try:
                group, is_member = await asyncio.gather(
                    sync_to_async(GroupChat.objects.get)(pk=self.group_id, is_active=True),
                    sync_to_async(GroupChatMembership.objects.filter(
                        group_id=self.group_id,
                        member=self.student,
                    ).exists)(),
                    return_exceptions=True,
                )
                if isinstance(group, Exception):
                    await self.close(code=4004)  # Group not found
                    return
                if isinstance(is_member, Exception) or not is_member:
                    await self.close(code=4005)  # Not a member
                    return
                self.group = group
            except Exception as e:
                logger.error(f"Database error in group chat connect: {e}")
                await self.close(code=4000)
                return

            self.room_group_name = f'group_chat_{self.group_id}'

            await asyncio.gather(
                self.channel_layer.group_add(self.room_group_name, self.channel_name),
                self.accept()
            )

            # Mark existing messages as read and send recent history
            await self.mark_all_messages_as_read()
            recent = await self.get_recent_group_messages()
            await self.send(text_data=json.dumps({
                'type': 'chat_history',
                'messages': recent,
            }))

        except Exception as e:
            logger.error(f"Unexpected error in group chat connect: {e}")
            await self.close(code=4000)

    async def get_recent_group_messages(self, count=50):
        """Fetch recent group chat messages for the current group and user."""
        return await get_recent_group_messages(
            self.group,
            count=count,
            kinde_user_id=getattr(self.student, 'kinde_user_id', None),
        )

    async def mark_all_messages_as_read(self):
        """Mark all unread messages in this group as read for the current user. Bulk: 1 SELECT + 1 bulk INSERT."""
        from Users.models import GroupChatMessage  # Deferred import

        try:
            def _mark_messages_read():
                unread_ids = list(
                    GroupChatMessage.objects.filter(group=self.group)
                    .exclude(read_by=self.student)
                    .values_list('id', flat=True)
                )
                if not unread_ids:
                    return 0
                Through = GroupChatMessage.read_by.through
                Through.objects.bulk_create(
                    [
                        Through(groupchatmessage_id=mid, student_id=self.student.id)
                        for mid in unread_ids
                    ],
                    ignore_conflicts=True,
                )
                return len(unread_ids)

            count = await sync_to_async(_mark_messages_read)()
            if count > 0:
                logger.info(f"Marked {count} group message(s) as read for {self.student.name} in {self.group.name}")
        except Exception as e:
            logger.error(f"Error marking group messages as read: {e}")

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        """Handle incoming group chat messages."""
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON format.")
            return

        message_text = data.get('message', '').strip()
        image_id = data.get('image_id')
        parent_message_id = data.get('parent_message_id')
        post_id = data.get('post_id')
        community_post_id = data.get('community_post_id')
        student_event_id = data.get('student_event_id')
        community_event_id = data.get('community_event_id')
        student_profile_id = data.get('student_profile_id')
        community_profile_id = data.get('community_profile_id')

        if not message_text and not image_id:
            await self.send_error("Message cannot be empty.")
            return
        if message_text and len(message_text) > 1000:
            await self.send_error("Message too long.")
            return

        from Users.models import (
            GroupChatMessage,
            Posts,
            Community_Posts,
            Student_Events,
            Community_Events,
            Student,
            Communities,
        )

        try:
            # Fetch linked objects in parallel (like DirectChatConsumer)
            async def _get_post():
                return await Posts.objects.select_related('student').prefetch_related(
                    'images', 'student_mentions', 'community_mentions'
                ).aget(pk=post_id) if post_id else None

            async def _get_student_event():
                return await Student_Events.objects.select_related('student').aget(pk=student_event_id) if student_event_id else None

            async def _get_community_event():
                return await Community_Events.objects.select_related('community', 'poster').aget(pk=community_event_id) if community_event_id else None

            async def _get_community_post():
                return await Community_Posts.objects.select_related('poster', 'community').prefetch_related('images').aget(pk=community_post_id) if community_post_id else None

            async def _get_student_profile():
                return await Student.objects.select_related('university').aget(pk=student_profile_id) if student_profile_id else None

            async def _get_community_profile():
                return await Communities.objects.select_related('location').aget(pk=community_profile_id) if community_profile_id else None

            async def _get_parent_message():
                return await GroupChatMessage.objects.select_related('student', 'reply__student').aget(pk=parent_message_id, group=self.group) if parent_message_id else None

            (
                post_obj,
                student_event_obj,
                community_event_obj,
                community_post_obj,
                student_profile_obj,
                community_profile_obj,
                parent_message_obj,
            ) = await asyncio.gather(
                _get_post(),
                _get_student_event(),
                _get_community_event(),
                _get_community_post(),
                _get_student_profile(),
                _get_community_profile(),
                _get_parent_message(),
                return_exceptions=True,
            )
            for obj in (post_obj, student_event_obj, community_event_obj, community_post_obj,
                       student_profile_obj, community_profile_obj, parent_message_obj):
                if isinstance(obj, Exception):
                    raise obj

            if image_id:
                # Editing an existing image message
                message_obj = await GroupChatMessage.objects.select_related(
                    'student', 'reply__student'
                ).aget(pk=image_id, student=self.student, group=self.group)
                if message_text:
                    message_obj.message = message_text
                    await sync_to_async(message_obj.save)(update_fields=['message'])
            else:
                # Create new message
                message_obj = await GroupChatMessage.objects.acreate(
                    group=self.group,
                    student=self.student,
                    message=message_text,
                    reply=parent_message_obj,
                    post=post_obj,
                    community_post=community_post_obj,
                    student_event=student_event_obj,
                    community_event=community_event_obj,
                    student_profile=student_profile_obj,
                    community_profile=community_profile_obj,
                )

            # Reload with relations for payload
            message_obj = await GroupChatMessage.objects.select_related(
                'student',
                'group',
                'reply__student',
                'post__student',
                'community_post__poster',
                'community_post__community',
                'student_event__student',
                'community_event__community',
                'community_event__poster',
                'student_profile',
                'community_profile',
            ).prefetch_related(
                'post__images',
                'post__student_mentions',
                'post__community_mentions',
                'community_post__images',
            ).aget(pk=message_obj.pk)

            await self.broadcast_message(message_obj)

        except Exception as e:
            logger.error(f"Error in group chat receive: {e}")
            await self.send_error("Failed to process message.")

    async def broadcast_message(self, message_obj):
        from Users.serializers import GroupChatMessageSerializer, StudentChatSerializer

        context = {'request': None, 'kinde_user_id': self.student.kinde_user_id}
        payload = GroupChatMessageSerializer(message_obj, context=context).data

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'group_chat_message',
                'message': payload,
                'sender_kinde_id': self.student.kinde_user_id,
                'message_id': message_obj.id,
            },
        )

    async def send_error(self, message):
        await self.send(text_data=json.dumps({"error": message}))

    async def group_chat_message(self, event):
        """Handle broadcasted group messages and mark as read when appropriate."""
        from Users.models import GroupChatMessage as GroupChatMessageModel

        message_id = event.get('message_id')
        sender_kinde_id = event.get('sender_kinde_id')
        is_me = (self.student.kinde_user_id == sender_kinde_id)

        # If this message is not from me and I'm connected, mark it as read
        if not is_me and message_id:
            try:
                def _mark_as_read():
                    try:
                        msg = GroupChatMessageModel.objects.get(id=message_id, group=self.group)
                        if not msg.read_by.filter(id=self.student.id).exists():
                            msg.read_by.add(self.student)
                            return True
                    except GroupChatMessageModel.DoesNotExist:
                        return False
                    return False

                await sync_to_async(_mark_as_read)()
            except Exception as e:
                logger.error(f"Error marking group message as read: {e}")

        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'message': event['message'],
            'is_me': is_me,
        }))


#block
class DirectChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        """Optimized direct chat connection using helper functions"""
        try:
            self.other_user_kinde_id = self.scope['url_route']['kwargs']['user_id']

            # Check authentication
            from django.contrib.auth.models import AnonymousUser
            if isinstance(self.scope['user'], AnonymousUser):
                await self.close(code=4003)
                return

            self.me = self.scope['user']

            # Use your helper function to get the other user
            self.other_user = await get_student_from_kinde_id(self.other_user_kinde_id)
            if not self.other_user:
                await self.close(code=4004)
                return

            # Prevent chat with self
            if self.me.pk == self.other_user.pk:
                await self.close(code=4005)
                return
            
            is_blocked = await self.check_blocking_status()
            if is_blocked:
                await self.close(code=4006)
                return

            # Create consistent room name
            user_pks = sorted([str(self.me.pk), str(self.other_user.pk)])
            self.room_group_name = f'direct_chat_{user_pks[0]}_{user_pks[1]}'

            # Accept connection first for faster perceived performance
            await self.accept()
            
            # Join group in parallel with marking messages as read
            await asyncio.gather(
                self.channel_layer.group_add(self.room_group_name, self.channel_name),
                self.mark_last_message_as_read()
            )
            
            # Send connection confirmation
            await self.send(text_data=json.dumps({
                'type': 'connection_established',
                'message': f'Connected to direct chat with {self.other_user.name}.'
            }))
            
        except Exception as e:
            logger.error(f"Error in direct chat connect: {e}")
            await self.close(code=4000)


    async def check_blocking_status(self):
        """Check if either user has blocked the other - cached snapshot."""
        from .cache_utils import build_pair_block_cache_key, get_relationship_snapshot

        cache_key = build_pair_block_cache_key(self.me.pk, self.other_user.pk)
        cached = await sync_to_async(cache.get)(cache_key)
        if cached is not None:
            return cached

        try:
            me_snapshot = await sync_to_async(get_relationship_snapshot)(self.me.pk)
            other_snapshot = await sync_to_async(get_relationship_snapshot)(self.other_user.pk)

            block_exists = (
                self.other_user.pk in set(me_snapshot.get('blocking', [])) or
                self.me.pk in set(other_snapshot.get('blocking', []))
            )

            await sync_to_async(cache.set)(cache_key, block_exists, 300)
            return block_exists
        except Exception as exc:
            logger.error(f"Error checking blocking status: {exc}")
            return False
    
    async def mark_last_message_as_read(self):
        """Mark all unread messages from the other user as read - OPTIMIZED. Notifies sender via message_read_event."""
        try:
            def _mark_messages_read():
                qs = DirectMessage.objects.filter(
                    sender=self.other_user,
                    receiver=self.me,
                    is_read=False
                )
                marked_ids = list(qs.values_list('id', flat=True))
                if not marked_ids:
                    return [], None
                now = django_timezone.now()
                qs.update(is_read=True, read_at=now)
                return marked_ids, now
            
            marked_ids, read_at = await sync_to_async(_mark_messages_read)()
            
            if marked_ids and read_at:
                read_at_iso = read_at.isoformat()
                if read_at_iso.endswith('+00:00'):
                    read_at_iso = read_at_iso[:-6] + 'Z'
                asyncio.create_task(self._send_read_notification(marked_ids, read_at_iso))
                
        except Exception as e:
            logger.error(f"Error marking messages as read: {e}")

    async def _send_read_notification(self, message_ids, read_at_iso):
        """Send message_read to direct chat room so sender sees blue checkmarks."""
        try:
            await self.channel_layer.group_send(self.room_group_name, {
                'type': 'message_read_event',
                'message_ids': message_ids,
                'read_at': read_at_iso,
                'reader_kinde_id': self.me.kinde_user_id,
            })
        except Exception as e:
            logger.error(f"Error sending read notification: {e}")

    async def disconnect(self, close_code):
        """Clean disconnection"""
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        """Optimized direct message handling - BATCH QUERIES. Handles chat_message, mark_as_read, typing, stop_typing."""
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON format.")
            return

        msg_type = data.get('type')

        # --- mark_as_read: user opened chat and marks specific messages as read ---
        if msg_type == 'mark_as_read':
            message_ids = data.get('message_ids') or []
            receiver_kinde_id = data.get('receiver_kinde_id')
            if receiver_kinde_id != self.other_user.kinde_user_id:
                await self.send_error("receiver_kinde_id does not match this conversation.")
                return
            if not message_ids:
                return
            now = django_timezone.now()

            def _mark():
                valid = list(
                    DirectMessage.objects.filter(
                        id__in=message_ids,
                        sender=self.other_user,
                        receiver=self.me,
                    ).values_list('id', flat=True)
                )
                DirectMessage.objects.filter(id__in=valid, is_read=False).update(is_read=True, read_at=now)
                return valid

            marked_ids = await sync_to_async(_mark)()
            if not marked_ids:
                return
            read_at_iso = now.isoformat()
            if read_at_iso.endswith('+00:00'):
                read_at_iso = read_at_iso[:-6] + 'Z'
            await self.channel_layer.group_send(self.room_group_name, {
                'type': 'message_read_event',
                'message_ids': list(marked_ids),
                'read_at': read_at_iso,
                'reader_kinde_id': self.me.kinde_user_id,
            })
            return

        # --- typing / stop_typing: forward to other user in this conversation ---
        if msg_type == 'typing':
            await self.channel_layer.group_send(self.room_group_name, {
                'type': 'typing_indicator',
                'sender_kinde_id': self.me.kinde_user_id,
            })
            return
        if msg_type == 'stop_typing':
            await self.channel_layer.group_send(self.room_group_name, {
                'type': 'stop_typing_indicator',
                'sender_kinde_id': self.me.kinde_user_id,
            })
            return

        # --- chat_message: send a new message (type may be 'chat_message' or omitted for backward compat) ---
        message_text = data.get('message', '').strip()
        image_id = data.get('image_id')
        parent_message_id = data.get('parent_message_id')
        
        # Extract all IDs
        post_id = data.get('post_id')
        community_post_id = data.get('community_post_id')
        student_event_id = data.get('student_event_id')
        community_event_id = data.get('community_event_id')
        student_profile_id = data.get('student_profile_id')
        community_profile_id = data.get('community_profile_id')

        # Validate input
        if not message_text and not image_id:
            await self.send_error("Message cannot be empty.")
            return
            
        if message_text and len(message_text) > 1000:
            await self.send_error("Message too long.")
            return
        
        is_blocked = await self.check_blocking_status()
        if is_blocked:
            await self.send_error("Cannot send message. User relationship has changed.")
            return

        try:
            # OPTIMIZATION: Fetch all related objects in parallel
            related_objects = await self._fetch_related_objects_parallel(
                post_id, community_post_id, student_event_id, 
                community_event_id, student_profile_id, 
                community_profile_id, parent_message_id
            )
            
            if related_objects.get('error'):
                await self.send_error(related_objects['error'])
                return

            # Create or update message
            if image_id:
                try:
                    message_obj = await DirectMessage.objects.select_related(
                        'sender', 'receiver', 'reply__sender', 'reply__receiver'
                    ).aget(pk=image_id, sender=self.me)

                    if message_text:
                        message_obj.message = message_text
                        await sync_to_async(message_obj.save)(update_fields=['message'])

                except DirectMessage.DoesNotExist:
                    await self.send_error("Image message not found.")
                    return
            else:
                try:
                    message_obj = await create_direct_message(
                        self.me,
                        self.other_user,
                        message_text=message_text,
                        **related_objects
                    )
                except ValueError as e:
                    await self.send_error(str(e))
                    return

            # Attach related objects to message_obj for serialization
            for key, value in related_objects.items():
                if value and key != 'error':
                    setattr(message_obj, key.replace('_obj', ''), value)

            # Broadcast and mark as read in parallel
            await asyncio.gather(
                self.broadcast_message(message_obj),
                self.check_and_mark_message_as_read(message_obj),
                return_exceptions=True
            )
            
        except Exception as e:
            logger.error(f"Error in direct chat receive: {e}")
            await self.send_error("Failed to process message.")

    async def _fetch_related_objects_parallel(self, post_id, community_post_id, 
                                             student_event_id, community_event_id,
                                             student_profile_id, community_profile_id,
                                             parent_message_id):
        """Fetch all related objects in parallel - MAJOR OPTIMIZATION"""
        
        async def fetch_post():
            if not post_id:
                return None
            try:
                return await Posts.objects.select_related('student').prefetch_related(
                    'images', 'student_mentions', 'community_mentions'
                ).aget(pk=post_id)
            except Posts.DoesNotExist:
                return {'error': 'post'}
        
        async def fetch_community_post():
            if not community_post_id:
                return None
            try:
                return await Community_Posts.objects.select_related(
                    'poster', 'community'
                ).prefetch_related('images').aget(pk=community_post_id)
            except Community_Posts.DoesNotExist:
                return {'error': 'community_post'}
        
        async def fetch_student_event():
            if not student_event_id:
                return None
            try:
                return await Student_Events.objects.select_related('student').aget(pk=student_event_id)
            except Student_Events.DoesNotExist:
                return {'error': 'student_event'}
        
        async def fetch_community_event():
            if not community_event_id:
                return None
            try:
                return await Community_Events.objects.select_related(
                    'community', 'poster'
                ).aget(pk=community_event_id)
            except Community_Events.DoesNotExist:
                return {'error': 'community_event'}
        
        async def fetch_student_profile():
            if not student_profile_id:
                return None
            try:
                return await Student.objects.select_related('university').aget(pk=student_profile_id)
            except Student.DoesNotExist:
                return {'error': 'student_profile'}
        
        async def fetch_community_profile():
            if not community_profile_id:
                return None
            try:
                return await Communities.objects.select_related('location').aget(pk=community_profile_id)
            except Communities.DoesNotExist:
                return {'error': 'community_profile'}
        
        async def fetch_parent_message():
            if not parent_message_id:
                return None
            try:
                return await DirectMessage.objects.select_related(
                    'sender', 'receiver'
                ).aget(pk=parent_message_id)
            except DirectMessage.DoesNotExist:
                return {'error': 'parent_message'}
        
        # Fetch all in parallel
        results = await asyncio.gather(
            fetch_post(),
            fetch_community_post(),
            fetch_student_event(),
            fetch_community_event(),
            fetch_student_profile(),
            fetch_community_profile(),
            fetch_parent_message(),
            return_exceptions=True
        )
        
        # Check for errors
        error_map = {
            'post': 'Associated post not found.',
            'community_post': 'Associated community post not found.',
            'student_event': 'Associated student event not found.',
            'community_event': 'Associated community event not found.',
            'student_profile': 'Student profile not found.',
            'community_profile': 'Community profile not found.',
            'parent_message': 'Reply message not found.'
        }
        
        for result in results:
            if isinstance(result, dict) and result.get('error'):
                return {'error': error_map[result['error']]}
        
        return {
            'post': results[0],
            'community_post': results[1],
            'student_event': results[2],
            'community_event': results[3],
            'student_profile': results[4],
            'community_profile': results[5],
            'reply': results[6]
        }

    async def broadcast_message(self, message_obj):
        """Optimized message broadcasting with caching"""
        # Build payload in thread pool to avoid blocking
        serialized_message = await asyncio.to_thread(
            self._build_direct_message_payload,
            message_obj
        )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': serialized_message,
                'sender_kinde_id': self.me.kinde_user_id,
                'message_id': message_obj.id
            }
        )

    def _build_direct_message_payload(self, message_obj):
        """Build payload - keep existing logic"""
        context = {'request': None, 'kinde_user_id': getattr(self, 'kinde_user_id', None)}

        sender_data = StudentChatSerializer(message_obj.sender, context=context).data if message_obj.sender else None
        receiver_data = StudentChatSerializer(message_obj.receiver, context=context).data if message_obj.receiver else None

        payload = {
            'id': message_obj.id,
            'sender': sender_data,
            'receiver': receiver_data,
            'message': message_obj.message,
            'image_url': message_obj.image.url if message_obj.image else None,
            'timestamp': message_obj.timestamp.isoformat() if message_obj.timestamp else None,
            'is_read': message_obj.is_read,
        }
        if getattr(message_obj, 'read_at', None):
            payload['read_at'] = message_obj.read_at.isoformat()
        if getattr(message_obj, 'delivered_at', None):
            payload['delivered_at'] = message_obj.delivered_at.isoformat()
        payload['status'] = 'read' if (message_obj.is_read and getattr(message_obj, 'read_at', None)) else ('delivered' if getattr(message_obj, 'delivered_at', None) else 'sent')

        if getattr(message_obj, 'reply', None):
            payload['reply'] = DirectMessageParentSerializer(message_obj.reply, context=context).data

        if getattr(message_obj, 'post', None):
            payload['post'] = PostNameSerializer(message_obj.post, context=context).data

        if getattr(message_obj, 'community_post', None):
            payload['community_post'] = CommunityPostNameSerializer(message_obj.community_post, context=context).data

        if getattr(message_obj, 'student_event', None):
            payload['student_event'] = StudentEventNameSerializer(message_obj.student_event, context=context).data

        if getattr(message_obj, 'community_event', None):
            payload['community_event'] = CommunityEventsNameSerializer(message_obj.community_event, context=context).data

        if getattr(message_obj, 'student_profile', None):
            payload['student_profile'] = StudentNameSerializer(message_obj.student_profile, context=context).data

        if getattr(message_obj, 'community_profile', None):
            payload['community_profile'] = CommunityNameSerializer(message_obj.community_profile, context=context).data

        return payload
    
    async def check_and_mark_message_as_read(self, message_obj):
        """Lightweight check - actual marking happens in chat_message"""
        pass

    async def send_error(self, message):
        """Helper to send error messages"""
        await self.send(text_data=json.dumps({"error": message}))

    async def chat_message(self, event):
        """Handle broadcasted messages - OPTIMIZED. Optionally mark delivered and notify sender."""
        message_id = event.get('message_id')
        sender_kinde_id = event.get('sender_kinde_id')
        
        is_me = (self.me.kinde_user_id == sender_kinde_id)
        
        if not is_me and message_id:
            asyncio.create_task(self._mark_message_read_async(message_id, sender_kinde_id))
            asyncio.create_task(self._mark_delivered_and_notify(message_id, sender_kinde_id))
        
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'message': event['message'],
            'is_me': is_me,
            'is_read': True if not is_me and message_id else None
        }))

    async def _mark_delivered_and_notify(self, message_id, sender_kinde_id):
        """Optional: set delivered_at and send message_delivered to sender."""
        try:
            def _mark_delivered():
                now = django_timezone.now()
                updated = DirectMessage.objects.filter(
                    id=message_id,
                    receiver=self.me,
                    delivered_at__isnull=True
                ).update(delivered_at=now)
                return (updated > 0, now)
            marked, delivered_at = await sync_to_async(_mark_delivered)()
            if marked:
                delivered_at_iso = delivered_at.isoformat()
                if delivered_at_iso.endswith('+00:00'):
                    delivered_at_iso = delivered_at_iso[:-6] + 'Z'
                await self.channel_layer.group_send(self.room_group_name, {
                    'type': 'message_delivered_event',
                    'message_ids': [message_id],
                    'delivered_at': delivered_at_iso,
                })
        except Exception as e:
            logger.error(f"Error marking message as delivered: {e}")

    async def message_delivered_event(self, event):
        """Send message_delivered to WebSocket (sender sees single check)."""
        await self.send(text_data=json.dumps({
            'type': 'message_delivered',
            'message_ids': event.get('message_ids', []),
            'delivered_at': event.get('delivered_at'),
        }))

    async def _mark_message_read_async(self, message_id, sender_kinde_id):
        """Mark message as read asynchronously; notify sender via direct chat room (message_read_event)."""
        try:
            def _mark_as_read():
                now = django_timezone.now()
                updated = DirectMessage.objects.filter(
                    id=message_id,
                    receiver=self.me,
                    is_read=False
                ).update(is_read=True, read_at=now)
                return (updated > 0, now)
            
            marked, read_at = await sync_to_async(_mark_as_read)()
            
            if marked:
                read_at_iso = read_at.isoformat()
                if read_at_iso.endswith('+00:00'):
                    read_at_iso = read_at_iso[:-6] + 'Z'
                await self.channel_layer.group_send(self.room_group_name, {
                    'type': 'message_read_event',
                    'message_ids': [message_id],
                    'read_at': read_at_iso,
                    'reader_kinde_id': self.me.kinde_user_id,
                })
        except Exception as e:
            logger.error(f"Error marking message as read: {e}")

    async def message_read_event(self, event):
        """Send message_read to WebSocket (sender sees blue checkmarks)."""
        payload = {
            'type': 'message_read',
            'message_ids': event.get('message_ids', []),
            'read_at': event.get('read_at'),
        }
        if event.get('reader_kinde_id'):
            payload['reader_kinde_id'] = event['reader_kinde_id']
        await self.send(text_data=json.dumps(payload))

    async def typing_indicator(self, event):
        """Forward typing to other user in this direct chat."""
        await self.send(text_data=json.dumps({
            'type': 'typing',
            'sender_kinde_id': event.get('sender_kinde_id'),
        }))

    async def stop_typing_indicator(self, event):
        """Forward stop_typing to other user in this direct chat."""
        payload = {'type': 'stop_typing'}
        if event.get('sender_kinde_id'):
            payload['sender_kinde_id'] = event['sender_kinde_id']
        await self.send(text_data=json.dumps(payload))


# class DirectChatConsumer(AsyncWebsocketConsumer):
#     async def connect(self):
#         """Optimized direct chat connection using helper functions"""
#         try:
#             self.other_user_kinde_id = self.scope['url_route']['kwargs']['user_id']

#             # Check authentication
#             from django.contrib.auth.models import AnonymousUser
#             if isinstance(self.scope['user'], AnonymousUser):
#                 await self.close(code=4003)
#                 return

#             self.me = self.scope['user']

#             # Use your helper function to get the other user
#             self.other_user = await get_student_from_kinde_id(self.other_user_kinde_id)
#             if not self.other_user:
#                 await self.close(code=4004)
#                 return

#             # Prevent chat with self
#             if self.me.pk == self.other_user.pk:
#                 await self.close(code=4005)
#                 return
            
#             is_blocked = await self.check_blocking_status()
#             if is_blocked:
#                 await self.close(code=4006)  # Custom code for blocked users
#                 return

#             # Create consistent room name
#             user_pks = sorted([str(self.me.pk), str(self.other_user.pk)])
#             self.room_group_name = f'direct_chat_{user_pks[0]}_{user_pks[1]}'

#             # Join group and accept concurrently
#             await asyncio.gather(
#                 self.channel_layer.group_add(self.room_group_name, self.channel_name),
#                 self.accept()
#             )

#             # Mark the last unread message from the other user as read
#             await self.mark_last_message_as_read()
            
#             # Send connection confirmation
#             await self.send(text_data=json.dumps({
#                 'type': 'connection_established',
#                 'message': f'Connected to direct chat with {self.other_user.name}.'
#             }))
            
#             # Optionally send recent messages using your helper function
#             # recent_messages = await get_recent_direct_messages(self.me, self.other_user)
#             # await self.send(text_data=json.dumps({
#             #     'type': 'chat_history',
#             #     'messages': recent_messages
#             # }))
            
#         except Exception as e:
#             logger.error(f"Error in direct chat connect: {e}")
#             await self.close(code=4000)


#     async def check_blocking_status(self):
#         """Check if either user has blocked the other"""
#         try:
#             # Check if I blocked the other user OR the other user blocked me
#             block_exists = await Block.objects.filter(
#                 Q(blocker=self.me, blocked=self.other_user) |
#                 Q(blocker=self.other_user, blocked=self.me)
#             ).aexists()
            
#             return block_exists
#         except Exception as e:
#             logger.error(f"Error checking blocking status: {e}")
#             return False
    
#     async def mark_last_message_as_read(self):
#         """Mark all unread messages from the other user as read when connection opens"""
#         try:
#             def _mark_messages_read():
#                 # Get all unread messages sent by the other user to me
#                 unread_messages = DirectMessage.objects.filter(
#                     sender=self.other_user,
#                     receiver=self.me,
#                     is_read=False
#                 ).order_by('-timestamp')
                
#                 count = unread_messages.update(is_read=True)
                
#                 # Get the last message ID for notification (if any messages were marked)
#                 if count > 0:
#                     last_message = DirectMessage.objects.filter(
#                         sender=self.other_user,
#                         receiver=self.me,
#                         is_read=True
#                     ).order_by('-timestamp').first()
#                     return count, last_message.id if last_message else None
#                 return 0, None
            
#             count, last_message_id = await sync_to_async(_mark_messages_read)()
            
#             if count > 0 and last_message_id:
#                 # Notify the other user that their messages were read
#                 other_user_group = f'user_updates_{self.other_user.kinde_user_id}'
#                 await self.channel_layer.group_send(
#                     other_user_group,
#                     {
#                         'type': 'user_updated',
#                         'data': {
#                             'type': 'messages_read',
#                             'message_count': count,
#                             'last_message_id': last_message_id,
#                             'read_by': self.me.kinde_user_id,
#                             'read_by_name': self.me.name
#                         }
#                     }
#                 )
#                 logger.info(f"Marked {count} message(s) as read from {self.other_user.name} to {self.me.name}")
#         except Exception as e:
#             logger.error(f"Error marking messages as read: {e}")

#     async def disconnect(self, close_code):
#         """Clean disconnection"""
#         if hasattr(self, 'room_group_name'):
#             await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

#     async def receive(self, text_data):
#         """Optimized direct message handling using helper functions"""
#         try:
#             data = json.loads(text_data)
#         except json.JSONDecodeError:
#             await self.send_error("Invalid JSON format.")
#             return
            
#         message_text = data.get('message', '').strip()
#         image_id = data.get('image_id')
#         parent_message_id = data.get('parent_message_id')
#         post_id = data.get('post_id')  # Unused currently
#         community_post_id = data.get('community_post_id')  # Unused currently
#         student_event_id = data.get('student_event_id')  # Unused currently
#         community_event_id = data.get('community_event_id')
#         student_profile_id = data.get('student_profile_id')
#         community_profile_id = data.get('community_profile_id')

#         # Validate input
#         if not message_text and not image_id:
#             await self.send_error("Message cannot be empty.")
#             return
            
#         if message_text and len(message_text) > 1000:
#             await self.send_error("Message too long.")
#             return
        
#         is_blocked = await self.check_blocking_status()
#         if is_blocked:
#             await self.send_error("Cannot send message. User relationship has changed.")
#             return

#         try:
#             post_obj = None
#             community_post_obj = None
#             student_event_obj = None
#             community_event_obj = None
#             student_profile_obj = None
#             community_profile_obj = None

#             if post_id:
#                 try:
#                     post_obj = await Posts.objects.select_related('student').prefetch_related(
#                         'images',
#                         'student_mentions',
#                         'community_mentions'
#                     ).aget(pk=post_id)
#                 except Posts.DoesNotExist:
#                     await self.send_error("Associated post not found.")
#                     return

#             if community_post_id:
#                 try:
#                     community_post_obj = await Community_Posts.objects.select_related(
#                         'poster', 'community'
#                     ).prefetch_related(
#                         'images'
#                     ).aget(pk=community_post_id)
#                 except Community_Posts.DoesNotExist:
#                     await self.send_error("Associated community post not found.")
#                     return

#             if student_event_id:
#                 try:
#                     student_event_obj = await Student_Events.objects.select_related('student').aget(pk=student_event_id)
#                 except Student_Events.DoesNotExist:
#                     await self.send_error("Associated student event not found.")
#                     return

#             if community_event_id:
#                 try:
#                     community_event_obj = await Community_Events.objects.select_related(
#                         'community', 'poster'
#                     ).aget(pk=community_event_id)
#                 except Community_Events.DoesNotExist:
#                     await self.send_error("Associated community event not found.")
#                     return

#             if student_profile_id:
#                 try:
#                     student_profile_obj = await Student.objects.select_related('university').aget(pk=student_profile_id)
#                 except Student.DoesNotExist:
#                     await self.send_error("Student profile not found.")
#                     return

#             if community_profile_id:
#                 try:
#                     community_profile_obj = await Communities.objects.select_related('location').aget(pk=community_profile_id)
#                 except Communities.DoesNotExist:
#                     await self.send_error("Community profile not found.")
#                     return

#             parent_message_obj = None
#             if parent_message_id:
#                 try:
#                     parent_message_obj = await DirectMessage.objects.select_related(
#                         'sender', 'receiver'
#                     ).aget(pk=parent_message_id)
#                 except DirectMessage.DoesNotExist:
#                     await self.send_error("Reply message not found.")
#                     return

#             # Create or update message
#             if image_id:
#                 try:
#                     message_obj = await DirectMessage.objects.select_related(
#                         'sender', 'receiver', 'reply__sender', 'reply__receiver'
#                     ).aget(pk=image_id, sender=self.me)

#                     if message_text:
#                         message_obj.message = message_text
#                         await sync_to_async(message_obj.save)(update_fields=['message'])

#                 except DirectMessage.DoesNotExist:
#                     await self.send_error("Image message not found.")
#                     return
#             else:
#                 # Use your helper function to create new message
#                 try:
#                     message_obj = await create_direct_message(
#                         self.me,
#                         self.other_user,
#                         message_text=message_text,
#                         reply=parent_message_obj,
#                         post=post_obj,
#                         community_post=community_post_obj,
#                         student_event=student_event_obj,
#                         community_event=community_event_obj,
#                         student_profile=student_profile_obj,
#                         community_profile=community_profile_obj
#                     )
#                 except ValueError as e:
#                     await self.send_error(str(e))
#                     return

#             # Ensure related objects are loaded for serialization
#             if message_obj.post_id and post_obj:
#                 message_obj.post = post_obj
#             if message_obj.community_post_id and community_post_obj:
#                 message_obj.community_post = community_post_obj
#             if message_obj.student_event_id and student_event_obj:
#                 message_obj.student_event = student_event_obj
#             if message_obj.community_event_id and community_event_obj:
#                 message_obj.community_event = community_event_obj
#             if message_obj.student_profile_id and student_profile_obj:
#                 message_obj.student_profile = student_profile_obj
#             if message_obj.community_profile_id and community_profile_obj:
#                 message_obj.community_profile = community_profile_obj

#             # Broadcast message
#             await self.broadcast_message(message_obj)
            
#             # Check if receiver is connected and mark message as read
#             await self.check_and_mark_message_as_read(message_obj)

#             # Note: Notifications are handled by Django signals, not here
#             # await self.send_message_notification(message_obj)  # REMOVED - causes duplicates
            
#         except Exception as e:
#             logger.error(f"Error in direct chat receive: {e}")
#             await self.send_error("Failed to process message.")

#     async def send_message_notification(self, message_obj):
#         """Send push notification for new direct message"""
#         try:
#             # Check if the other user has muted me
#             is_muted = await MutedStudents.objects.filter(
#                 student=self.other_user,
#                 muted_student=self.me
#             ).aexists()
            
#             if is_muted:
#                 return  # Don't send notification if user is muted
            
#             # Prepare notification data
#             title = f"New message from {self.me.name}"
#             body = message_obj.message[:50] + "..." if len(message_obj.message) > 50 else message_obj.message
            
#             notification_data = {
#                 "type": "direct_message",
#                 "sender_id": str(self.me.id),
#                 "sender_name": self.me.name,
#                 "sender_kinde_id": self.me.kinde_user_id,
                
#             }
            
#             # Send push notification using sync_to_async
#             await sync_to_async(send_push_notifications_to_user)(
#                 user=self.other_user,
#                 title=title,
#                 body=body,
#                 data=notification_data
#             )
            
#             # Optionally create a database notification record
#             await Notification.objects.acreate(
#                 recipient=self.other_user,
#                 content=f"New message from {self.me.name}: {body}",
#                 notificationtype_id=1,  # Assuming 1 is for direct messages
#                 # Add any other fields your Notification model needs
#             )
            
#         except Exception as e:
#             logger.error(f"Error sending message notification: {e}")


#     async def broadcast_message(self, message_obj):
#         """Optimized message broadcasting"""
#         serialized_message = await asyncio.to_thread(
#             self._build_direct_message_payload,
#             message_obj
#         )

#         await self.channel_layer.group_send(
#             self.room_group_name,
#             {
#                 'type': 'chat_message',
#                 'message': serialized_message,
#                 'sender_kinde_id': self.me.kinde_user_id,
#                 'message_id': message_obj.id
#             }
#         )

#     def _build_direct_message_payload(self, message_obj):
#         """Build the direct message payload with nested objects without extra DB hits."""
#         context = {'request': None, 'kinde_user_id': getattr(self, 'kinde_user_id', None)}

#         sender_data = StudentChatSerializer(message_obj.sender, context=context).data if message_obj.sender else None
#         receiver_data = StudentChatSerializer(message_obj.receiver, context=context).data if message_obj.receiver else None

#         payload = {
#             'id': message_obj.id,
#             'sender': sender_data,
#             'receiver': receiver_data,
#             'message': message_obj.message,
#             'image_url': message_obj.image.url if message_obj.image else None,
#             'timestamp': message_obj.timestamp.isoformat() if message_obj.timestamp else None,
#             'is_read': message_obj.is_read,
#         }

#         if getattr(message_obj, 'reply', None):
#             payload['reply'] = DirectMessageParentSerializer(message_obj.reply, context=context).data

#         if getattr(message_obj, 'post', None):
#             payload['post'] = PostNameSerializer(message_obj.post, context=context).data

#         if getattr(message_obj, 'community_post', None):
#             payload['community_post'] = CommunityPostNameSerializer(message_obj.community_post, context=context).data

#         if getattr(message_obj, 'student_event', None):
#             payload['student_event'] = StudentEventNameSerializer(message_obj.student_event, context=context).data

#         if getattr(message_obj, 'community_event', None):
#             payload['community_event'] = CommunityEventsNameSerializer(message_obj.community_event, context=context).data

#         if getattr(message_obj, 'student_profile', None):
#             payload['student_profile'] = StudentNameSerializer(message_obj.student_profile, context=context).data

#         if getattr(message_obj, 'community_profile', None):
#             payload['community_profile'] = CommunityNameSerializer(message_obj.community_profile, context=context).data

#         return payload
    
#     async def check_and_mark_message_as_read(self, message_obj):
#         """Check if receiver is connected and mark message as read if they are"""
#         try:
#             # The chat_message handler will mark it as read when receiver receives it
#             # This method is kept for potential future use but the actual marking
#             # happens in chat_message handler when the receiver processes the message
#             pass
#         except Exception as e:
#             logger.error(f"Error in check_and_mark_message_as_read: {e}")

#     async def send_error(self, message):
#         """Helper to send error messages"""
#         await self.send(text_data=json.dumps({"error": message}))

#     async def chat_message(self, event):
#         """Handle broadcasted messages"""
#         message_id = event.get('message_id')
#         sender_kinde_id = event.get('sender_kinde_id')
        
#         # If this message is for me (I'm the receiver) and I'm connected, mark it as read
#         is_me = (self.me.kinde_user_id == sender_kinde_id)
#         if not is_me and message_id:
#             # This message is from the other user to me
#             try:
#                 def _mark_as_read():
#                     try:
#                         message = DirectMessage.objects.get(id=message_id, receiver=self.me)
#                         if not message.is_read:
#                             message.is_read = True
#                             message.save(update_fields=['is_read'])
#                             return True
#                     except DirectMessage.DoesNotExist:
#                         return False
#                     return False
                
#                 marked = await sync_to_async(_mark_as_read)()
                
#                 if marked:
#                     # Notify sender that their message was read
#                     sender_group = f'user_updates_{sender_kinde_id}'
#                     await self.channel_layer.group_send(
#                         sender_group,
#                         {
#                             'type': 'user_updated',
#                             'data': {
#                                 'type': 'message_read',
#                                 'message_id': message_id,
#                                 'read_by': self.me.kinde_user_id,
#                                 'read_by_name': self.me.name
#                             }
#                         }
#                     )
#                     logger.info(f"Marked message {message_id} as read when received via WebSocket")
#             except Exception as e:
#                 logger.error(f"Error marking message as read in chat_message handler: {e}")
        
#         await self.send(text_data=json.dumps({
#             'type': 'chat_message',
#             'message': event['message'],
#             'is_me': is_me,
#             'is_read': True if not is_me and message_id else None  # Include read status for received messages
#         }))


class UnifiedChatsConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for unified chats list updates
    Broadcasts simple updates when new messages are created in direct or community chats
    """
    
    async def connect(self):
        try:
            from django.contrib.auth.models import AnonymousUser

            self.kinde_user_id = self.scope['url_route']['kwargs']['kinde_user_id']

            # Authentication: connecting user must match URL parameter (prevent guessing another user's feed)
            if isinstance(self.scope.get('user'), AnonymousUser):
                await self.close(code=4003)
                return
            if self.scope['user'].kinde_user_id != self.kinde_user_id:
                await self.close(code=4003)
                return

            # Fetch student once at connect and reuse in _enhance_update_data (avoids N DB hits per update)
            self.student = await get_student_from_kinde_id(self.kinde_user_id)
            if not self.student:
                logger.error(f"User not found for kinde_user_id: {self.kinde_user_id}")
                await self.close()
                return

            self.room_group_name = f'unified_chats_{self.kinde_user_id}'
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            await self.accept()
            await self.send(text_data=json.dumps({
                'type': 'connection_established',
                'message': 'Connected to unified chats updates',
                'user_id': self.kinde_user_id
            }))
            if settings.DEBUG:
                logger.info(f"UnifiedChatsConsumer connected for user: {self.kinde_user_id}")
        except Exception as e:
            logger.error(f"Error in UnifiedChatsConsumer connect: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
    
    # Receive message from room group
    async def unified_chats_update(self, event):
        """Handle unified chats update from signals"""
        try:
            update_data = event.get('update_data', {})
            enhanced_data = await self._enhance_update_data(update_data)
            await self.send(text_data=json.dumps({
                'type': 'unified_chats_update',
                'message': 'Chat list updated',
                'update_data': enhanced_data
            }))
            if settings.DEBUG:
                logger.info(f"UnifiedChatsConsumer sent update to user: {self.kinde_user_id}")
        except Exception as e:
            logger.error(f"Error in unified chats update: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def _enhance_update_data(self, update_data):
        """Enhance update with conversation name, etc. Uses self.student from connect (no per-update re-fetch)."""
        try:
            from .models import (
                Student,
                Communities,
                DirectMessage,
                CommunityChatMessage,
            )

            conversation_type = update_data.get('conversation_type')
            conversation_target_id = update_data.get('conversation_target_id')
            message_id = update_data.get('message_id')

            enhanced_data = update_data.copy()
            enhanced_data.setdefault('is_read', True)

            # Reuse student cached at connect (avoids N DB hits per message when many users have chat list open)
            current_student = getattr(self, 'student', None)

            if conversation_type == 'direct_chat':
                try:
                    target_user = await Student.objects.aget(kinde_user_id=conversation_target_id)
                    enhanced_data.update({
                        'conversation_name': target_user.name,
                        'conversation_username': target_user.username,
                        'conversation_profile_picture': target_user.profile_image.url
                        if target_user.profile_image
                        else None,
                        'conversation_kinde_id': target_user.kinde_user_id,
                        'conversation_id': target_user.id,
                    })
                except Student.DoesNotExist:
                    logger.warning(
                        "UnifiedChatsConsumer: target user not found for kinde_id %s",
                        conversation_target_id,
                    )

                if message_id:
                    try:
                        message = await DirectMessage.objects.select_related(
                            'sender', 'receiver'
                        ).aget(id=message_id)

                        # Type-safe ID comparison (int vs int) so sender isn't misclassified and shown unread
                        current_id = int(current_student.id) if current_student else None
                        sender_id = int(message.sender_id)
                        receiver_id = int(message.receiver_id)
                        if current_id is not None:
                            if sender_id == current_id:
                                enhanced_data['is_read'] = True  # I sent it
                            elif receiver_id == current_id:
                                enhanced_data['is_read'] = message.is_read  # I received it
                            else:
                                enhanced_data['is_read'] = True
                        else:
                            # Fallback if student not set (shouldn't happen after connect)
                            enhanced_data['is_read'] = True
                    except DirectMessage.DoesNotExist:
                        logger.warning(
                            "UnifiedChatsConsumer: direct message %s not found for update",
                            message_id,
                        )

            elif conversation_type == 'community_chat':
                try:
                    community = await Communities.objects.aget(id=conversation_target_id)
                    enhanced_data.update({
                        'conversation_name': community.community_name,
                        'conversation_profile_picture': community.community_image.url
                        if community.community_image
                        else None,
                        'conversation_id': community.id,
                        'conversation_tag': community.community_tag,
                        'conversation_bio': community.community_bio,
                    })
                except Communities.DoesNotExist:
                    logger.warning(
                        "UnifiedChatsConsumer: community %s not found for update",
                        conversation_target_id,
                    )

                if message_id and current_student:
                    try:
                        community_message = await CommunityChatMessage.objects.select_related(
                            'student', 'community'
                        ).aget(id=message_id)

                        if int(community_message.student_id) == int(current_student.id):
                            enhanced_data['is_read'] = True
                        else:
                            try:
                                is_read = await community_message.read_by.filter(
                                    id=current_student.id
                                ).aexists()
                            except AttributeError:
                                is_read = await sync_to_async(
                                    community_message.read_by.filter(
                                        id=current_student.id
                                    ).exists
                                )()
                            enhanced_data['is_read'] = is_read
                    except CommunityChatMessage.DoesNotExist:
                        logger.warning(
                            "UnifiedChatsConsumer: community message %s not found for update",
                            message_id,
                        )

            return enhanced_data

        except Exception as e:
            logger.error(f"Error enhancing update data: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return update_data
    
    # Receive message from WebSocket (for client requests)
    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message_type = text_data_json.get('type')
        
        if message_type == 'ping':
            # Rate limit ping responses to prevent excessive traffic
            # Only respond to ping if at least 5 seconds have passed since last pong
            current_time = asyncio.get_event_loop().time()
            if not hasattr(self, '_last_pong_time'):
                self._last_pong_time = 0
            
            if current_time - self._last_pong_time >= 5.0:  # 5 second minimum between pongs
                self._last_pong_time = current_time
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'message': 'Connection alive'
                }))
            # Silently ignore pings that come too frequently


class PostCommentsConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time post comment updates
    """
    
    async def connect(self):
        self.post_id = self.scope['url_route']['kwargs']['post_id']
        self.room_group_name = f'post_comments_{self.post_id}'
        
        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'Connected to post {self.post_id} comments updates'
        }))
        
    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
    
    # Receive message from room group
    async def comment_added(self, event):
        """Handle new comment notification"""
        await self.send(text_data=json.dumps({
            'type': 'comment_added',
            'comment': event['comment_data']
        }))
    
    async def comment_deleted(self, event):
        """Handle comment deletion notification"""
        await self.send(text_data=json.dumps({
            'type': 'comment_deleted',
            'comment_id': event['comment_id']
        }))


class CommunityPostCommentsConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time community post comment updates
    """
    
    async def connect(self):
        self.community_post_id = self.scope['url_route']['kwargs']['community_post_id']
        self.room_group_name = f'community_post_comments_{self.community_post_id}'
        
        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'Connected to community post {self.community_post_id} comments updates'
        }))
        
    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
    
    # Receive message from room group
    async def comment_added(self, event):
        """Handle new comment notification"""
        await self.send(text_data=json.dumps({
            'type': 'comment_added',
            'comment': event['comment_data']
        }))
    
    async def comment_deleted(self, event):
        """Handle comment deletion notification"""
        await self.send(text_data=json.dumps({
            'type': 'comment_deleted',
            'comment_id': event['comment_id']
        }))


class StudentEventDiscussionsConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time student event discussion updates
    """
    
    async def connect(self):
        self.student_event_id = self.scope['url_route']['kwargs']['student_event_id']
        self.room_group_name = f'student_event_discussions_{self.student_event_id}'
        
        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'Connected to student event {self.student_event_id} discussions updates'
        }))
        
    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
    
    # Receive message from room group
    async def discussion_added(self, event):
        """Handle new discussion notification"""
        await self.send(text_data=json.dumps({
            'type': 'discussion_added',
            'discussion': event['discussion_data']
        }))
    
    async def discussion_deleted(self, event):
        """Handle discussion deletion notification"""
        await self.send(text_data=json.dumps({
            'type': 'discussion_deleted',
            'discussion_id': event['discussion_id']
        }))


class CommunityEventDiscussionsConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time community event discussion updates
    """
    
    async def connect(self):
        self.community_event_id = self.scope['url_route']['kwargs']['community_event_id']
        self.room_group_name = f'community_event_discussions_{self.community_event_id}'
        
        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'Connected to community event {self.community_event_id} discussions updates'
        }))
        
    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
    
    # Receive message from room group
    async def discussion_added(self, event):
        """Handle new discussion notification"""
        await self.send(text_data=json.dumps({
            'type': 'discussion_added',
            'discussion': event['discussion_data']
        }))
    
    async def discussion_deleted(self, event):
        """Handle discussion deletion notification"""
        await self.send(text_data=json.dumps({
            'type': 'discussion_deleted',
            'discussion_id': event['discussion_id']
        }))