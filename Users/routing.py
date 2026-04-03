from django.urls import re_path
from . import consumers


websocket_urlpatterns = [
    # ... existing chat/notification paths ...
    re_path(r'ws/posts/updates/$', consumers.PostUpdateConsumer.as_asgi()),
    re_path(r'ws/chat/direct/(?P<user_id>\w+)/?$', consumers.DirectChatConsumer.as_asgi()), # user_id is Kinde ID of other user
    re_path(r'ws/chat/community/(?P<community_id>\d+)/?$', consumers.CommunityChatConsumer.as_asgi()), # community_id is PK of Community model
    re_path(r'ws/feed/updates/$', consumers.FeedUpdateConsumer.as_asgi()),
    re_path(r'ws/posts/(?P<post_id>\d+)/updates/?$', consumers.SinglePostUpdateConsumer.as_asgi()),
    re_path(r'ws/community-posts/(?P<community_post_id>\d+)/updates/?$', consumers.SingleCommunityPostUpdateConsumer.as_asgi()),
    re_path(r'ws/events/(?P<event_id>\d+)/updates/?$', consumers.SingleEventUpdateConsumer.as_asgi()),
    re_path(r'ws/community-events/(?P<community_event_id>\d+)/updates/?$', consumers.SingleCommunityEventUpdateConsumer.as_asgi()),
    re_path(r'ws/users/(?P<kinde_user_id>\w+)/updates/?$', consumers.UserUpdateConsumer.as_asgi()),
    # Multi-item view WebSocket routes
    re_path(r'ws/student-posts/(?P<student_id>\d+)/updates/?$', consumers.StudentPostsUpdateConsumer.as_asgi()),
    re_path(r'ws/community-posts-list/(?P<community_id>\d+)/updates/?$', consumers.CommunityPostsListUpdateConsumer.as_asgi()),
    re_path(r'ws/student-events-list/(?P<student_id>\d+)/updates/?$', consumers.StudentEventsListUpdateConsumer.as_asgi()),
    re_path(r'ws/community-events-list/(?P<community_id>\d+)/updates/?$', consumers.CommunityEventsListUpdateConsumer.as_asgi()),
    # Feed WebSocket routes
    re_path(r'ws/postfeed/(?P<kinde_user_id>\w+)/updates/?$', consumers.PostFeedUpdateConsumer.as_asgi()),
    re_path(r'ws/eventsfeed/(?P<kinde_user_id>\w+)/updates/?$', consumers.EventsFeedUpdateConsumer.as_asgi()),
    # Unified chats WebSocket route
    re_path(r'ws/unified-chats/(?P<kinde_user_id>\w+)/updates/?$', consumers.UnifiedChatsConsumer.as_asgi()),
    # Comments and discussions WebSocket routes
    re_path(r'ws/posts/(?P<post_id>\d+)/comments/?$', consumers.PostCommentsConsumer.as_asgi()),
    re_path(r'ws/community-posts/(?P<community_post_id>\d+)/comments/?$', consumers.CommunityPostCommentsConsumer.as_asgi()),
    re_path(r'ws/student-events/(?P<student_event_id>\d+)/discussions/?$', consumers.StudentEventDiscussionsConsumer.as_asgi()),
    re_path(r'ws/community-events/(?P<community_event_id>\d+)/discussions/?$', consumers.CommunityEventDiscussionsConsumer.as_asgi()),
    # Notifications WebSocket route
    re_path(r'ws/notifications/(?P<kinde_user_id>\w+)/?$', consumers.NotificationConsumer.as_asgi()),
    # Group chat WebSocket route
    re_path(r'ws/chat/group/(?P<group_id>\d+)/?$', consumers.GroupChatConsumer.as_asgi()),

]