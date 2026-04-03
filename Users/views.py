from .models import *
from django.http import JsonResponse, HttpResponse, response
from rest_framework.decorators import api_view
from .serializers import *
from .cache_utils import *
from .kinde_functions import *
from .kinde_functions import _classify_token_error_message
from django.db import transaction
import json
import random
import string
from django.core.mail import send_mail
from django.db.models import Count
from collections import defaultdict
from .tasks import send_verification_email, send_welcome_email, send_account_deletion_notification, send_admin_deletion_notification
from django.utils import timezone
import math
from datetime import datetime, timedelta
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import (
    Q,
    F,
    Case,
    When,
    OuterRef,
    Subquery,
    Prefetch,
    IntegerField,
    Value,
    FloatField,
    ExpressionWrapper,
)
from django.db.models.functions import Greatest, Coalesce
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync, sync_to_async
from django.views.decorators.cache import cache_control
from .scoring import (
    get_interest_overlap_annotations, get_location_match_annotations,
    get_author_friend_annotations, get_community_membership_annotations,
    W_AUTHOR_FRIEND, W_MEMBER_COMMUNITY,
)
from django.shortcuts import render
from django.conf import settings



def _parse_pagination_params(request, default_limit=20, max_limit=50):
    """Utility to extract and normalize limit/offset query parameters."""
    try:
        limit = int(request.GET.get('limit', default_limit))
    except (TypeError, ValueError):
        limit = default_limit

    try:
        offset = int(request.GET.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0

    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)

    return limit, offset


def _get_pending_deletion_student_ids():
    """
    Returns a list of student IDs who have pending (non-cancelled) deletion requests.
    These students' data should be hidden from all feeds and public views.
    """
    from .models import DataDeletionRequest
    return list(
        DataDeletionRequest.objects.filter(
            is_cancelled=False,
            deleted_at__isnull=True
        ).values_list('student_id', flat=True)
    )


def _serialize_student_brief(student):
    if not student:
        return None
    return {
        'id': student.id,
        'name': student.name,
        'username': getattr(student, 'username', None),
        'kinde_user_id': getattr(student, 'kinde_user_id', None),
        'profile_image': student.profile_image.url if getattr(student, 'profile_image', None) else None,
    }


def _serialize_community_brief(community):
    if not community:
        return None
    return {
        'id': community.id,
        'community_name': community.community_name,
        'community_tag': community.community_tag,
        'community_image': community.community_image.url if getattr(community, 'community_image', None) else None,
    }


def build_student_event_payload(event, kinde_user_id=None):
    payload = {
        'id': event.id,
        'event_name': event.event_name,
        'description': event.description,
        'RSVP': event.RSVP,
        'student': event.student_id,
        'date': event.date.isoformat() if event.date else None,
        'dateposted': event.dateposted.isoformat() if event.dateposted else None,
        'student_username': getattr(event.student, 'username', None),
        'student_profile_picture': event.student.profile_image.url if getattr(event.student, 'profile_image', None) else None,
        'student_name': getattr(event.student, 'name', None),
        'final_score': float(getattr(event, 'final_score', 0.0) or 0.0),
        'popularity_score': float(getattr(event, 'popularity_score', 0.0) or 0.0),
        'interest_match_score': float(getattr(event, 'interest_match_score', 0.0) or 0.0),
        'friend_activity_score': float(getattr(event, 'friend_activity_score', 0.0) or 0.0),
        'location_score': float(getattr(event, 'location_score', 0.0) or 0.0),
    }

    rsvp_count = getattr(event, 'rsvp_count', None)
    if rsvp_count is None and hasattr(event, 'eventrsvp'):
        rsvp_count = len(event.eventrsvp.all())
    payload['rsvp_count'] = rsvp_count if rsvp_count is not None else 0

    comment_count = getattr(event, 'comment_count', None)
    if comment_count is None and hasattr(event, 'student_events_discussion_set'):
        comment_count = len(event.student_events_discussion_set.all())
    payload['comment_count'] = comment_count if comment_count is not None else 0

    is_rsvpd = False
    if kinde_user_id:
        if hasattr(event, '_prefetched_objects_cache') and 'eventrsvp' in event._prefetched_objects_cache:
            is_rsvpd = any(rsvp.student and rsvp.student.kinde_user_id == kinde_user_id for rsvp in event.eventrsvp.all())
        else:
            is_rsvpd = EventRSVP.objects.filter(event=event, student__kinde_user_id=kinde_user_id).exists()
    payload['is_rsvpd'] = is_rsvpd

    is_bookmarked = False
    if kinde_user_id:
        if hasattr(event, '_prefetched_objects_cache') and 'bookmarkedstudentevents_set' in event._prefetched_objects_cache:
            is_bookmarked = any(bookmark.student and bookmark.student.kinde_user_id == kinde_user_id for bookmark in event.bookmarkedstudentevents_set.all())
        else:
            is_bookmarked = BookmarkedStudentEvents.objects.filter(student_event=event, student__kinde_user_id=kinde_user_id).exists()
    payload['isBookmarked'] = is_bookmarked

    payload['is_mine'] = bool(kinde_user_id and event.student and event.student.kinde_user_id == kinde_user_id)

    payload['images'] = [
        {
            'id': image.id,
            'image_url': image.image.url if image.image else None,
        }
        for image in getattr(event, 'images', []).all()
    ]

    payload['student_mentions'] = [
        _serialize_student_brief(student) for student in getattr(event, 'student_mentions', []).all()
    ]

    payload['community_mentions'] = [
        _serialize_community_brief(community) for community in getattr(event, 'community_mentions', []).all()
    ]

    return payload


def _prune_expired_notifications():
    """Delete notifications older than 2 months."""
    cutoff = timezone.now() - timedelta(days=60)
    Notification.objects.filter(created_at__lt=cutoff).delete()


def _notification_list_queryset(base_qs):
    """
    Apply select_related and prefetch_related to a Notification queryset so that
    NotificationSerializer (and nested name serializers) do not trigger N+1 queries.
    """
    return (
        base_qs
        .select_related(
            'sender',
            'notificationtype',
            'post',
            'post__student',
            'community_post',
            'community_post__community',
            'community_post__poster',
            'student_event',
            'student_event__student',
            'community_event',
            'community_event__community',
            'community_event__poster',
            'post_comment',
            'post_comment__post',
            'post_comment__post__student',
            'post_comment__student',
            'community_post_comment',
            'community_post_comment__community_post',
            'community_post_comment__community_post__community',
            'community_post_comment__community_post__poster',
            'community_post_comment__student',
            'student_event_discussion',
            'student_event_discussion__student_event',
            'student_event_discussion__student_event__student',
            'student_event_discussion__student',
            'community_event_discussion',
            'community_event_discussion__community_event',
            'community_event_discussion__community_event__community',
            'community_event_discussion__community_event__poster',
            'community_event_discussion__student',
        )
        .prefetch_related(
            'post__images',
            'post__videos',
            'post__student_mentions',
            'post__community_mentions',
            'community_post__images',
            'community_post__community_mentions',
            'community_post__student_mentions',
            'student_event__images',
            'student_event__videos',
            'student_event__student_mentions',
            'student_event__community_mentions',
            'community_event__images',
            'community_event__videos',
            'community_event__student_mentions',
            'community_event__community_mentions',
            'post_comment__post__images',
            'post_comment__post__videos',
            'post_comment__post__student_mentions',
            'post_comment__post__community_mentions',
            'community_post_comment__community_post__images',
            'community_post_comment__community_post__student_mentions',
            'community_post_comment__community_post__community_mentions',
        )
    )

# Helper function to broadcast post updates to all relevant feed groups
def broadcast_post_update_to_feeds(post_data, post_type='post'):
    """
    Broadcast post updates to all users' post feeds
    """
    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            # Broadcast to global feed updates (existing functionality)
            async_to_sync(channel_layer.group_send)(
                'global_feed_updates',
                {
                    'type': 'feed.update',
                    'data': {
                        'update_type': f'{post_type}_updated',
                        'content_type': post_type,
                        'item_id': post_data.get('id'),
                        'item_data': post_data
                    }
                }
            )
        except Exception as e:
            print(f"Error broadcasting post update to feeds: {e}")

# Helper function to broadcast event updates to all relevant feed groups
def broadcast_event_update_to_feeds(event_data, event_type='student_event'):
    """
    Broadcast event updates to all users' events feeds
    """
    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            # Broadcast to global feed updates (existing functionality)
            async_to_sync(channel_layer.group_send)(
                'global_feed_updates',
                {
                    'type': 'feed.update',
                    'data': {
                        'update_type': f'{event_type}_updated',
                        'content_type': event_type,
                        'item_id': event_data.get('id'),
                        'item_data': event_data
                    }
                }
            )
        except Exception as e:
            print(f"Error broadcasting event update to feeds: {e}")
from .consumers import create_direct_message, create_community_message, create_community_message_for_sharable, create_direct_message_for_sharable
from django.shortcuts import get_object_or_404
from django.db import transaction
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.decorators import parser_classes, api_view
from Users.scoring import *
from channels.db import database_sync_to_async
from django.db.models import ObjectDoesNotExist
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import DefaultStorage # <--- IMPORT THIS
import logging
logger = logging.getLogger(__name__)
import asyncio
from .firebase_utils import send_push_notification
import json
import re


KINDE_USERINFO_URL = "https://studify.kinde.com/oauth2/v2/user_profile"

def welcome_user(request):
    from django.shortcuts import render
    return render(request, 'welcome.html')

def terms_of_service(request):
    from django.shortcuts import render
    return render(request, 'terms_of_service.html')

def privacy_policy(request):
    from django.shortcuts import render
    return render(request, 'privacy_policy.html')

def delete_account(request):
    from django.shortcuts import render
    return render(request, 'delete_account.html')

def open_app(request):
    """Universal link handler page for deep linking to mobile app"""
    from django.shortcuts import render
    return render(request, 'open_app.html')


# --- Smart QR / Download page: redirect by device (iOS → App Store, Android → Play Store, Desktop → landing) ---
# iOS app is live on the App Store.
IS_IOS_LIVE = True
IOS_APP_ID = '6757610530'
IOS_APP_STORE_URL = 'https://apps.apple.com/gb/app/studico/id6757610530'
ANDROID_PLAY_STORE_URL = 'https://play.google.com/store/apps/details?id=com.studico.studify'
# Replace with your TestFlight link while iOS is in review.
TESTFLIGHT_URL = ''


@cache_control(max_age=0, no_cache=True, no_store=True, must_revalidate=True)
def smart_download(request):
    """
    Smart download: detects device from User-Agent and either redirects to store or shows landing page.
    iOS in review: show landing with TestFlight option; set IS_IOS_LIVE=True when approved.
    """
    from django.shortcuts import render, redirect

    user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    is_ios = bool(re.search(r'iphone|ipad|ipod', user_agent))
    is_android = bool(re.search(r'android', user_agent))
    is_mobile = is_ios or is_android or bool(re.search(r'mobile', user_agent))

    # Optional: track QR/download page visits (if QRScan model exists)
    try:
        from .models import QRScan
        QRScan.objects.create(
            user_agent=user_agent[:500],
            device_type='ios' if is_ios else 'android' if is_android else 'desktop',
            ip_address=request.META.get('REMOTE_ADDR'),
            referer=(request.META.get('HTTP_REFERER') or '')[:500],
        )
    except Exception:
        pass

    skip_redirect = request.GET.get('redirect') == 'false'

    if is_ios:
        if IS_IOS_LIVE and not skip_redirect:
            return redirect(IOS_APP_STORE_URL)
        context = {
            'status': 'review',
            'is_ios': True,
            'is_mobile': True,
            'is_android': False,
            'is_desktop': False,
            'ios_url': IOS_APP_STORE_URL,
            'android_url': ANDROID_PLAY_STORE_URL,
            'testflight_url': TESTFLIGHT_URL or IOS_APP_STORE_URL,
        }
        return render(request, 'download.html', context)

    if is_android and not skip_redirect:
        return redirect(ANDROID_PLAY_STORE_URL)

    # Desktop or unknown: show landing page
    context = {
        'status': None,
        'is_ios': False,
        'is_android': False,
        'is_mobile': is_mobile,
        'is_desktop': True,
        'ios_url': IOS_APP_STORE_URL,
        'android_url': ANDROID_PLAY_STORE_URL,
        'testflight_url': TESTFLIGHT_URL or IOS_APP_STORE_URL,
    }
    return render(request, 'download.html', context)


def generate_qr_code(request):
    """Generate QR code image pointing to the smart download URL."""
    try:
        import qrcode
        from io import BytesIO
    except ImportError:
        return HttpResponse(b'', status=501, content_type='image/png')
    download_url = request.build_absolute_uri('/download/')
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(download_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return HttpResponse(buffer.getvalue(), content_type='image/png')


def robots_txt(request):
    """Returns robots.txt content"""
    from django.http import HttpResponse
    content = """User-agent: *
Allow: /
Sitemap: https://www.teamstudico.com/sitemap.xml
"""
    return HttpResponse(content, content_type='text/plain')


@parser_classes([MultiPartParser, FormParser])
async def create_student(request):
    profile_image = request.FILES.get('profile_image')
    data = request.POST
    
    name = data.get('name')
    username = data.get('username')
    bio = data.get('bio')
    university_id = data.get('university')
    interest_ids = data.getlist('interest', []) # Use getlist for multiple interests
    student_email = data.get('student_email')
    student_location_id = data.get('location')

    if not all([name, username, university_id, student_email, student_location_id]):
        return JsonResponse({'message': 'Missing required fields.'}, status=status.HTTP_400_BAD_REQUEST)
    
    if await sync_to_async(Student.objects.filter(username=username).aexists)() or await sync_to_async(Communities.objects.filter(community_tag=username).aexists)():
        return JsonResponse({'message': 'Username already in use, please choose another.'}, status=status.HTTP_400_BAD_REQUEST)
    
    if await sync_to_async(Student.objects.filter(student_email=student_email).aexists)():
        return JsonResponse({'message': 'Student email already assigned to an account, please choose another.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        university = await University.objects.aget(pk=university_id)
        location = await Location.objects.aget(pk=student_location_id)
        
        async with sync_to_async(transaction.atomic, thread_sensitive=True)():
            student = await Student.objects.acreate(
                name=name,
                username=username,
                bio=bio,
                university=university,
                student_email=student_email,
                student_location=location,
                profile_image=profile_image
            )
            interests = await sync_to_async(list)(Interests.objects.filter(pk__in=interest_ids))
            await sync_to_async(student.student_interest.set)(interests)
        
        serializer = await sync_to_async(StudentSerializer)(student)
        
        return JsonResponse({'message': 'Student created successfully!', 'student': serializer.data}, status=status.HTTP_201_CREATED)
    
    except (University.DoesNotExist, Location.DoesNotExist):
        return JsonResponse({'message': 'Invalid university or location ID.'}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return JsonResponse({'message': f'Failed to create student: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


    







@csrf_exempt
@kinde_auth_required
@parser_classes([MultiPartParser, FormParser])
async def create_community(request, kinde_user_id=None):
    # Use request.FILES.get for a single file upload
    community_image_file = request.FILES.get('community_image') 
    data = request.POST
    
    name = data.get('community_name')
    bio = data.get('community_bio')
    description = data.get('description')
    community_tag = data.get('community_tag')

    if not all([name, bio, description, community_tag]):
        return JsonResponse({"status": "error", "message": "Missing required fields."}, status=status.HTTP_400_BAD_REQUEST)
    
    # --- Check for existing tag asynchronously (aexists is already async) ---
    if await Communities.objects.filter(community_tag=community_tag).aexists() or await Student.objects.filter(username=community_tag).aexists():
        return JsonResponse({"status": "error", "message": "Community tag or username already in use, please choose another."}, status=status.HTTP_400_BAD_REQUEST)

    if len(description) > 1000:
        return JsonResponse({"status": "error", "message": "Description is too long. Keep it under 1000 characters."}, status=status.HTTP_400_BAD_REQUEST)

    # Verify student exists before proceeding
    try:
        student_exists = await Student.objects.filter(kinde_user_id=kinde_user_id).aexists()
        if not student_exists:
            return JsonResponse({"status": "error", "message": "Authenticated student not found."}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return JsonResponse({"status": "error", "message": "Failed to verify student."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    try:
        # Use sync_to_async to wrap the atomic transaction block
        # Pass kinde_user_id and fetch student inside sync context to avoid session issues
        def create_community_with_membership():
            with transaction.atomic():
                # Fetch student in sync context
                sender_student = Student.objects.get(kinde_user_id=kinde_user_id)
                
                # Create community
                community = Communities.objects.create(
                    community_name=name,
                    community_bio=bio,
                    description=description,
                    community_tag=community_tag,
                    community_image=community_image_file
                )
                
                # Create membership with admin role
                Membership.objects.create(user=sender_student, community=community, role="admin")
                return community
        
        community = await sync_to_async(create_community_with_membership)()
        
        # Get user's memberships and roles (the newly created community will be included)
        user_memberships = await sync_to_async(set)(
            Membership.objects.filter(user__kinde_user_id=kinde_user_id).values_list('community_id', flat=True)
        )
        
        # Get user's roles in communities
        user_community_roles = await sync_to_async(dict)(
            Membership.objects.filter(user__kinde_user_id=kinde_user_id).values_list('community_id', 'role')
        )
        
        # Serialize the community data
        serializer = CommunitySerializer(community, context={
            'kinde_user_id': kinde_user_id,
            'user_memberships': user_memberships,
            'user_community_roles': user_community_roles
        })
        serializer_data = await sync_to_async(lambda: serializer.data)()
        
        return JsonResponse({"status": "success", "message": "Community created successfully.", "community": serializer_data}, status=status.HTTP_201_CREATED)
    
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Failed to create community: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)





#reggie table tennis and fmnb 
@api_view(['POST'])
@kinde_auth_required
def join_community(request, kinde_user_id=None):
    data = request.data

    community = data.get('community_id')

    student_id = Student.objects.get(kinde_user_id=kinde_user_id)

    try:
        community_id = Communities.objects.get(id=community)
    except Communities.DoesNotExist:
        return JsonResponse({'message': 'Community does not exist.'}, status=404)




    membership, created = Membership.objects.get_or_create(user=student_id, community=community_id, role="member")
    if created:
        return JsonResponse({'message': 'User Joined Community'})
    return JsonResponse({'message': 'User already part of community.'})

@api_view(['POST'])
@kinde_auth_required
def leave_community(request, kinde_user_id=None):
    data = request.data
    community = data.get('community_id')

    if not community:
        return JsonResponse({'message': 'Invalid data.'}, status=400)

    try:
        student_id = Student.objects.get(kinde_user_id=kinde_user_id)
        community_id = Communities.objects.get(id=community)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({'message': 'Invalid student or community ID.'}, status=404)

    try:
        membership = Membership.objects.get(user=student_id, community=community_id)
    except Membership.DoesNotExist:
        return JsonResponse({'message': 'Membership does not exist.'}, status=404)

    # Check if this is the last member in the community
    total_members = Membership.objects.filter(community=community_id).count()
    
    if total_members == 1:
        # This is the last member, delete the entire community
        membership.delete()
        community_id.delete()
        return JsonResponse({'message': 'You were the last member. Community has been deleted.'}, status=200)
    
    if membership.role == 'admin':
        next_admin = Membership.objects.filter(community=community_id, role='secondary_admin').first()
        if next_admin:
            next_admin.role = 'admin'
            next_admin.save()
        else:
            # If no secondary admin exists, promote the first regular member to admin
            next_admin = Membership.objects.filter(community=community_id, role='member').exclude(user=student_id).first()
            if next_admin:
                next_admin.role = 'admin'
                next_admin.save()

    membership.delete()
    return JsonResponse({'message': 'User left the community successfully.'}, status=200)




@api_view(['POST'])
def send_direct_message(request):
    data = request.data
    sender_id = data.get('sender_id')
    receiver_id = data.get('receiver_id')
    message_text = data.get('message')

    if not sender_id or not receiver_id or not message_text:
        return JsonResponse({"status": "error", "message": "Invalid data."})

    sender = Student.objects.get(pk=sender_id)
    receiver = Student.objects.get(pk=receiver_id)

    DirectMessage.objects.create(sender=sender, receiver=receiver, message=message_text)
    return JsonResponse({"status": "success", "message": "Message sent."})

############################################################################################################################


@api_view(['POST'])
def reply_to_message(request):
    data = request.data
    sender_id = data.get('sender_id')
    receiver_id = data.get('receiver_id')
    message_text = data.get('message')

    return send_direct_message({
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "message": message_text,
    })

def find_mentions(content):
    """Find and return mentioned students and communities from content"""
    mentions = re.findall(r'@(\w+)', content)
    
    if not mentions:
        return Student.objects.none(), Communities.objects.none()
    
    # Single query approach - check both models for the same mentions
    mentioned_students = Student.objects.filter(username__in=mentions).only('id', 'username')
    mentioned_communities = Communities.objects.filter(community_tag__in=mentions).only('id', 'community_tag')

    return mentioned_students, mentioned_communities


@csrf_exempt
@kinde_auth_required
@parser_classes([MultiPartParser, FormParser])
async def create_post(request, kinde_user_id=None):
    images = request.FILES.getlist('images')
    videos = request.FILES.getlist('videos')
    data = request.POST
    
    content = data.get('content')

    if not content:
        return JsonResponse({"status": "error", "message": "Content is required."}, status=status.HTTP_400_BAD_REQUEST)

    character_count = len(content)
    if character_count > 2000:
        return JsonResponse({"status": "error", "message": "Post is too long. Keep it under 2000 characters."}, status=status.HTTP_400_BAD_REQUEST)
    
    # File size limits (in bytes)
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
    
    # Validate image sizes
    for image in images:
        if image.size > MAX_IMAGE_SIZE:
            size_mb = image.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Image '{image.name}' is too large ({size_mb:.2f}MB). Maximum size is 10MB. Please reduce the image size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    # Validate video sizes
    for video in videos:
        if video.size > MAX_VIDEO_SIZE:
            size_mb = video.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Video '{video.name}' is too large ({size_mb:.2f}MB). Maximum size is 100MB. Please reduce the video size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        sender_student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Student not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Find mentions before creating post
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(content)
        
        # Define the database operations as a synchronous function
        def create_post_with_media():
            import time
            with transaction.atomic():
                post = Posts.objects.create(context_text=content, student=sender_student)
                
                # Add mentions to the post
                if mentioned_students.exists():
                    post.student_mentions.set(mentioned_students)
                if mentioned_communities.exists():
                    post.community_mentions.set(mentioned_communities)
                
                # Process all media files in order to maintain selection order
                # Combine images and videos, processing them in the order they were received
                # Images are processed first (as they come first in the request), then videos
                # Small delay between batches to ensure different timestamps
                for image in images:
                    PostImages.objects.create(post=post, image=image)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                for video in videos:
                    PostVideos.objects.create(post=post, video=video)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                return post
        
        # Execute the database operations synchronously within async context
        post = await sync_to_async(create_post_with_media)()

        # Re-fetch the post with prefetch_related for the serializer
        updated_post_queryset = Posts.objects.select_related('student').prefetch_related(
            Prefetch('likes'), # Prefetch PostLike objects
            Prefetch('comments'), # Prefetch PostComment objects
            Prefetch('images', queryset=PostImages.objects.only('id', 'created_at', 'image')),
            Prefetch('videos', queryset=PostVideos.objects.only('id', 'created_at', 'video')),
            Prefetch('student_mentions'),
            Prefetch('community_mentions')
        ).annotate(
            like_count=Count('likes', distinct=True),
            comment_count=Count('comments', distinct=True)
        ).filter(pk=post.pk) # Use filter instead of aonly
        
        updated_post = await updated_post_queryset.afirst() # Get the single object after prefetching/annotating
        
        if not updated_post:
            return JsonResponse({"status": "error", "message": "Post not found after creation."}, status=status.HTTP_404_NOT_FOUND)
            
        # Fix: Don't wrap the serializer instantiation in sync_to_async, just the .data access
        serializer = PostSerializer(updated_post, context={'kinde_user_id': kinde_user_id})
        serializer_data = await sync_to_async(lambda: serializer.data)()
        
        return JsonResponse({
            "status": "success", 
            "message": "Post created successfully.", 
            "post": serializer_data
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error", 
            "message": f"Failed to create post or upload images: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@kinde_auth_required
@parser_classes([MultiPartParser, FormParser])
async def create_community_post(request, kinde_user_id=None):
    images = request.FILES.getlist('images')
    videos = request.FILES.getlist('videos')
    data = request.POST
    
    community_id = data.get('community_id')
    content = data.get('content')
    
    if not all([community_id, content]):
        return JsonResponse({"status": "error", "message": "Community ID and content are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    character_count = len(content)
    if character_count > 3000:
        return JsonResponse({"status": "error", "message": "Post is too long. Keep it under 3000 characters."}, status=status.HTTP_400_BAD_REQUEST)
    
    # File size limits (in bytes)
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
    
    # Validate image sizes
    for image in images:
        if image.size > MAX_IMAGE_SIZE:
            size_mb = image.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Image '{image.name}' is too large ({size_mb:.2f}MB). Maximum size is 10MB. Please reduce the image size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    # Validate video sizes
    for video in videos:
        if video.size > MAX_VIDEO_SIZE:
            size_mb = video.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Video '{video.name}' is too large ({size_mb:.2f}MB). Maximum size is 100MB. Please reduce the video size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        sender_student = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community = await Communities.objects.aget(pk=community_id)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({"status": "error", "message": "Invalid student or community ID."}, status=status.HTTP_404_NOT_FOUND)
    
    # Check membership using aexists()
    is_member = await Membership.objects.filter(user=sender_student, community=community).aexists()
    if not is_member:
        return JsonResponse({"status": "error", "message": "You must be a member of the community to post."}, status=status.HTTP_403_FORBIDDEN)
    
    try:
        # Find mentions before creating post
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(content)
        
        # Define the database operations as a synchronous function
        def create_community_post_with_media():
            import time
            with transaction.atomic():
                compost = Community_Posts.objects.create(post_text=content, community=community, poster=sender_student)
                
                # Add mentions to the post
                if mentioned_students.exists():
                    compost.student_mentions.set(mentioned_students)
                if mentioned_communities.exists():
                    compost.community_mentions.set(mentioned_communities)
                
                # Process all media files in order to maintain selection order
                for image in images:
                    Community_Posts_Image.objects.create(community_post=compost, image=image)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                for video in videos:
                    Community_Posts_Video.objects.create(community_post=compost, video=video)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                return compost
        
        # Execute the database operations synchronously within async context
        compost = await sync_to_async(create_community_post_with_media)()
        
        # Re-fetch the post with proper relations for the serializer
        updated_post_queryset = Community_Posts.objects.select_related('poster', 'community').prefetch_related(
            'images',
            'videos',
            'student_mentions',
            'community_mentions'
        ).filter(pk=compost.pk)
        
        updated_post = await updated_post_queryset.afirst()
        
        if not updated_post:
            return JsonResponse({"status": "error", "message": "Post not found after creation."}, status=status.HTTP_404_NOT_FOUND)
        
        # Don't wrap the serializer instantiation in sync_to_async, just the .data access
        serializer = CommunityPostSerializer(updated_post, context={'kinde_user_id': kinde_user_id})
        serializer_data = await sync_to_async(lambda: serializer.data)()
        
        return JsonResponse({
            "status": "success", 
            "message": "Community post created.", 
            "post": serializer_data
        }, status=status.HTTP_201_CREATED)
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error", 
            "message": f"Failed to create post or upload images: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@kinde_auth_required
@parser_classes([MultiPartParser, FormParser])
async def create_community_event(request, kinde_user_id=None):
    images = request.FILES.getlist('images')
    videos = request.FILES.getlist('videos')
    data = request.POST
    
    community_id = data.get('community_id')
    event_name = data.get('event_name')
    description = data.get('description')
    date = data.get('date')
    
    if not all([community_id, event_name, description]):
        return JsonResponse({"status": "error", "message": "Community ID, event name, and description are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    # Collect all validation errors
    validation_errors = []
    
    # Validate event_name length (max 50 characters as per model)
    if len(event_name) > 100:
        validation_errors.append("Event name is too long. Maximum length is 100 characters.")
    
    # Validate description length
    character_count = len(description)
    if character_count > 2000:
        validation_errors.append("Description is too long. Keep it under 2000 characters.")
    
    # Return all validation errors at once if any exist
    if validation_errors:
        error_message = " | ".join(validation_errors) if len(validation_errors) > 1 else validation_errors[0]
        return JsonResponse({"status": "error", "message": error_message}, status=status.HTTP_400_BAD_REQUEST)
    
    # File size limits (in bytes)
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
    
    # Validate image sizes
    for image in images:
        if image.size > MAX_IMAGE_SIZE:
            size_mb = image.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Image '{image.name}' is too large ({size_mb:.2f}MB). Maximum size is 10MB. Please reduce the image size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    # Validate video sizes
    for video in videos:
        if video.size > MAX_VIDEO_SIZE:
            size_mb = video.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Video '{video.name}' is too large ({size_mb:.2f}MB). Maximum size is 100MB. Please reduce the video size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        sender_student = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community = await Communities.objects.aget(pk=community_id)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({"status": "error", "message": "Invalid student or community ID."}, status=status.HTTP_404_NOT_FOUND)
    
    # Check membership using aexists()
    is_member = await Membership.objects.filter(user=sender_student, community=community).aexists()
    if not is_member:
        return JsonResponse({"status": "error", "message": "You must be a member of the community to post."}, status=status.HTTP_403_FORBIDDEN)
    
    try:
        # Find mentions in both event name and description
        combined_content = f"{event_name} {description}"
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(combined_content)
        
        # Define the database operations as a synchronous function
        def create_community_event_with_media():
            import time
            with transaction.atomic():
                comevent = Community_Events.objects.create(
                    event_name=event_name, 
                    description=description, 
                    community=community, 
                    poster=sender_student, 
                    RSVP=0,
                    date=date
                )
                
                # Add mentions to the event
                if mentioned_students.exists():
                    comevent.student_mentions.set(mentioned_students)
                if mentioned_communities.exists():
                    comevent.community_mentions.set(mentioned_communities)
                
                # Process all media files in order to maintain selection order
                for image in images:
                    Community_Events_Image.objects.create(community_event=comevent, image=image)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                for video in videos:
                    Community_Events_Video.objects.create(community_event=comevent, video=video)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                return comevent
        
        # Execute the database operations synchronously within async context
        comevent = await sync_to_async(create_community_event_with_media)()
        
        # Re-fetch the event with proper relations for the serializer
        updated_event_queryset = Community_Events.objects.select_related('poster', 'community').prefetch_related(
            'images',
            'videos',
            'student_mentions',
            'community_mentions'
        ).filter(pk=comevent.pk)
        
        updated_event = await updated_event_queryset.afirst()
        
        if not updated_event:
            return JsonResponse({"status": "error", "message": "Event not found after creation."}, status=status.HTTP_404_NOT_FOUND)
        
        # Don't wrap the serializer instantiation in sync_to_async, just the .data access
        serializer = CommunityEventsSerializer(updated_event, context={'kinde_user_id': kinde_user_id})
        serializer_data = await sync_to_async(lambda: serializer.data)()
        
        return JsonResponse({
            "status": "success", 
            "message": "Community event created.", 
            "event": serializer_data
        }, status=status.HTTP_201_CREATED)
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error", 
            "message": f"Failed to create event or upload images: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@kinde_auth_required
@parser_classes([MultiPartParser, FormParser])
async def post_student_event(request, kinde_user_id=None):
    images = request.FILES.getlist('images')
    videos = request.FILES.getlist('videos')
    data = request.POST
    
    event_name = data.get('event_name')
    description = data.get('description')
    date = data.get('date')
    
    if not all([event_name, description]):
        return JsonResponse({"status": "error", "message": "Event name and description are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    # Collect all validation errors
    validation_errors = []
    
    # Validate event_name length (max 50 characters as per model)
    if len(event_name) > 100:
        validation_errors.append("Event name is too long. Maximum length is 100 characters.")
    
    # Validate description length
    character_count = len(description)
    if character_count > 2000:
        validation_errors.append("Description is too long. Keep it under 2000 characters.")
    
    # Return all validation errors at once if any exist
    if validation_errors:
        error_message = " | ".join(validation_errors) if len(validation_errors) > 1 else validation_errors[0]
        return JsonResponse({"status": "error", "message": error_message}, status=status.HTTP_400_BAD_REQUEST)
    
    # File size limits (in bytes)
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
    
    # Validate image sizes
    for image in images:
        if image.size > MAX_IMAGE_SIZE:
            size_mb = image.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Image '{image.name}' is too large ({size_mb:.2f}MB). Maximum size is 10MB. Please reduce the image size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    # Validate video sizes
    for video in videos:
        if video.size > MAX_VIDEO_SIZE:
            size_mb = video.size / (1024 * 1024)
            return JsonResponse({
                "status": "error", 
                "message": f"Video '{video.name}' is too large ({size_mb:.2f}MB). Maximum size is 100MB. Please reduce the video size and try again."
            }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        sender_student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Student not found."}, status=status.HTTP_404_NOT_FOUND)
    
    try:
        combined_content = f"{event_name} {description}"
        mentioned_students_qs, mentioned_communities_qs = await sync_to_async(find_mentions)(combined_content)
        mentioned_students = list(mentioned_students_qs)
        mentioned_communities = list(mentioned_communities_qs)
        
        def create_student_event_with_media():
            import time
            with transaction.atomic():
                student_event = Student_Events.objects.create(
                    event_name=event_name,
                    description=description,
                    RSVP=0,
                    student=sender_student,
                    date=date
                )
                
                if mentioned_students:
                    student_event.student_mentions.set(mentioned_students)
                if mentioned_communities:
                    student_event.community_mentions.set(mentioned_communities)
                
                # Process all media files in order to maintain selection order
                for image in images:
                    Student_Events_Image.objects.create(student_event=student_event, image=image)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                for video in videos:
                    Student_Events_Video.objects.create(student_event=student_event, video=video)
                    time.sleep(0.001)  # Small delay to ensure unique timestamps
                
                return student_event
        
        student_event = await sync_to_async(create_student_event_with_media)()
        
        updated_event = await Student_Events.objects.filter(pk=student_event.pk).select_related('student').prefetch_related(
            'images',
            'videos',
            'student_mentions',
            'community_mentions',
            Prefetch('eventrsvp', queryset=EventRSVP.objects.select_related('student')),
            Prefetch('bookmarkedstudentevents_set', queryset=BookmarkedStudentEvents.objects.select_related('student')),
            Prefetch('student_events_discussion_set', queryset=Student_Events_Discussion.objects.select_related('student'))
        ).annotate(
            rsvp_count=Count('eventrsvp', distinct=True),
            comment_count=Count('student_events_discussion', distinct=True)
        ).afirst()
        
        if not updated_event:
            return JsonResponse({"status": "error", "message": "Event not found after creation."}, status=status.HTTP_404_NOT_FOUND)
        
        event_payload = build_student_event_payload(updated_event, kinde_user_id=kinde_user_id)
        
        return JsonResponse({
            "status": "success", 
            "message": "Student event created.", 
            "event": event_payload
        }, status=status.HTTP_201_CREATED)
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error", 
            "message": f"Failed to create event or upload images: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
@api_view(['POST'])
@kinde_auth_required
def toggle_like_post(request, kinde_user_id=None):
    """
    Toggles a like on a Post and broadcasts the update to both
    the general feed and the specific post's detail page.
    """
    data = request.data
    post_id = data.get('post_id')

    if not post_id:
        return JsonResponse({"status": "error", "message": "Post ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    if kinde_user_id is None:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
        post = Posts.objects.get(pk=post_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated student not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Posts.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Post not found."}, status=status.HTTP_404_NOT_FOUND)

    like, created = PostLike.objects.get_or_create(student=student, post=post)

    action_message = ""
    status_code = None

    if not created:
        # If exists, user is unliking
        try:
            like.delete()
            action_message = "Post unliked."
            status_code = status.HTTP_200_OK
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f'user_updates_{kinde_user_id}', # Target the specific user's group
                    {
                        'type': 'user.updated', # This matches the consumer's async def user_updated method
                        'data': {
                            'update_type': 'item_removed', # A specific update type for Flutter to handle
                            'content_type': 'liked_post',
                            'item_id': post_id,
                        }
                    }
                )
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"Failed to unlike post: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    else:
        action_message = "Post liked."
        status_code = status.HTTP_201_CREATED

    # --- After like/unlike, fetch the updated post data and broadcast it ---
    # Re-fetch the post WITH ANNOTATIONS to get accurate counts for the serializer
    updated_post = Posts.objects.annotate(
        like_count=Count('likes'), # Ensure 'likes' is the related_name on PostLike
        comment_count=Count('comments') # Ensure 'comments' is the related_name on PostComment
    ).get(pk=post_id) 
    
    # Prepare serializer context (crucial for `is_liked` method in serializer)
    serializer_context = {'kinde_user_id': kinde_user_id} 
    updated_post_data = PostSerializer(updated_post, context=serializer_context).data

    # Use Channel Layer to send updates
    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            # 1. Broadcast to the general feed update group (main feed)
            async_to_sync(channel_layer.group_send)(
                'global_feed_updates', 
                {
                    'type': 'feed.update', 
                    'data': {
                        'update_type': 'post_liked', # Specific for Flutter
                        'content_type': 'post',
                        'item_id': post_id,
                        'item_data': updated_post_data
                    }
                }
            )
            # 2. Broadcast to the specific post's detail page group
            async_to_sync(channel_layer.group_send)(
                f'post_updates_{post_id}', # This targets SinglePostUpdateConsumer
                {
                    'type': 'post_updated', # Matches SinglePostUpdateConsumer's method
                    'post_type': 'posts', # To differentiate (useful if consumer handles multiple types)
                    'post_data': updated_post_data
                }
            )
            
            # 3. Broadcast to the student's posts list group (StudentPostsUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                f'student_posts_updates_{post.student.id}',
                {
                    'type': 'post_updated',
                    'post_data': updated_post_data
                }
            )
            
            # 4. Broadcast to all users' post feeds (PostFeedUpdateConsumer)
            # Note: This will broadcast to all users who have the post in their feed
            # In a production system, you might want to be more selective about who receives updates
            # The global_feed_updates broadcast is already handled above, so this is for specific feed consumers
        except Exception as e:
            print(f"Error broadcasting post update via WebSocket: {e}")
            # You might want to log this error more robustly in production

    return JsonResponse({'status': 'success', 'message': action_message, 'post': updated_post_data}, status=status_code)
@api_view(['POST'])
@kinde_auth_required
def toggle_like_community_post(request, kinde_user_id=None):
    """
    Toggles a like on a Community_Posts and broadcasts the update.
    """
    data = request.data
    community_post_id = data.get('community_post_id')

    if not community_post_id:
        return JsonResponse({"status": "error", "message": "Community post ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    if kinde_user_id is None:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
        community_post = Community_Posts.objects.get(pk=community_post_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Community_Posts.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Community post not found."}, status=status.HTTP_404_NOT_FOUND)

    like, created = LikeCommunityPost.objects.get_or_create(student=student, event=community_post)

    action_message = ""
    status_code = None

    if not created:
        action_message = "Community post unliked."
        status_code = status.HTTP_200_OK
        try:
            like.delete()
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f'user_updates_{kinde_user_id}', # Target the specific user's group
                    {
                        'type': 'user.updated', # This matches the consumer's async def user_updated method
                        'data': {
                            'update_type': 'item_removed',
                            'content_type': 'liked_community_post', # <--- CORRECTED THIS LINE
                            'item_id': community_post_id,
                        }
                    }
                )
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"Failed to unlike post: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    else:
        action_message = "Community post liked."
        status_code = status.HTTP_201_CREATED


    # 5. Broadcast Community Post Update via WebSocket
    updated_community_post = Community_Posts.objects.annotate(
        like_count=Count('likecommunitypost')
    ).get(pk=community_post_id)

    serializer_context = {'kinde_user_id': kinde_user_id} 
    updated_community_post_data = CommunityPostSerializer(updated_community_post, context=serializer_context).data

    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            # 1. Broadcast to the general feed update group (FeedUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                'global_feed_updates', # <--- This is the correct general feed group
                {
                    'type': 'feed.update',
                    'data': {
                        'update_type': 'community_post_liked',
                        'content_type': 'community_post',
                        'item_id': community_post_id,
                        'item_data': updated_community_post_data
                    }
                }
            )

            # 2. Broadcast to the specific community post's detail page group (SingleCommunityPostUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                f'community_post_updates_{community_post_id}',
                {
                    'type': 'community_post_updated',
                    'post_data': updated_community_post_data
                }
            )
            
            # 3. Broadcast to the community's posts list group (CommunityPostsListUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                f'community_posts_list_updates_{community_post.community.id}',
                {
                    'type': 'community_post_updated',
                    'post_data': updated_community_post_data
                }
            )
            
            # 4. Broadcast to all users' post feeds
            broadcast_post_update_to_feeds(updated_community_post_data, 'community_post')
        except Exception as e:
            print(f"Error broadcasting community post update via WebSocket: {e}")

    return JsonResponse({"status": "success", "message": action_message, "post": updated_community_post_data}, status=status_code)




@csrf_exempt
@kinde_auth_required
async def transfer_community_admin(request, kinde_user_id=None):
    """
    Transfer admin role from current admin to a new admin.
    Current admin becomes a regular member.
    Only the current admin can perform this action.
    """
    if request.method != 'POST':
        return JsonResponse({
            'status': 'error',
            'message': 'Only POST method allowed.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    data = json.loads(request.body.decode('utf-8'))
    community_id = data.get('community_id')
    new_admin_id = data.get('new_admin_id')

    if not all([community_id, new_admin_id]):
        return JsonResponse({
            'status': 'error',
            'message': 'Community ID and new admin ID are required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get current admin (the requester)
        current_admin = await Student.objects.aget(kinde_user_id=kinde_user_id)
        
        # Get community
        community = await Communities.objects.aget(pk=community_id)
        
        # Get new admin
        new_admin = await Student.objects.aget(pk=new_admin_id)
        
        # Verify current admin is actually the admin of this community
        current_admin_membership = await Membership.objects.filter(
            user=current_admin,
            community=community,
            role='admin'
        ).afirst()
        
        if not current_admin_membership:
            return JsonResponse({
                'status': 'error',
                'message': 'You must be the community admin to transfer admin role.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Verify new admin is a member of the community
        new_admin_membership = await Membership.objects.filter(
            user=new_admin,
            community=community
        ).afirst()
        
        if not new_admin_membership:
            return JsonResponse({
                'status': 'error',
                'message': 'New admin must be a member of the community.'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Transfer admin role in a transaction
        def transfer_admin_role():
            with transaction.atomic():
                current_admin_membership_sync = Membership.objects.get(pk=current_admin_membership.pk)
                new_admin_membership_sync = Membership.objects.get(pk=new_admin_membership.pk)
                
                current_admin_membership_sync.role = 'secondary_admin'
                current_admin_membership_sync.save()
                
                new_admin_membership_sync.role = 'admin'
                new_admin_membership_sync.save()
        
        await sync_to_async(transfer_admin_role)()
        
        return JsonResponse({
            'status': 'success',
            'message': 'Community admin transferred successfully.'
        }, status=status.HTTP_200_OK)
        
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Student not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Communities.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Community not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Failed to transfer admin role: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@kinde_auth_required
async def promote_to_secondary_admin(request, kinde_user_id=None):
    """
    Promote a community member to secondary admin.
    Only the main admin can perform this action.
    """
    if request.method != 'POST':
        return JsonResponse({
            'status': 'error',
            'message': 'Only POST method allowed.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    data = json.loads(request.body.decode('utf-8'))
    community_id = data.get('community_id')
    member_id = data.get('member_id')

    if not all([community_id, member_id]):
        return JsonResponse({
            'status': 'error',
            'message': 'Community ID and member ID are required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get the requester (must be admin)
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        
        # Get community
        community = await Communities.objects.aget(pk=community_id)
        
        # Get member to promote
        member_to_promote = await Student.objects.aget(pk=member_id)
        
        # Verify requester is the admin of this community
        requester_membership = await Membership.objects.filter(
            user=requester,
            community=community,
            role__in=['admin', 'secondary_admin']
        ).afirst()
        
        if not requester_membership:
            return JsonResponse({
                'status': 'error',
                'message': 'Only the community admin can promote members to secondary admin.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Get member's membership
        member_membership = await Membership.objects.filter(
            user=member_to_promote,
            community=community
        ).afirst()
        
        if not member_membership:
            return JsonResponse({
                'status': 'error',
                'message': 'Member not found in the community.'
            }, status=status.HTTP_404_NOT_FOUND)

        # Check if the user is already a secondary admin or admin
        if member_membership.role in ['admin', 'secondary_admin']:
            return JsonResponse({
                'status': 'error',
                'message': 'User is already an admin or secondary admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Promote the user to secondary admin
        def promote_member():
            member_membership_sync = Membership.objects.get(pk=member_membership.pk)
            member_membership_sync.role = 'secondary_admin'
            member_membership_sync.save()
        
        await sync_to_async(promote_member)()

        return JsonResponse({
            'status': 'success',
            'message': 'User promoted to secondary admin successfully.'
        }, status=status.HTTP_200_OK)
        
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Student not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Communities.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Community not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Failed to promote member: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@kinde_auth_required
async def demote_secondary_admin(request, kinde_user_id=None):
    """
    Demote a secondary admin back to a regular member.
    Only the main admin can perform this action.
    """
    if request.method != 'POST':
        return JsonResponse({
            'status': 'error',
            'message': 'Only POST method allowed.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    data = json.loads(request.body.decode('utf-8'))
    community_id = data.get('community_id')
    secondary_admin_id = data.get('secondary_admin_id')

    if not all([community_id, secondary_admin_id]):
        return JsonResponse({
            'status': 'error',
            'message': 'Community ID and secondary admin ID are required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get the requester (must be main admin)
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        
        # Get community
        community = await Communities.objects.aget(pk=community_id)
        
        # Get secondary admin to demote
        secondary_admin = await Student.objects.aget(pk=secondary_admin_id)
        
        # Verify requester is the main admin of this community
        requester_membership = await Membership.objects.filter(
            user=requester,
            community=community,
            role='admin'
        ).afirst()
        
        if not requester_membership:
            return JsonResponse({
                'status': 'error',
                'message': 'Only the community admin can demote secondary admins.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Get secondary admin's membership
        secondary_admin_membership = await Membership.objects.filter(
            user=secondary_admin,
            community=community
        ).afirst()
        
        if not secondary_admin_membership:
            return JsonResponse({
                'status': 'error',
                'message': 'User not found in the community.'
            }, status=status.HTTP_404_NOT_FOUND)

        # Check if the user is actually a secondary admin
        if secondary_admin_membership.role != 'secondary_admin':
            return JsonResponse({
                'status': 'error',
                'message': 'User is not a secondary admin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Demote the secondary admin to member
        def demote_secondary_admin():
            membership_sync = Membership.objects.get(pk=secondary_admin_membership.pk)
            membership_sync.role = 'member'
            membership_sync.save()
        
        await sync_to_async(demote_secondary_admin)()

        return JsonResponse({
            'status': 'success',
            'message': 'Secondary admin demoted to member successfully.'
        }, status=status.HTTP_200_OK)
        
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Student not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Communities.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Community not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Failed to demote secondary admin: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



    
@csrf_exempt
@kinde_auth_required
async def edit_profile(request, kinde_user_id=None):
    """
    Async view to edit user profile using DRF serializer for validation.
    Handles both form data and JSON requests.
    """
    if request.method != 'POST':
        return JsonResponse({
            'status': 'error', 
            'message': 'Only POST method allowed.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    if not kinde_user_id:
        return JsonResponse({
            'status': 'error', 
            'message': 'User ID not found in authentication data.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Fetch the student object asynchronously
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error', 
            'message': 'Student not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    # Parse request data based on content type
    try:
        if request.content_type and 'multipart/form-data' in request.content_type:
            # Form data request (with potential file upload)
            data = request.POST.dict()
            files = request.FILES.dict()
            
            # Handle comma-separated interests for form data
            if 'student_interest_ids' in data:
                interest_str = data['student_interest_ids']
                if interest_str.strip():
                    data['student_interest_ids'] = [int(x.strip()) for x in interest_str.split(',') if x.strip()]
                else:
                    data['student_interest_ids'] = []
            
            # Add files to data
            data.update(files)
            
        elif request.content_type == 'application/json':
            # JSON request
            data = json.loads(request.body.decode('utf-8')) if request.body else {}
        else:
            # Fallback to POST data
            data = request.POST.dict()
            
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
        return JsonResponse({
            'status': 'error', 
            'message': f'Invalid request data: {str(e)}'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Use the serializer for validation and saving
    try:
        # Create serializer with partial update
        serializer = StudentProfileUpdateSerializer(student, data=data, partial=True)
        
        # Validate the data (run in sync_to_async)
        is_valid = await sync_to_async(serializer.is_valid)()
        
        if is_valid:
            # Use transaction for data integrity - run the whole save operation in sync context
            def save_with_transaction():
                with transaction.atomic():
                    return serializer.save()
            
            updated_student = await sync_to_async(save_with_transaction, thread_sensitive=True)()
                
                # Refresh to get all related data
            await updated_student.arefresh_from_db()
            
            # Serialize the response
            response_serializer_data = await sync_to_async(
                lambda: StudentSerializer(updated_student).data
            )()
            
            # After save, parse bio mentions and update M2M using existing helper
            bio_text = data.get('bio') if 'bio' in data else updated_student.bio
            mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(bio_text or '')
            await sync_to_async(updated_student.student_mentions.set)(mentioned_students)
            await sync_to_async(updated_student.community_mentions.set)(mentioned_communities)

            return JsonResponse({
                'status': 'success',
                'message': 'Profile updated successfully.',
                'student': response_serializer_data
            }, status=status.HTTP_200_OK)
        else:
            # Return validation errors
            errors = await sync_to_async(lambda: serializer.errors)()
            return JsonResponse({
                'status': 'error',
                'message': 'Validation failed.',
                'errors': errors
            }, status=status.HTTP_400_BAD_REQUEST)
            
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Error updating profile: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    


@csrf_exempt
@kinde_auth_required
async def set_username(request, kinde_user_id=None):
    """
    Set username for users that don't have one yet.
    Only allows setting username if the user doesn't already have one.
    """
    if request.method != 'POST':
        return JsonResponse({
            'status': 'error',
            'message': 'Only POST method allowed.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    if not kinde_user_id:
        return JsonResponse({
            'status': 'error',
            'message': 'User ID not found in authentication data.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Fetch the student object asynchronously
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Student not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    # Check if user already has a username
    if student.username:
        return JsonResponse({
            'status': 'error',
            'message': 'Username already set. Cannot change username using this endpoint.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Parse request data
    try:
        if request.content_type == 'application/json':
            data = json.loads(request.body.decode('utf-8')) if request.body else {}
        else:
            data = request.POST.dict()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Invalid request data: {str(e)}'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Get username from request
    username = data.get('username', '').strip()

    # Validate username
    if not username:
        return JsonResponse({
            'status': 'error',
            'message': 'Username is required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Check username length (max 25 characters as per model)
    if len(username) > 25:
        return JsonResponse({
            'status': 'error',
            'message': 'Username must be 25 characters or less.'
        }, status=status.HTTP_400_BAD_REQUEST)

    if len(username) < 1:
        return JsonResponse({
            'status': 'error',
            'message': 'Username cannot be empty.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Check if username already exists
    def check_username_exists(username_to_check):
        return Student.objects.filter(username=username_to_check).exists()
    
    username_exists = await sync_to_async(check_username_exists)(username)
    
    if username_exists:
        return JsonResponse({
            'status': 'error',
            'message': 'Username already in use. Please choose a different username.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Set the username
    try:
        def save_username():
            with transaction.atomic():
                student.username = username
                student.save()
                return student
        
        updated_student = await sync_to_async(save_username, thread_sensitive=True)()
        
        # Refresh to get updated data
        await updated_student.arefresh_from_db()
        
        # Serialize the response
        response_serializer_data = await sync_to_async(
            lambda: StudentSerializer(updated_student).data
        )()
        
        return JsonResponse({
            'status': 'success',
            'message': 'Username set successfully.',
            'student': response_serializer_data
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Error setting username: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@kinde_auth_required
async def edit_community_profile(request, kinde_user_id=None):
    """
    Edit a community's profile (only by admins/secondary_admins).
    Accepts multipart or JSON. Updates basic fields, interests, location,
    and parses @mentions in community_bio to populate student/community mentions.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Only POST method allowed.'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    try:
        auth_student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    # Parse request
    try:
        if request.content_type and 'multipart/form-data' in request.content_type:
            data = request.POST.dict()
            files = request.FILES.dict()
            if 'community_interest_ids' in data:
                interest_str = data['community_interest_ids']
                if interest_str.strip():
                    data['community_interest_ids'] = [int(x.strip()) for x in interest_str.split(',') if x.strip()]
                else:
                    data['community_interest_ids'] = []
            data.update(files)
        elif request.content_type == 'application/json':
            data = json.loads(request.body.decode('utf-8')) if request.body else {}
        else:
            data = request.POST.dict()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
        return JsonResponse({'status': 'error', 'message': f'Invalid request data: {str(e)}'}, status=400)

    community_id = data.get('community_id')
    if not community_id:
        return JsonResponse({'status': 'error', 'message': 'community_id is required.'}, status=400)

    try:
        community = await Communities.objects.aget(id=community_id)
    except Communities.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community not found.'}, status=404)

    # Authorization: admin or secondary_admin
    is_admin = await Membership.objects.filter(user=auth_student, community=community, role__in=['admin', 'secondary_admin']).aexists()
    if not is_admin:
        return JsonResponse({'status': 'error', 'message': 'Permission denied.'}, status=403)

    try:
        def _update():
            with transaction.atomic():
                if 'community_name' in data:
                    community.community_name = data.get('community_name') or community.community_name
                if 'community_bio' in data:
                    community.community_bio = data.get('community_bio')
                if 'description' in data:
                    community.description = data.get('description') or community.description
                if 'community_tag' in data:
                    community.community_tag = data.get('community_tag') or community.community_tag
                if 'community_image' in data and data['community_image']:
                    community.community_image = data['community_image']
                # Location update
                if 'location_id' in data and data['location_id']:
                    try:
                        community.location = Location.objects.get(id=int(data['location_id']))
                    except Exception:
                        pass
                community.save()
                # Interests set
                if 'community_interest_ids' in data:
                    ids = data['community_interest_ids'] if isinstance(data['community_interest_ids'], list) else []
                    community.community_interest.set(Interests.objects.filter(id__in=ids))
                return community

        updated = await sync_to_async(_update, thread_sensitive=True)()

        # Mentions from bio using existing helper
        bio_text = data.get('community_bio') if 'community_bio' in data else updated.community_bio
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(bio_text or '')
        await sync_to_async(updated.student_mentions.set)(mentioned_students)
        await sync_to_async(updated.community_mentions.set)(mentioned_communities)

        return JsonResponse({
            'status': 'success',
            'message': 'Community updated successfully.',
            'community': {
                'id': updated.id,
                'community_name': updated.community_name,
                'community_bio': updated.community_bio,
                'community_tag': updated.community_tag,
            }
        }, status=200)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Error updating community: {str(e)}'}, status=500)


def calculate_trending_score(obj, interaction_count_field='likes', date_field='post_date'):
    # Get interaction count (likes/comments/RSVPs etc.)
    interaction_count = getattr(obj, interaction_count_field).count() if hasattr(getattr(obj, interaction_count_field), 'all') else getattr(obj, interaction_count_field, 0)

    # Time since creation
    created_time = getattr(obj, date_field)
    if isinstance(created_time, datetime):
        hours_since_post = (datetime.now() - created_time).total_seconds() / 3600
    else:
        # If it's a DateField, use days and approximate
        hours_since_post = (datetime.now().date() - created_time).days * 24

    hours_since_post = max(hours_since_post, 1)  # Prevent division by 0

    # Trending score
    score = interaction_count / math.pow(hours_since_post, 1.5)
    return score



def calculate_trending_score(obj, time_field='date', interaction_count_field=None):
    """
    Calculates a trending score based on likes/comments/RSVPs over time.
    You can optionally specify a custom interaction field like 'RSVP'.
    """
    now = timezone.now()
    created_time = getattr(obj, time_field, None)

    if created_time is None:
        return 0

    # Normalize datetime (handle naive datetimes)
    if timezone.is_naive(created_time):
        created_time = timezone.make_aware(created_time)

    time_diff = now - created_time
    hours_since = time_diff.total_seconds() / 3600
    hours_since = max(hours_since, 1)

    # Custom field (like RSVP for events)
    if interaction_count_field and hasattr(obj, interaction_count_field):
        interaction_count = getattr(obj, interaction_count_field)
    else:
        # Fallback to generic interaction: likes + comments
        likes = getattr(obj, 'likes', []).count() if hasattr(obj, 'likes') else 0
        comments = getattr(obj, 'comments', []).count() if hasattr(obj, 'comments') else 0
        interaction_count = likes + comments

    return interaction_count / math.pow(hours_since, 1.5)

#block
@api_view(['GET'])
@kinde_auth_required
def get_post_comments(request, kinde_user_id=None):
    post_id = request.query_params.get('post_id')

    
    try:
        post = Posts.objects.get(pk=post_id)
    except Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Post not found.'}, status=404)

    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)
    
    # Get student IDs with pending deletion requests
    pending_deletion_ids = _get_pending_deletion_student_ids()
    
    comments_qs = PostComment.objects.filter(post=post, parent__isnull=True).select_related('student').prefetch_related('replies__student', 'student_mentions', 'community_mentions').exclude(student__id__in=pending_deletion_ids).order_by('-commented_at')
    
    total_count = comments_qs.count()
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    paginated_comments = list(comments_qs[offset:offset + limit])
    serializer = PostCommentSerializer(paginated_comments, many=True, context={'kinde_user_id': kinde_user_id})
    
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': serializer.data,
        'count': len(serializer.data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)
@api_view(['GET'])
@kinde_auth_required
def get_communities_of_student(request, kinde_user_id=None):
    """
    Optimized version with kinde_auth_required
    """
    student_id = request.query_params.get('student_id')
    if not student_id:
        return JsonResponse({'status': 'error', 'message': 'student_id is required.'}, status=400)

    try:
        student = Student.objects.get(pk=student_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    memberships_qs = Membership.objects.filter(user=student).select_related(
        'community', 'community__location'
    ).prefetch_related('community__community_interest').order_by('-date_joined', '-community__id')

    total_count = memberships_qs.count()

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    page_memberships = list(memberships_qs[offset:offset + limit])

    if not page_memberships:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': total_count,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    community_ids = [membership.community_id for membership in page_memberships]

    try:
        auth_student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    friends = Student.objects.filter(
        Q(sent_requests__receiver=auth_student, sent_requests__status='accepted') |
        Q(received_requests__sender=auth_student, received_requests__status='accepted')
    ).distinct()

    if community_ids:
        # Get requesting user's memberships (for is_member check)
        user_memberships = set(
            Membership.objects.filter(
                user=auth_student,
                community_id__in=community_ids
            ).values_list('community_id', flat=True)
        )

        # Get TARGET student's community roles (the student whose communities we're fetching)
        # This is what should be returned in the response
        target_student_community_roles = dict(
            Membership.objects.filter(
                user=student,  # Target student, not auth_student
                community_id__in=community_ids
            ).values_list('community_id', 'role')
        )
        
        # Also get requesting user's roles for other context (if needed)
        

        relationship_snapshot = get_relationship_snapshot(auth_student.id)
        all_muted_communities = set(relationship_snapshot.get('muted_communities', []))
        all_blocked_by_communities = set(relationship_snapshot.get('blocked_by_communities', []))
        community_id_set = set(community_ids)

        user_muted_communities = all_muted_communities & community_id_set
        user_blocked_by_communities = all_blocked_by_communities & community_id_set

        all_memberships = Membership.objects.filter(
            community_id__in=community_ids
        ).select_related('user', 'community')
    else:
        user_memberships = set()
        target_student_community_roles = {}  # Target student's roles
      # Requesting user's roles (for other context if needed)
        user_muted_communities = set()
        user_blocked_by_communities = set()
        all_memberships = Membership.objects.none()

    friends_by_id = {friend.id: friend for friend in friends}
    membership_map = {}
    for membership in all_memberships:
        membership_map.setdefault(membership.community_id, []).append(membership)

    friends_in_community_data = {}
    for membership in page_memberships:
        community = membership.community
        community_memberships = membership_map.get(community.id, [])
        community_friends = []
        first_friend_added = False

        for community_membership in community_memberships:
            if community_membership.user_id in friends_by_id:
                friend = friends_by_id[community_membership.user_id]
                if not first_friend_added:
                        friend_data = {
                            'id': friend.id,
                            'kinde_user_id': friend.kinde_user_id,
                            'name': friend.name,
                            'username': getattr(friend, 'username', ''),
                            'bio': getattr(friend, 'bio', ''),
                            'profile_image': friend.profile_image.url if friend.profile_image else None,
                            'is_online': getattr(friend, 'is_online', False),
                        'role': community_membership.role,
                        'joined_at': community_membership.created_at.isoformat() if hasattr(community_membership, 'created_at') else None
                        }
                        community_friends.append(friend_data)
                        first_friend_added = True
                else:
                    community_friends.append({'id': friend.id})
        
        friends_in_community_data[community.id] = community_friends

    serializer_context = {
        'request': request,
        'kinde_user_id': kinde_user_id,
        'user_memberships': user_memberships,
        'user_community_roles': target_student_community_roles,  # Use target student's roles, not requesting user's
        'user_muted_communities': user_muted_communities,
        'user_blocked_by_communities': user_blocked_by_communities,
        'friends_in_community': friends_in_community_data
    }
    
    communities = [membership.community for membership in page_memberships]
    serialized = CommunitySerializer(communities, many=True, context=serializer_context).data

    results = []
    for membership, community_data in zip(page_memberships, serialized):
        entry = dict(community_data)
        entry['role'] = membership.role
        entry['kind'] = 'community'
        entry['date_joined'] = membership.date_joined.isoformat() if hasattr(membership, 'date_joined') and membership.date_joined else None
        results.append(entry)

    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': results,
        'count': len(results),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)
#new view, get admins of community
@csrf_exempt
@kinde_auth_required
async def get_friends_of_student(request, kinde_user_id=None):
    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    friends_ids_queryset = Friendship.objects.filter(
        Q(sender=me, status='accepted') | Q(receiver=me, status='accepted')
    ).annotate(
        friend_id=Case(
            When(sender=me, then=F('receiver_id')),
            default=F('sender_id'),
            output_field=IntegerField()
        )
    ).exclude(
        Q(sender__id__in=pending_deletion_ids) | Q(receiver__id__in=pending_deletion_ids)
    ).values('friend_id')

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    friends_queryset = Student.objects.filter(
        pk__in=Subquery(friends_ids_queryset),
        is_verified=True
    ).exclude(id__in=pending_deletion_ids).select_related('university', 'student_location').prefetch_related('student_interest').order_by('name', 'id')

    total_count = await sync_to_async(friends_queryset.count)()

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=status.HTTP_200_OK)

    friends_page = await sync_to_async(list)(friends_queryset[offset:offset + limit])

    def _serialize(data):
        return StudentNameSerializer(data, many=True, context={'request': request}).data

    serialized = await sync_to_async(_serialize)(friends_page)

    results = []
    for item in serialized:
        row = dict(item)
        row.setdefault('kind', 'student')
        results.append(row)

    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': results,
        'count': len(results),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=status.HTTP_200_OK)





#block







@api_view(['GET'])
@kinde_auth_required
def get_friend_request_notifications(request, kinde_user_id=None):
    _prune_expired_notifications()

    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    # Friend request notifications are identified by content containing "friend request"
    # They can be:
    # 1. "You have a new friend request from {sender_name}" - pending requests
    # 2. "{receiver_name} accepted your friend request" - accepted requests
    from django.db.models import Q
    friend_request_notifications = _notification_list_queryset(
        Notification.objects.filter(recipient=student).filter(
            Q(content__icontains='friend request')
        )
    ).order_by('-created_at')

    serializer = NotificationSerializer(friend_request_notifications, many=True)
    return JsonResponse(serializer.data, status=200, safe=False)


@csrf_exempt
@kinde_auth_required
async def get_community_events_notifications(request, kinde_user_id=None):
    """
    Returns all notifications concerning community events.
    This includes notifications where community_event or community_event_discussion is not null,
    covering RSVPs, mentions, discussions, and other community event-related activity.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    await sync_to_async(_prune_expired_notifications)()

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    notifications_qs = (
        Notification.objects.filter(recipient=student)
        .filter(Q(community_event__isnull=False) | Q(community_event_discussion__isnull=False))
        .select_related(
            'sender',
            'notificationtype',
            'community_event',
            'community_event_discussion',
            'community_event__community',
            'community_event__poster',
            'community_event_discussion__student',
            'community_event_discussion__community_event',
            'community_event_discussion__community_event__community'
        )
        .prefetch_related(
            'community_event__images',
            'community_event__videos',
            'community_event__student_mentions',
            'community_event__community_mentions'
        )
        .order_by('-created_at')
    )

    total_count = await sync_to_async(notifications_qs.count)()

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    paginated_notifications = await sync_to_async(list)(notifications_qs[offset:offset + limit])

    def _serialize_notifications(data):
        return NotificationSerializer(data, many=True).data

    serializer_data = await sync_to_async(_serialize_notifications)(paginated_notifications)

    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': serializer_data,
        'count': len(serializer_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

@csrf_exempt
@kinde_auth_required
async def get_student_events_notifications(request, kinde_user_id=None):
    """
    Returns all notifications concerning student events.
    This includes notifications where student_event or student_event_discussion is not null,
    covering RSVPs, mentions, discussions, and other student event-related activity.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    await sync_to_async(_prune_expired_notifications)()

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    notifications_qs = (
        Notification.objects.filter(recipient=student)
        .filter(Q(student_event__isnull=False) | Q(student_event_discussion__isnull=False))
        .select_related(
            'sender',
            'notificationtype',
            'student_event',
            'student_event_discussion',
            'student_event__student',
            'student_event_discussion__student',
            'student_event_discussion__student_event',
            'student_event_discussion__student_event__student'
        )
        .prefetch_related(
            'student_event__images',
            'student_event__videos',
            'student_event__student_mentions',
            'student_event__community_mentions'
        )
        .order_by('-created_at')
    )

    total_count = await sync_to_async(notifications_qs.count)()

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    paginated_notifications = await sync_to_async(list)(notifications_qs[offset:offset + limit])

    def _serialize_notifications(data):
        return NotificationSerializer(data, many=True).data

    serializer_data = await sync_to_async(_serialize_notifications)(paginated_notifications)

    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': serializer_data,
        'count': len(serializer_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

@api_view(['GET'])
def get_sfy_notifications(request):
    student_id = request.query_params.get('student_id')

    try:
        student = Student.objects.get(pk=student_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)
    


    friend_request_notification = _notification_list_queryset(
        Notification.objects.filter(recipient=student, notificationtype=4)
    ).order_by('-created_at')
    serializer = NotificationSerializer(friend_request_notification, many=True)
    return JsonResponse(serializer.data, status=200, safe=False)

@csrf_exempt
@kinde_auth_required
async def get_post_notifications(request, kinde_user_id=None):
    """
    Returns all notifications concerning student posts.
    This includes notifications where post or post_comment is not null,
    covering likes, comments, mentions, and other post-related activity.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    await sync_to_async(_prune_expired_notifications)()

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    notifications_qs = (
        Notification.objects.filter(recipient=student)
        .filter(Q(post__isnull=False) | Q(post_comment__isnull=False))
        .select_related(
            'sender',
            'notificationtype',
            'post',
            'post_comment',
            'post__student',
            'post_comment__post',
            'post_comment__student',
            'post_comment__post__student'
        )
        .prefetch_related(
            'post__images',
            'post__student_mentions',
            'post__community_mentions',
            'post_comment__post__images'
        )
        .order_by('-created_at')
    )
    
    total_count = await sync_to_async(notifications_qs.count)()
    paginated_notifications = await sync_to_async(list)(notifications_qs[offset:offset + limit])

    def _serialize_notifications(data):
        return NotificationSerializer(data, many=True).data

    serializer_data = await sync_to_async(_serialize_notifications)(paginated_notifications)

    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': serializer_data,
        'count': len(serializer_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

@csrf_exempt
@kinde_auth_required
async def get_community_notifications(request, kinde_user_id=None):
    """
    Returns all notifications concerning community posts.
    This includes notifications where community_post or community_post_comment is not null,
    covering likes, comments, mentions, and other community post-related activity.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    await sync_to_async(_prune_expired_notifications)()

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    notifications_qs = (
        Notification.objects.filter(recipient=student)
        .filter(Q(community_post__isnull=False) | Q(community_post_comment__isnull=False))
        .select_related(
            'sender',
            'notificationtype',
            'community_post',
            'community_post_comment',
            'community_post__community',
            'community_post__poster',
            'community_post_comment__community_post',
            'community_post_comment__student',
            'community_post_comment__community_post__community',
            'community_post_comment__community_post__poster'
        )
        .prefetch_related(
            'community_post__community__community_interest',
            'community_post_comment__community_post__community__community_interest'
        )
        .order_by('-created_at')
    )
    
    total_count = await sync_to_async(notifications_qs.count)()
    paginated_notifications = await sync_to_async(list)(notifications_qs[offset:offset + limit])

    def _serialize_notifications(data):
        return NotificationSerializer(data, many=True).data

    serializer_data = await sync_to_async(_serialize_notifications)(paginated_notifications)

    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': serializer_data,
        'count': len(serializer_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)


@csrf_exempt
@kinde_auth_required
async def get_other_notifications(request, kinde_user_id=None):
    """
    Returns all notifications that are NOT friend requests (type 1), 
    community events (type 2), or student events (type 3).
    This includes post likes, comments, mentions, and other activity notifications.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    await sync_to_async(_prune_expired_notifications)()

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    notifications_qs = _notification_list_queryset(
        Notification.objects.filter(recipient=student).exclude(
            notificationtype__id__in=[1, 2, 3]
        )
    ).order_by('-created_at')

    total_count = await sync_to_async(notifications_qs.count)()
    paginated_notifications = await sync_to_async(list)(notifications_qs[offset:offset + limit])

    def _serialize_notifications(data):
        return NotificationSerializer(data, many=True).data

    serializer_data = await sync_to_async(_serialize_notifications)(paginated_notifications)

    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': serializer_data,
        'count': len(serializer_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)



def generate_referral_code():
    """Generate a unique referral code (e.g. 8 alphanumeric)."""
    for _ in range(20):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if not Student.objects.filter(referral_code=code).exists():
            return code
    raise ValueError("Could not generate unique referral code")


def extract_access_token(auth_header):
    """
    Extracts the access token from a compound Authorization header.
    Expected format: "IDBearer <id_token>; AccessBearer <access_token>"
    """
    if not auth_header:
        return None

    # Split by semicolon, then identify the AccessBearer section
    parts = auth_header.split(";")
    for part in parts:
        part = part.strip()
        if part.startswith("AccessBearer "):
            return part.split("AccessBearer ")[1].strip()
    return None

@csrf_exempt
def authenticate_user(request):
    """
    Step 1: Verify token using `verify_kinde_token()`.
    Step 2: If token is valid, fetch user info from Kinde.
    Step 3: If user does not exist in our DB, create an account.
    """
    auth_header = request.headers.get("Authorization", "")

    access_token = extract_access_token(auth_header)
    if not access_token:
        logger.warning(
            "auth_failure category=no_token path=%s method=%s detail=%s",
            request.path,
            request.method,
            "No access token provided to authenticate_user",
        )
        return JsonResponse({"error": "No access token provided"}, status=401)

    

    # Step 1: Verify token
    token_data = verify_kinde_token(access_token)

    if "error" in token_data:
        err_msg = token_data.get("error", "")
        category = _classify_token_error_message(err_msg)
        token_hash_prefix = hashlib.sha256(access_token.encode()).hexdigest()[:8]
        logger.info(
            "auth_failure category=%s path=%s method=%s hash_prefix=%s error=%s",
            category,
            request.path,
            request.method,
            token_hash_prefix,
            err_msg,
        )
        return JsonResponse(token_data, status=401)  # Return error response from verification

    # Step 2: Fetch user info from Kinde
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(KINDE_USERINFO_URL, headers=headers)

    if response.status_code != 200:
        return JsonResponse({"error": f"Kinde response failed: {response.status_code}"}, status=401)

    # Step 3: Extract user info and store
    user_data = response.json()

    def _blank_if_none(val):
        """Sign in with Apple can hide name/email; Kinde may return None or 'None'. Use blank instead."""
        if val is None:
            return ""
        s = str(val).strip()
        return "" if s == "None" else s

    kinde_user_id = user_data.get("sub")  # Unique Kinde user ID
    email = _blank_if_none(user_data.get("email"))
    raw_name = user_data.get("name")
    if raw_name is not None:
        raw_name = str(raw_name).strip()
    else:
        given = _blank_if_none(user_data.get("given_name"))
        family = _blank_if_none(user_data.get("family_name"))
        raw_name = f"{given} {family}".strip()
    name = _blank_if_none(raw_name if raw_name else None)

    # Ensure user exists in our DB
    student, created = Student.objects.get_or_create(
        email=email,
        defaults={
            "email": email,
            "name": name,
            "kinde_user_id": kinde_user_id,
            
        }
    )

    # Silent ban check: no frontend change; banned users get 403 and app treats as access denied
    now = timezone.now()
    if BannedStudents.objects.filter(student=student).filter(
        Q(banned_until__isnull=True) | Q(banned_until__gt=now)
    ).exists():
        token_hash_prefix = hashlib.sha256(access_token.encode()).hexdigest()[:8]
        logger.info(
            "auth_forbidden category=banned path=%s method=%s sub=%s hash_prefix=%s",
            request.path,
            request.method,
            kinde_user_id,
            token_hash_prefix,
        )
        return JsonResponse({"error": "Access denied."}, status=403)

    # Parse refresh_token from body for single active session and storage
    refresh_token = ''
    try:
        if request.body and request.content_type == 'application/json':
            data = json.loads(request.body.decode('utf-8'))
            refresh_token = (data.get('refresh_token') or '').strip()
    except Exception:
        pass

    # Single active session: if this is a new sign-in (new tokens), revoke previous session
    revoke_previous_session_if_new_signin(kinde_user_id, student, refresh_token, new_access_token=access_token)

    # Cache access token so we can revoke it on ban for immediate kick-off
    cache_access_token_for_revoke(kinde_user_id, access_token)

    # Store Kinde refresh_token if client sent it (so we can revoke on ban and for single active session)
    if refresh_token:
        KindeRefreshToken.objects.update_or_create(
            student=student,
            defaults={'refresh_token': refresh_token},
        )

    # Store session data
    request.session["user_id"] = student.id
    request.session["kinde_id"] = kinde_user_id
    request.session["email"] = email
    request.session["name"] = name

    return JsonResponse({
        "message": "User authenticated successfully",
        "status_code": response.status_code,
        "data": {
            "user": {
                "id": student.id,
                "email": student.email,
                "name": student.name,
                "kinde_user_id": student.kinde_user_id,
            
                "created": created  # True if a new user was added
            }
        }
    }, status=200)

@api_view(['POST'])
@kinde_auth_required
def request_student_email_verification(request, kinde_user_id=None):
    # Extract student email from request data
    data = request.data
    student_email = data.get('student_email')
    country_code = data.get('country_code')

    # Validate email domain against the selected country's allowed domains
    if country_code:
        try:
            country = Country.objects.get(code=country_code)
            allowed = country.allowed_email_domains
            if allowed and not any(student_email.lower().endswith(d) for d in allowed):
                domains_str = ", ".join(allowed)
                return JsonResponse({"error": f"Email must end with one of: {domains_str}"}, status=400)
        except Country.DoesNotExist:
            return JsonResponse({"error": "Invalid country code."}, status=400)

    if Student.objects.filter(student_email=student_email, is_verified=True).exists():
        return JsonResponse({"error": "Student email is already in use."}, status=400)

    student, _ = Student.objects.get_or_create(kinde_user_id=kinde_user_id)

    # Save country on student if provided
    if country_code and not student.country_id:
        try:
            student.country = Country.objects.get(code=country_code)
            student.save(update_fields=['country'])
        except Country.DoesNotExist:
            pass

    # Generate a 6-digit OTP
    otp = str(random.randint(100000, 999999))

    EmailVerification.objects.update_or_create(
        student=student,
        defaults={
            'email': student_email,
            'otp': otp,
            'is_verified': False
        }
    )

    # Send email asynchronously using Celery
    send_verification_email.delay(student_email, otp)

    return JsonResponse({"message": "OTP sent to your student email."}, status=200)

@api_view(['POST'])
@kinde_auth_required
def verify_student_email_otp(request, kinde_user_id=None):
    data = request.data
    input_otp = data.get('otp')

    if not input_otp:
        return JsonResponse({"error": "OTP is required."}, status=400)

    

    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
        record = EmailVerification.objects.get(student=student)
    except (Student.DoesNotExist, EmailVerification.DoesNotExist):
        return JsonResponse({"error": "Verification record not found."}, status=404)

    if record.otp == input_otp:
        # Check if another verified student already has this email
        existing_verified = Student.objects.filter(
            student_email=record.email,
            is_verified=True
        ).exclude(id=student.id).first()
        
        if existing_verified:
            return JsonResponse({"error": "This email is already verified by another student."}, status=400)
        
        record.is_verified = True
        record.save()

        student.is_verified = True
        student.student_email = record.email
        student.save()

        # Give this user a referral code now that they are verified
        if not student.referral_code:
            student.referral_code = generate_referral_code()
            student.save(update_fields=['referral_code'])

        send_welcome_email.delay(student.email)

        return JsonResponse({"message": "Student email verified successfully."}, status=200)
    else:
        return JsonResponse({"error": "Invalid OTP."}, status=401)
    
@api_view(['GET'])
def check_verification_status(request):
    auth_header = request.headers.get("Authorization", "")
    
    access_token = ""
    if "AccessBearer" in auth_header:
        access_token = auth_header.split("AccessBearer")[1].strip()
    
    if not access_token:
        logger.warning(
            "auth_failure category=no_token path=%s method=%s detail=%s",
            request.path,
            request.method,
            "No access token provided to check_verification_status",
        )
        return JsonResponse({"error": "No access token provided"}, status=401)
    
    # Pass the extracted token, not the full header
    token_data = verify_kinde_token(access_token)  # ← Changed this line
    
    if "error" in token_data:
        err_msg = token_data.get("error", "")
        category = _classify_token_error_message(err_msg)
        token_hash_prefix = hashlib.sha256(access_token.encode()).hexdigest()[:8]
        logger.info(
            "auth_failure category=%s path=%s method=%s hash_prefix=%s error=%s",
            category,
            request.path,
            request.method,
            token_hash_prefix,
            err_msg,
        )
        return JsonResponse(token_data, status=401)

    # Step 2: Fetch user info from Kinde
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(KINDE_USERINFO_URL, headers=headers)

    if response.status_code != 200:
        return JsonResponse({"error": f"Kinde response failed: {response.status_code}"}, status=401)

    # Step 3: Extract user info and store
    user_data = response.json()

    kinde_user_id = user_data.get("sub")

    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
        if student.is_verified:
            return JsonResponse({"verified": True})
        else:
            return JsonResponse({"verified": False, "message": "Student is not verified."})
    except Student.DoesNotExist:
        return JsonResponse({"error": "Student not found."}, status=404)


# --- Raffle referral API ---

@api_view(['GET'])
@kinde_auth_required
def referral_me(request, kinde_user_id=None):
    """Return current user's referral code and verified referrals count (for sharing / raffle entries)."""
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({"error": "Student not found."}, status=404)

    # Total raffle entries for this user: entries as referrer + entries as referred
    entries_count = VerifiedReferral.objects.filter(
        Q(referrer=student) | Q(referred=student)
    ).count()
    # Build shareable link using the configured base URL
    base_url = getattr(settings, "APP_BASE_URL", "http://www.teamstudico.com").rstrip("/")
    referral_link = f"{base_url}/download/"
    return JsonResponse({
        "referral_code": student.referral_code,
        "verified_referrals_count": entries_count,
        "referral_code_used": student.referral_code_used,
        "referral_link": referral_link,
    })


@api_view(['POST'])
@kinde_auth_required
def referral_submit(request, kinde_user_id=None):
    """
    Attach a referral code for a VERIFIED student.
    Frontend calls this after the user has verified their student email and
    enters who referred them. This both stores the code and credits the referrer.
    """
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({"error": "Student not found."}, status=404)

    if not student.is_verified:
        return JsonResponse(
            {"error": "You must verify your student email before claiming a referral"},
            status=400,
        )

    if student.referral_code_used:
        return JsonResponse(
            {"error": "Referral already claimed"},
            status=400,
        )

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}

    code = (data.get("referral_code") or "").strip()
    if not code:
        return JsonResponse({"error": "Referral code required"}, status=400)

    # Prevent self-referral explicitly
    if student.referral_code and student.referral_code == code:
        return JsonResponse({"error": "Cannot refer yourself"}, status=400)

    referrer = (
        Student.objects.filter(referral_code=code, is_verified=True)
        .exclude(id=student.id)
        .first()
    )
    if not referrer:
        return JsonResponse({"error": "Invalid referral code"}, status=400)

    # Store the code and credit the referrer now that the user is verified
    student.referral_code_used = code
    student.save(update_fields=["referral_code_used"])

    _, created = VerifiedReferral.objects.get_or_create(
        referrer=referrer,
        referred=student,
        defaults={},
    )
    if created:
        Student.objects.filter(pk=referrer.pk).update(
            verified_referrals_count=F("verified_referrals_count") + 1,
        )

    return JsonResponse(
        {
            "ok": True,
            "referrer": {
                "id": referrer.id,
                "name": referrer.name,
                "username": getattr(referrer, "username", ""),
            },
        }
    )


@api_view(['GET'])
def referral_validate(request):
    """Check if a referral code is valid (exists and belongs to a verified user). Public or auth'd."""
    code = (request.GET.get('code') or '').strip()
    if not code:
        return JsonResponse({"valid": False})
    valid = Student.objects.filter(referral_code=code, is_verified=True).exists()
    return JsonResponse({"valid": valid})


@api_view(['GET'])
@kinde_auth_required
def raffle_status(request, kinde_user_id=None):
    """Return current raffle campaign (if any), user's verified referral count (entries), and winner if drawn."""
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({"error": "Student not found."}, status=404)
    now = timezone.now()
    campaign = Raffle.objects.filter(starts_at__lte=now, ends_at__gte=now).order_by('-ends_at').first()
    # Entries for this user (referrer + referred)
    entries_count = VerifiedReferral.objects.filter(
        Q(referrer=student) | Q(referred=student)
    ).count()

    payload = {
        "verified_referrals_count": entries_count,
        "campaign": None,
        "winner": None,
        "rules_text": "Each verified referral = 1 raffle entry. More referrals = higher chance to win.",
    }
    if campaign:
        payload["campaign"] = {
            "id": campaign.id,
            "name": campaign.name,
            "starts_at": campaign.starts_at.isoformat() if campaign.starts_at else None,
            "ends_at": campaign.ends_at.isoformat() if campaign.ends_at else None,
            "drawn_at": campaign.drawn_at.isoformat() if campaign.drawn_at else None,
        }
        if campaign.drawn_at and campaign.winner_id:
            payload["winner"] = {
                "id": campaign.winner_id,
                "is_you": campaign.winner_id == student.id,
                "name": campaign.winner.name if campaign.winner else None,
            }
    return JsonResponse(payload)


@api_view(['POST'])
def raffle_draw(request):
    """Admin-only: run weighted draw for current/designated raffle.
    Each verified referral gives one entry to the referrer and one entry to
    the referred user (both get a chance to win).
    """
    import os
    secret = os.environ.get('RAFFLE_DRAW_SECRET')
    secret_ok = secret and request.headers.get('X-Raffle-Draw-Secret') == secret
    staff_ok = getattr(request.user, 'is_authenticated', False) and getattr(request.user, 'is_staff', False)
    if not (secret_ok or staff_ok):
        return JsonResponse({"error": "Forbidden."}, status=403)
    raffle_id = request.data.get('raffle_id') if request.content_type == 'application/json' else request.POST.get('raffle_id')
    if raffle_id:
        try:
            campaign = Raffle.objects.get(pk=raffle_id)
        except Raffle.DoesNotExist:
            return JsonResponse({"error": "Raffle not found."}, status=404)
    else:
        now = timezone.now()
        campaign = Raffle.objects.filter(starts_at__lte=now, ends_at__gte=now).order_by('-ends_at').first()
        if not campaign:
            return JsonResponse({"error": "No active raffle found."}, status=404)
    if campaign.drawn_at:
        return JsonResponse({"error": "Raffle already drawn.", "winner_id": campaign.winner_id}, status=400)
    # Weighted entries: for each VerifiedReferral in the campaign window,
    # give 1 entry to the referrer and 1 entry to the referred user.
    from collections import Counter

    qs = VerifiedReferral.objects.filter(
        created_at__gte=campaign.starts_at,
        created_at__lte=campaign.ends_at,
    ).values_list("referrer_id", "referred_id")

    counter = Counter()
    for referrer_id, referred_id in qs:
        if referrer_id:
            counter[referrer_id] += 1
        if referred_id:
            counter[referred_id] += 1

    entries = [{"student_id": sid, "entries": count} for sid, count in counter.items()]

    if not entries:
        return JsonResponse({"error": "No verified referrals in campaign window."}, status=400)

    total = sum(e["entries"] for e in entries)
    r = random.uniform(0, total)
    for e in entries:
        r -= e["entries"]
        if r <= 0:
            winner_id = e["student_id"]
            break
    else:
        winner_id = entries[-1]["student_id"]
    winner = Student.objects.get(pk=winner_id)
    campaign.winner = winner
    campaign.drawn_at = timezone.now()
    campaign.save(update_fields=['winner_id', 'drawn_at'])
    return JsonResponse({
        "message": "Raffle drawn.",
        "winner_id": winner.id,
        "winner_name": winner.name,
    })


#add posts and community posts
@api_view(['GET'])
@kinde_auth_required
def global_search(request, kinde_user_id=None):
    query = (request.GET.get('q') or '').strip()
    if not query:
        return JsonResponse({"error": "No search query provided."}, status=400)

    try:
        similarity_threshold = float(request.GET.get('similarity', 0.2))
    except (TypeError, ValueError):
        similarity_threshold = 0.2
    similarity_threshold = max(0.0, min(similarity_threshold, 1.0))

    try:
        popularity_weight = float(request.GET.get('popularity_weight', 0.25))
    except (TypeError, ValueError):
        popularity_weight = 0.25
    popularity_weight = max(0.0, min(popularity_weight, 5.0))
    popularity_weight_value = Value(popularity_weight, output_field=FloatField())
    zero_float = Value(0.0, output_field=FloatField())

    limit, offset = _parse_pagination_params(request, default_limit=10, max_limit=50)
    
    try:
        searcher_student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return Response(
            {'status': 'error', 'message': 'Authenticated student profile not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    relationship_snapshot = get_relationship_snapshot(searcher_student.id)
    blocked_student_ids = set(relationship_snapshot.get('blocked_by', []))
    blocked_community_ids = set(relationship_snapshot.get('blocked_by_communities', []))
    user_muted_student_ids = set(relationship_snapshot.get('muted_students', []))
    user_blocked_student_ids = set(relationship_snapshot.get('blocking', []))

    friend_snapshot = get_friend_snapshot(searcher_student.id)
    friend_ids = set(friend_snapshot.get('ids', []))
    friend_details_map = {entry['id']: entry for entry in friend_snapshot.get('details', [])}

    membership_rows = list(
        Membership.objects.filter(user=searcher_student).values_list('community_id', 'role')
    )
    user_memberships = {community_id for community_id, _ in membership_rows}
    user_community_roles = {community_id: role for community_id, role in membership_rows}

    students_base_qs = (
        Student.objects.annotate(
            name_similarity=TrigramSimilarity('name', query),
            username_similarity=TrigramSimilarity('username', query),
            bio_similarity=TrigramSimilarity('bio', query),
        )
        .annotate(
            combined_similarity=ExpressionWrapper(
                Coalesce(F('name_similarity'), zero_float) +
                Coalesce(F('username_similarity'), zero_float) +
                Coalesce(F('bio_similarity'), zero_float),
                output_field=FloatField(),
            )
        )
        .filter(
            Q(name__icontains=query) |
            Q(username__icontains=query) |
            Q(name_similarity__gt=similarity_threshold) |
            Q(username_similarity__gt=similarity_threshold) |
            Q(bio_similarity__gt=0.3)
        )
        .exclude(id__in=blocked_student_ids)
        .exclude(id=searcher_student.id)
        .filter(is_verified=True)
    )
    students_base_qs = students_base_qs.annotate(
        sent_friend_count=Count('sent_requests', filter=Q(sent_requests__status='accepted'), distinct=True),
        received_friend_count=Count('received_requests', filter=Q(received_requests__status='accepted'), distinct=True),
    ).annotate(
        friend_popularity=ExpressionWrapper(
            Coalesce(F('sent_friend_count'), zero_float) + Coalesce(F('received_friend_count'), zero_float),
            output_field=FloatField(),
        ),
    ).annotate(
        relevance_score=ExpressionWrapper(
            F('combined_similarity') + popularity_weight_value * Coalesce(
                F('friend_popularity'), zero_float,
            ),
            output_field=FloatField(),
        ),
    ).order_by('-relevance_score', '-combined_similarity').select_related(
        'university', 'student_location'
    ).prefetch_related(
        'student_interest',
        Prefetch('membership_set', queryset=Membership.objects.select_related('community')),
    )
    total_student_count = students_base_qs.count()
    students_page = list(students_base_qs[offset:offset + limit])
    student_ids = [student.id for student in students_page]

    mutual_context = {student_id: [] for student_id in student_ids}
    if student_ids and friend_ids:
        mutual_friendships = Friendship.objects.filter(
            status='accepted'
        ).filter(
            Q(sender_id__in=friend_ids, receiver_id__in=student_ids) |
            Q(receiver_id__in=friend_ids, sender_id__in=student_ids)
        ).select_related('sender', 'receiver')

        for relation in mutual_friendships:
            if relation.sender_id in friend_ids:
                friend_obj = relation.sender
                candidate_id = relation.receiver_id
            else:
                friend_obj = relation.receiver
                candidate_id = relation.sender_id

            bucket = mutual_context.setdefault(candidate_id, [])
            if not bucket:
                bucket.append({
                    'id': friend_obj.id,
                    'kinde_user_id': friend_obj.kinde_user_id,
                    'name': friend_obj.name,
                    'username': getattr(friend_obj, 'username', ''),
                    'bio': getattr(friend_obj, 'bio', ''),
                    'profile_image': friend_obj.profile_image.url if getattr(friend_obj, 'profile_image', None) else None,
                    'is_online': getattr(friend_obj, 'is_online', False),
                })
            elif len(bucket) < 5:
                bucket.append({'id': friend_obj.id})

    student_results = StudentSerializer(
        students_page,
        many=True,
        context={
            'kinde_user_id': kinde_user_id,
            'mutual_friends': mutual_context,
            'user_muted_student_ids': user_muted_student_ids,
            'user_blocked_student_ids': user_blocked_student_ids,
        },
    ).data

    community_member_count_sq = (
        Membership.objects.filter(community=OuterRef('pk'))
        .values('community')
        .annotate(total=Count('id'))
        .values('total')
    )

    communities_base_qs = (
        Communities.objects.annotate(
            community_name_similarity=TrigramSimilarity('community_name', query),
            community_bio_similarity=TrigramSimilarity('community_bio', query),
            description_similarity=TrigramSimilarity('description', query),
        )
        .annotate(
            combined_similarity=ExpressionWrapper(
                Coalesce(F('community_name_similarity'), zero_float) +
                Coalesce(F('community_bio_similarity'), zero_float) +
                Coalesce(F('description_similarity'), zero_float),
                output_field=FloatField(),
            ),
        )
        .filter(
            Q(community_name__icontains=query) |
            Q(community_name_similarity__gt=similarity_threshold) |
            Q(community_bio_similarity__gt=similarity_threshold) |
            Q(description_similarity__gt=0.3)
        )
        .exclude(id__in=blocked_community_ids)
        .select_related('location')
        .prefetch_related('community_interest')
    )
    communities_base_qs = communities_base_qs.annotate(
        member_popularity=Coalesce(
            Subquery(community_member_count_sq, output_field=FloatField()),
            zero_float,
        ),
    ).annotate(
        relevance_score=ExpressionWrapper(
            F('combined_similarity') + popularity_weight_value * Coalesce(
                F('member_popularity'), zero_float
            ),
            output_field=FloatField(),
        ),
    ).order_by('-relevance_score', '-combined_similarity')
    total_community_count = communities_base_qs.count()
    communities_page = list(communities_base_qs[offset:offset + limit])
    community_ids = [community.id for community in communities_page]

    friends_in_community_data = {community_id: [] for community_id in community_ids}
    if community_ids and friend_ids:
        friend_memberships = Membership.objects.filter(
            community_id__in=community_ids,
            user_id__in=friend_ids,
        ).select_related('user', 'community')

        community_friend_memberships = defaultdict(list)
        for membership in friend_memberships:
            community_friend_memberships[membership.community_id].append(membership)

        for community_id, memberships_for_community in community_friend_memberships.items():
            bucket = friends_in_community_data.get(community_id)
            if bucket is None:
                continue

            for membership in memberships_for_community:
                friend = membership.user
                if not bucket:
                    friend_entry = friend_details_map.get(friend.id, {
                            'id': friend.id,
                            'kinde_user_id': friend.kinde_user_id,
                            'name': friend.name,
                            'username': getattr(friend, 'username', ''),
                            'bio': getattr(friend, 'bio', ''),
                        'profile_image': friend.profile_image.url if getattr(friend, 'profile_image', None) else None,
                    })
                    detail = friend_entry.copy()
                    joined_at_value = getattr(membership, 'date_joined', None) or getattr(membership, 'created_at', None)
                    detail.update({
                            'role': membership.role,
                        'joined_at': joined_at_value.isoformat() if joined_at_value else None,
                    })
                    bucket.append(detail)
                elif len(bucket) < 5:
                    bucket.append({'id': friend.id})

    community_results = CommunitySerializer(
        communities_page,
        many=True,
        context={
        'kinde_user_id': kinde_user_id,
        'user_memberships': user_memberships,
        'user_community_roles': user_community_roles,
            'friends_in_community': friends_in_community_data,
        },
    ).data

    community_event_rsvp_sq = (
        CommunityEventRSVP.objects.filter(event=OuterRef('pk'))
        .values('event')
        .annotate(total=Count('id'))
        .values('total')
    )

    community_events_base_qs = (
        Community_Events.objects.annotate(
            event_name_similarity=TrigramSimilarity('event_name', query),
            description_similarity=TrigramSimilarity('description', query),
        )
        .annotate(
            combined_similarity=ExpressionWrapper(
                Coalesce(F('event_name_similarity'), zero_float) +
                Coalesce(F('description_similarity'), zero_float),
                output_field=FloatField(),
            ),
        )
        .select_related('community', 'poster')
        .prefetch_related('images', 'communityeventrsvp', 'community__community_interest')
        .filter(
            Q(event_name_similarity__gt=similarity_threshold) |
            Q(description_similarity__gt=similarity_threshold)
        )
        .exclude(community__id__in=blocked_community_ids)
    )
    community_events_base_qs = community_events_base_qs.annotate(
        rsvp_count=Coalesce(
            Subquery(community_event_rsvp_sq, output_field=FloatField()),
            zero_float,
        ),
    ).annotate(
        relevance_score=ExpressionWrapper(
            F('combined_similarity') + popularity_weight_value * Coalesce(
                F('rsvp_count'), zero_float
            ),
            output_field=FloatField(),
        ),
    ).order_by('-relevance_score', '-combined_similarity')
    total_community_event_count = community_events_base_qs.count()
    community_events_page = list(community_events_base_qs[offset:offset + limit])
    community_event_results = CommunityEventsSerializer(community_events_page, many=True).data

    student_event_rsvp_sq = (
        EventRSVP.objects.filter(event=OuterRef('pk'))
        .values('event')
        .annotate(total=Count('id'))
        .values('total')
    )

    student_events_base_qs = (
        Student_Events.objects.annotate(
            event_name_similarity=TrigramSimilarity('event_name', query),
            description_similarity=TrigramSimilarity('description', query),
        )
        .annotate(
            combined_similarity=ExpressionWrapper(
                Coalesce(F('event_name_similarity'), zero_float) +
                Coalesce(F('description_similarity'), zero_float),
                output_field=FloatField(),
            ),
        )
        .select_related('student')
        .prefetch_related('images', 'eventrsvp', 'student__student_interest')
        .filter(
            Q(event_name_similarity__gt=similarity_threshold) |
            Q(description_similarity__gt=similarity_threshold)
        )
        .exclude(student__id__in=blocked_student_ids)
    )
    student_events_base_qs = student_events_base_qs.annotate(
        rsvp_count=Coalesce(
            Subquery(student_event_rsvp_sq, output_field=FloatField()),
            zero_float,
        ),
    ).annotate(
        relevance_score=ExpressionWrapper(
            F('combined_similarity') + popularity_weight_value * Coalesce(
                F('rsvp_count'), zero_float
            ),
            output_field=FloatField(),
        ),
    ).order_by('-relevance_score', '-combined_similarity')
    total_student_event_count = student_events_base_qs.count()
    student_events_page = list(student_events_base_qs[offset:offset + limit])
    student_event_results = StudentEventSerializer(student_events_page, many=True).data

    post_like_count_sq = (
        PostLike.objects.filter(post=OuterRef('pk'))
        .values('post')
        .annotate(total=Count('id'))
        .values('total')
    )

    posts_base_qs = (
        Posts.objects.annotate(
            content_similarity=TrigramSimilarity('context_text', query),
        )
        .annotate(
            combined_similarity=ExpressionWrapper(
                Coalesce(F('content_similarity'), zero_float),
                output_field=FloatField(),
            ),
        )
        .select_related('student')
        .prefetch_related('images', 'likes', 'student__student_interest')
        .filter(content_similarity__gt=similarity_threshold)
        .exclude(student__id__in=blocked_student_ids)
    )
    posts_base_qs = posts_base_qs.annotate(
        like_count=Coalesce(
            Subquery(post_like_count_sq, output_field=FloatField()),
            zero_float,
        ),
    ).annotate(
        relevance_score=ExpressionWrapper(
            F('combined_similarity') + popularity_weight_value * Coalesce(
                F('like_count'), zero_float
            ),
            output_field=FloatField(),
        ),
    ).order_by('-relevance_score', '-combined_similarity')
    total_post_count = posts_base_qs.count()
    posts_page = list(posts_base_qs[offset:offset + limit])
    post_results = PostSerializer(posts_page, many=True).data

    community_post_like_sq = (
        LikeCommunityPost.objects.filter(event=OuterRef('pk'))
        .values('event')
        .annotate(total=Count('id'))
        .values('total')
    )

    community_posts_base_qs = (
        Community_Posts.objects.annotate(
            content_similarity=TrigramSimilarity('post_text', query),
        )
        .annotate(
            combined_similarity=ExpressionWrapper(
                Coalesce(F('content_similarity'), zero_float),
                output_field=FloatField(),
            ),
        )
        .select_related('community', 'poster')
        .prefetch_related('images', 'likecommunitypost_set', 'community__community_interest')
        .filter(content_similarity__gt=similarity_threshold)
        .exclude(community__id__in=blocked_community_ids)
    )
    community_posts_base_qs = community_posts_base_qs.annotate(
        like_count=Coalesce(
            Subquery(community_post_like_sq, output_field=FloatField()),
            zero_float,
        ),
    ).annotate(
        relevance_score=ExpressionWrapper(
            F('combined_similarity') + popularity_weight_value * Coalesce(
                F('like_count'), zero_float
            ),
            output_field=FloatField(),
        ),
    ).order_by('-relevance_score', '-combined_similarity')
    total_community_post_count = community_posts_base_qs.count()
    community_posts_page = list(community_posts_base_qs[offset:offset + limit])
    community_post_results = CommunityPostSerializer(community_posts_page, many=True).data

    combined_results = []

    for obj, payload in zip(students_page, student_results):
        item = dict(payload)
        item['type'] = 'student'
        item['relevance_score'] = float(getattr(obj, 'relevance_score', 0.0) or 0.0)
        combined_results.append(item)

    for obj, payload in zip(communities_page, community_results):
        item = dict(payload)
        item['type'] = 'community'
        item['relevance_score'] = float(getattr(obj, 'relevance_score', 0.0) or 0.0)
        combined_results.append(item)

    for obj, payload in zip(community_events_page, community_event_results):
        item = dict(payload)
        item['type'] = 'community_event'
        item['relevance_score'] = float(getattr(obj, 'relevance_score', 0.0) or 0.0)
        combined_results.append(item)

    for obj, payload in zip(student_events_page, student_event_results):
        item = dict(payload)
        item['type'] = 'student_event'
        item['relevance_score'] = float(getattr(obj, 'relevance_score', 0.0) or 0.0)
        combined_results.append(item)

    for obj, payload in zip(posts_page, post_results):
        item = dict(payload)
        item['type'] = 'post'
        item['relevance_score'] = float(getattr(obj, 'relevance_score', 0.0) or 0.0)
        combined_results.append(item)

    for obj, payload in zip(community_posts_page, community_post_results):
        item = dict(payload)
        item['type'] = 'community_post'
        item['relevance_score'] = float(getattr(obj, 'relevance_score', 0.0) or 0.0)
        combined_results.append(item)

    combined_results.sort(key=lambda entry: entry.get('relevance_score', 0.0), reverse=True)

    response_payload = {
        "query": query,
        "limit": limit,
        "offset": offset,
        "results": combined_results,
        # "students": student_results,
        # "communities": community_results,
        # "community_events": community_event_results,
        # "student_events": student_event_results,
        # "posts": post_results,
        # "community_posts": community_post_results,
        "meta": {
            "students": {"returned": len(student_results), "total": total_student_count},
            "communities": {"returned": len(community_results), "total": total_community_count},
            "community_events": {"returned": len(community_event_results), "total": total_community_event_count},
            "student_events": {"returned": len(student_event_results), "total": total_student_event_count},
            "posts": {"returned": len(post_results), "total": total_post_count},
            "community_posts": {"returned": len(community_post_results), "total": total_community_post_count},
        },
    }

    return JsonResponse(response_payload)
@api_view(['GET'])
@kinde_auth_required
def search_friends_and_communities(request, kinde_user_id=None):
    


    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse({"error": "No search query provided"}, status=400)

    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'error': 'Student not found'}, status=404)

    relationship_snapshot = get_relationship_snapshot(student.id)
    user_muted_student_ids = set(relationship_snapshot.get('muted_students', []))
    user_blocked_student_ids = set(relationship_snapshot.get('blocking', []))

    # ----------------------
    # Search through FRIENDS
    # ----------------------
    friend_ids = Friendship.objects.filter(
        Q(sender=student) | Q(receiver=student),
        status='accepted'
    ).values_list('sender_id', 'receiver_id')

    # Flatten and remove self
    friend_ids_flat = set(sum(friend_ids, ()))
    friend_ids_flat.discard(student.id)

    friends = (
        Student.objects.filter(id__in=friend_ids_flat, is_verified=True)
        .annotate(
            similarity=Greatest(
                TrigramSimilarity('name', query),
                TrigramSimilarity('username', query),
                TrigramSimilarity('email', query),
            )
        )
        .filter(
            Q(name__icontains=query) | Q(username__icontains=query) | Q(email__icontains=query)
        )
        .order_by('-similarity')
        .select_related('university', 'student_location')
        .prefetch_related(
            'student_interest',
            Prefetch('membership_set', queryset=Membership.objects.select_related('community')),
        )
    )

    # --------------------------
    # Search through COMMUNITIES
    # --------------------------
    community_ids = Membership.objects.filter(user=student).values_list('community_id', flat=True)

    communities = (
        Communities.objects.filter(id__in=community_ids)
        .annotate(
            similarity=Greatest(
                TrigramSimilarity('community_name', query),
                TrigramSimilarity('community_bio', query),
                TrigramSimilarity('description', query),
            )
        )
        .filter(
            Q(community_name__icontains=query) |
            Q(community_bio__icontains=query) |
            Q(description__icontains=query) |
            Q(community_interest__interest__icontains=query)
        )
        .distinct()
        .order_by('-similarity')
        .select_related('location')
        .prefetch_related('community_interest')
    )

    # Get user's memberships
    user_memberships = set(community_ids)

    # Get user's roles in communities
    user_community_roles = dict(
        Membership.objects.filter(user=student, community_id__in=community_ids).values_list('community_id', 'role')
    )

    return JsonResponse({
        "friends": StudentSerializer(friends, many=True, context={
            'kinde_user_id': kinde_user_id,
            'user_muted_student_ids': user_muted_student_ids,
            'user_blocked_student_ids': user_blocked_student_ids,
        }).data,
        "communities": CommunitySerializer(communities, many=True, context={
            'kinde_user_id': kinde_user_id,
            'user_memberships': user_memberships,
            'user_community_roles': user_community_roles
        }).data,
    }, status=200)
    

@api_view(['GET'])
@kinde_auth_required
def checkfriendshipstatus(request, kinde_user_id=None):
    target_student_id = request.query_params.get('student_id')

    # Get the requesting user (authenticated via Kinde)
    

    try:
        requester = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        target_student = Student.objects.get(pk=target_student_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Target student not found.'}, status=404)

    sent_request = Friendship.objects.filter(
        sender=requester,
        receiver=target_student
    ).first()

    # Option 2: Target student sent request to authenticated user
    received_request = Friendship.objects.filter(
        sender=target_student,
        receiver=requester
    ).first()

    if sent_request and sent_request.status == 'accepted':
        return JsonResponse({
            'status': 'success',
            'message': 'Friendship exists.',
            'friendship_status': 'friends',
            'friendship_id': sent_request.id
        }, status=200)
    
    elif received_request and received_request.status == 'accepted':
        # This covers the same mutual acceptance, but ensures consistency
        return JsonResponse({
            'status': 'success',
            'message': 'You are friends.',
            'friendship_status': 'friends',
            'friendship_id': received_request.id

        }, status=200)

    elif sent_request and sent_request.status == 'pending':
        return JsonResponse({
            'status': 'success',
            'message': f'added',
            'friendship_status': 'pending_sent',
            'friendship_id': sent_request.id
        }, status=200)

    elif received_request and received_request.status == 'pending':
        return JsonResponse({
            'status': 'success',
            'message': 'accept/decline',
            'friendship_status': 'pending_received',
            'friendship_id': received_request.id,
            # This is the ID the frontend must send to accept/decline friend request endpoints
            'pending_friend_request_id': received_request.id,
        }, status=200)
    
    else:
        # No friendship object found in either direction, or status is declined (if you keep declined records)
        return JsonResponse({
            'status': 'success',
            'message': 'No friendship found.',
            'friendship_status': 'no_friendship',
            'friendship_id': None
        }, status=200)

@api_view(['GET'])
@kinde_auth_required
def checkifmembershipexists(request, kinde_user_id):
    community_id = request.query_params.get('community_id')

    


    try:
        community = Communities.objects.get(pk=community_id)
    except Communities.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community not found.'}, status=404)

    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    # Check if the student is a member of the community
    membership = Membership.objects.filter(community=community, user=student).first()

    if membership:
        # If a membership object is found, it exists.
        # Now you can access its attributes like 'role'.
        return JsonResponse({'status': 'success', 'message': 'Membership exists.', 'membership_status': 'member', 'role': membership.role}, status=200)
    else:
        # If no membership object is found, it does not exist.
        return JsonResponse({'status': 'not_member', 'message': 'No membership found.', 'membership_status': 'not_member'}, status=200)

#block user from seeing event if they have blocked the creator or the creator has blocked them




    

@api_view(['POST'])
@kinde_auth_required
def toggle_rsvp_event(request, kinde_user_id=None):
    """
    Toggles an RSVP status for a Student_Events and broadcasts the update.
    """
    event_id = request.data.get('event_id')

    if not event_id:
        return JsonResponse({'status': 'error', 'message': 'event_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    if kinde_user_id is None:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
        
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
        event = Student_Events.objects.get(pk=event_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated student not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Student_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Event not found.'}, status=status.HTTP_404_NOT_FOUND)

    existing_rsvp = EventRSVP.objects.filter(event=event, student=student)

    action_message = ""
    status_code = None

    if existing_rsvp.exists():
        try:
            existing_rsvp.delete()
            action_message = 'RSVP cancelled successfully.'
            status_code = status.HTTP_200_OK
            
            channel_layer = get_channel_layer()
            if channel_layer:
                # --- CORRECTED: Broadcast removal to the user's personal channel ---
                async_to_sync(channel_layer.group_send)(
                    f'user_updates_{kinde_user_id}',
                    {
                        'type': 'user.updated',
                        'data': {
                            'update_type': 'item_removed',
                            'content_type': 'rsvpd_student_event', # <--- CORRECTED THIS LINE
                            'item_id': event_id,
                        }
                    }
                )
                # --- END CORRECTED ---
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"Failed to cancel RSVP: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    else:
        EventRSVP.objects.create(
            student=student,
            event=event,
            status='going' # You can modify this to take status from request.data
        )
        action_message = 'RSVP created successfully.'
        status_code = status.HTTP_201_CREATED

    # --- Broadcast Event Update via WebSocket ---
    updated_event = Student_Events.objects.annotate(
        going_count=Count('eventrsvp', filter=Q(eventrsvp__status='going')),
        interested_count=Count('eventrsvp', filter=Q(eventrsvp__status='interested')),
        not_going_count=Count('eventrsvp', filter=Q(eventrsvp__status='not_going')),
        discussion_count=Count('student_events_discussion') # Include discussion count for completeness
    ).get(pk=event_id)

    serializer_context = {'kinde_user_id': kinde_user_id}
    updated_event_data = StudentEventSerializer(updated_event, context=serializer_context).data

    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            # 1. Broadcast to the specific event's detail page group (SingleEventUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                f'event_updates_{event_id}', # The dynamic group name
                {
                    'type': 'event_updated', # Matches SingleEventUpdateConsumer's method
                    'event_type': 'student_event', # Correct event_type for Flutter
                    'event_data': updated_event_data,
                    'update_type': 'rsvp_changed'
                }
            )
            # 2. Broadcast to the general feed update group (FeedUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                'global_feed_updates',
                {
                    'type': 'feed.update',
                    'data': {
                        'update_type': 'event_rsvp_changed',
                        'content_type': 'student_event',
                        'item_id': event_id,
                        'item_data': updated_event_data
                    }
                }
            )
            
            # 3. Broadcast to the student's events list group (StudentEventsListUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                f'student_events_list_updates_{event.student.id}',
                {
                    'type': 'event_updated',
                    'event_data': updated_event_data
                }
            )
            
            # 4. Broadcast to all users' events feeds
            broadcast_event_update_to_feeds(updated_event_data, 'student_event')
            # REMOVED: The redundant async_to_sync(channel_layer.group_send)(f'event_updates_{event_id}', ...)
            # was a duplicate and unnecessary.
        except Exception as e:
            print(f"Error broadcasting student event RSVP update via WebSocket: {e}")

    return JsonResponse({'status': 'success', 'message': action_message}, status=status_code)


@api_view(['POST'])
@kinde_auth_required
def toggle_rsvp_community_event(request, kinde_user_id=None):
    """
    Toggles an RSVP status for a Community_Events and broadcasts the update.
    """
    community_event_id = request.data.get('community_event_id')

    if not community_event_id:
        return JsonResponse({'status': 'error', 'message': 'community_event_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    if kinde_user_id is None:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
        event = Community_Events.objects.get(pk=community_event_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated student not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Community_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Event not found.'}, status=status.HTTP_404_NOT_FOUND)

    existing_rsvp = CommunityEventRSVP.objects.filter(event=event, student=student)

    action_message = ""
    status_code = None

    if existing_rsvp.exists():
        try:
            existing_rsvp.delete()
            action_message = 'RSVP cancelled successfully.'
            status_code = status.HTTP_200_OK
            
            channel_layer = get_channel_layer()
            if channel_layer:
                # --- CORRECTED: Broadcast removal to the user's personal channel ---
                async_to_sync(channel_layer.group_send)(
                    f'user_updates_{kinde_user_id}',
                    {
                        'type': 'user.updated',
                        'data': {
                            'update_type': 'item_removed',
                            'content_type': 'rsvpd_community_event', # <--- CORRECTED THIS LINE
                            'item_id': community_event_id,
                        }
                    }
                )
                # --- END CORRECTED ---
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"Failed to cancel RSVP: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    else:
        CommunityEventRSVP.objects.create(
            student=student,
            event=event,
            status='going' # You can modify this to take status from request.data
        )
        action_message = 'RSVP created successfully.'
        status_code = status.HTTP_201_CREATED

    # --- Broadcast Event Update via WebSocket ---
    updated_event = Community_Events.objects.annotate(
        going_count=Count('communityeventrsvp', filter=Q(communityeventrsvp__status='going')),
        interested_count=Count('communityeventrsvp', filter=Q(communityeventrsvp__status='interested')),
        not_going_count=Count('communityeventrsvp', filter=Q(communityeventrsvp__status='not_going')),
        discussion_count=Count('community_events_discussion') # Include discussion count for completeness
    ).get(pk=community_event_id)

    serializer_context = {'kinde_user_id': kinde_user_id}
    updated_event_data = CommunityEventsSerializer(updated_event, context=serializer_context).data

    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            # 1. Broadcast to the specific community event's detail page group (SingleCommunityEventUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                f'community_event_updates_{community_event_id}',
                {
                    'type': 'community_event_updated',
                    'event_data': updated_event_data,
                    'update_type': 'rsvp_changed'
                }
            )
            # 2. Broadcast to the general feed update group (FeedUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                'global_feed_updates',
                {
                    'type': 'feed.update',
                    'data': {
                        'update_type': 'event_rsvp_changed',
                        'content_type': 'community_event',
                        'item_id': community_event_id,
                        'item_data': updated_event_data
                    }
                }
            )
            
            # 3. Broadcast to the community's events list group (CommunityEventsListUpdateConsumer)
            async_to_sync(channel_layer.group_send)(
                f'community_events_list_updates_{event.community.id}',
                {
                    'type': 'community_event_updated',
                    'event_data': updated_event_data
                }
            )
            
            # 4. Broadcast to all users' events feeds
            broadcast_event_update_to_feeds(updated_event_data, 'community_event')
            # REMOVED: The redundant async_to_sync(channel_layer.group_send)(f'event_updates_{community_event_id}', ...)
            # that had event_type='student_event' was a duplicate and incorrect.
        except Exception as e:
            print(f"Error broadcasting community event RSVP update via WebSocket: {e}")

    return JsonResponse({'status': 'success', 'message': action_message}, status=status_code)  

#########################################################################
#########################################################################
#########################################################################

@api_view(['GET'])
@kinde_auth_required
def get_student_bookmarked_events(request, kinde_user_id=None):
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=50)
    fetch_size = max(offset + limit, limit)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = _get_pending_deletion_student_ids()
    
    student_event_qs = (
        BookmarkedStudentEvents.objects.filter(student=student)
        .exclude(student_event__student__id__in=pending_deletion_ids)  # Exclude bookmarks on events from users with pending deletion
        .select_related('student_event__student')
        .prefetch_related(
        'student_event__images', 
        'student_event__eventrsvp', 
        'student_event__student__student_interest'
        )
        .order_by('-bookmarked_at')
    )

    community_event_qs = (
        BookmarkedCommunityEvents.objects.filter(student=student)
        .exclude(community_event__poster__id__in=pending_deletion_ids)  # Exclude bookmarks on events from users with pending deletion
        .select_related('community_event__community', 'community_event__poster')
        .prefetch_related(
        'community_event__images',
        'community_event__communityeventrsvp',
        'community_event__community__community_interest'
        )
        .order_by('-bookmarked_at')
    )

    total_student_bookmarks = student_event_qs.count()
    total_community_bookmarks = community_event_qs.count()
    total_count = total_student_bookmarks + total_community_bookmarks

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    student_event_slice, community_event_slice = (
        list(student_event_qs[:fetch_size]),
        list(community_event_qs[:fetch_size])
    )

    combined_entries = [
        ('student_event', bookmark.bookmarked_at, bookmark)
        for bookmark in student_event_slice
    ]
    combined_entries.extend(
        ('community_event', bookmark.bookmarked_at, bookmark)
        for bookmark in community_event_slice
    )

    combined_entries.sort(key=lambda entry: entry[1], reverse=True)
    page_entries = combined_entries[offset:offset + limit]

    results = []
    for kind, bookmarked_at, bookmark in page_entries:
        if kind == 'student_event':
            data = StudentEventSerializer(
                bookmark.student_event,
                context={'kinde_user_id': kinde_user_id}
            ).data
        else:
            data = CommunityEventsSerializer(
            bookmark.community_event,
            context={'kinde_user_id': kinde_user_id}
        ).data
        data['kind'] = kind
        data['bookmarked_at'] = bookmarked_at.isoformat()
        results.append(data)

    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': results,
        'count': len(results),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

# Backwards-compatibility aliases so URLs need not change


@api_view(['GET'])
@kinde_auth_required
def get_student_bookmarked_posts(request, kinde_user_id=None):
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=50)
    fetch_size = max(offset + limit, limit)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = _get_pending_deletion_student_ids()
    
    post_bookmarks_qs = (
        BookmarkedPosts.objects.filter(student=student)
        .exclude(post__student__id__in=pending_deletion_ids)
        .select_related('post__student')
        .prefetch_related(
            'post__images', 'post__likes', 'post__comments',
            Prefetch('post__bookmarkedposts_set', queryset=BookmarkedPosts.objects.select_related('student')),
            'post__student__student_interest',
            'post__student_mentions', 'post__community_mentions',
        )
        .order_by('-bookmarked_at')
    )

    community_post_bookmarks_qs = (
        BookmarkedCommunityPosts.objects.filter(student=student)
        .exclude(community_post__poster__id__in=pending_deletion_ids)
        .select_related('community_post__community', 'community_post__poster')
        .prefetch_related(
            'community_post__images', 'community_post__videos',
            'community_post__likecommunitypost_set', 'community_post__community_posts_comment_set',
            Prefetch('community_post__bookmarkedcommunityposts_set', queryset=BookmarkedCommunityPosts.objects.select_related('student')),
            'community_post__community__community_interest',
            'community_post__student_mentions', 'community_post__community_mentions',
        )
        .order_by('-bookmarked_at')
    )

    total_post_bookmarks = post_bookmarks_qs.count()
    total_community_post_bookmarks = community_post_bookmarks_qs.count()
    total_count = total_post_bookmarks + total_community_post_bookmarks

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    post_bookmarks_slice = list(post_bookmarks_qs[:fetch_size])
    community_post_bookmarks_slice = list(community_post_bookmarks_qs[:fetch_size])

    combined_entries = [
        ('post', bookmark.bookmarked_at, bookmark)
        for bookmark in post_bookmarks_slice
    ]
    combined_entries.extend(
        ('community_post', bookmark.bookmarked_at, bookmark)
        for bookmark in community_post_bookmarks_slice
    )

    combined_entries.sort(key=lambda entry: entry[1], reverse=True)
    page_entries = combined_entries[offset:offset + limit]

    serializer_context = {'request': request, 'kinde_user_id': kinde_user_id}
    results = []
    for kind, bookmarked_at, bookmark in page_entries:
        if kind == 'post':
            data = PostSerializer(bookmark.post, context=serializer_context).data
        else:
            data = CommunityPostSerializer(
                bookmark.community_post,
                context=serializer_context
            ).data
        data['kind'] = kind
        data['bookmarked_at'] = bookmarked_at.isoformat()
        results.append(data)

    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': results,
        'count': len(results),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)


# Backwards-compatibility aliases so URLs need not change
#async
@api_view(['POST'])
@kinde_auth_required # Uncomment this if you have the kinde_auth_required decorator defined
def toggle_bookmark_student_event(request, kinde_user_id=None):
    """
    Toggles bookmark status for a Student Event:
    - If bookmarked, it is unbookmarked (deleted).
    - If not bookmarked, it is bookmarked (created).

    Args:
        request (Request): The request object.
        -   POST data:
            -   student_event_id (int): The ID of the Student_Events to toggle bookmark for.

    Returns:
        Response: A Response indicating the outcome of the toggle operation.
            -   status: 'success' or 'error'
            -   message: A descriptive message
            -   status_code: 200 for unbookmarked, 201 for bookmarked, 400 for bad request, 404 for not found.
    """
    event_id = request.data.get('event_id')

    if not event_id:
        return Response({'status': 'error', 'message': 'student_event_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get the authenticated student based on your Kinde setup
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return Response({'status': 'error', 'message': 'Authenticated student not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        student_event = Student_Events.objects.get(pk=event_id)
    except Student_Events.DoesNotExist:
        return Response({'status': 'error', 'message': 'Student Event not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Attempt to get the existing bookmark
    existing_bookmark = BookmarkedStudentEvents.objects.filter(student=student, student_event=student_event)

    if existing_bookmark.exists():
        # Bookmark exists, so delete it (toggle off)
        existing_bookmark.delete()
        return Response({'status': 'success', 'message': 'Event unbookmarked successfully.'}, status=status.HTTP_200_OK)
    else:
        # Bookmark does not exist, so create it (toggle on)
        BookmarkedStudentEvents.objects.create(student=student, student_event=student_event)
        return Response({'status': 'success', 'message': 'Event bookmarked successfully.'}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@kinde_auth_required # Uncomment this if you have the kinde_auth_required decorator defined
def toggle_bookmark_community_event(request, kinde_user_id=None):
    """
    Toggles bookmark status for a Community Event:
    - If bookmarked, it is unbookmarked (deleted).
    - If not bookmarked, it is bookmarked (created).

    Args:
        request (Request): The request object.
        -   POST data:
            -   community_event_id (int): The ID of the Community_Events to toggle bookmark for.

    Returns:
        Response: A Response indicating the outcome of the toggle operation.
            -   status: 'success' or 'error'
            -   message: A descriptive message
            -   status_code: 200 for unbookmarked, 201 for bookmarked, 400 for bad request, 404 for not found.
    """
    community_event_id = request.data.get('community_event_id')


    if not community_event_id:
        return Response({'status': 'error', 'message': 'community_event_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get the authenticated student based on your Kinde setup
  
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except (KeyError, AttributeError, Student.DoesNotExist):
        return Response({'status': 'error', 'message': 'Authenticated student not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        community_event = Community_Events.objects.get(pk=community_event_id)
    except Community_Events.DoesNotExist:
        return Response({'status': 'error', 'message': 'Community Event not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Attempt to get the existing bookmark
    existing_bookmark = BookmarkedCommunityEvents.objects.filter(student=student, community_event=community_event)

    if existing_bookmark.exists():
        # Bookmark exists, so delete it (toggle off)
        existing_bookmark.delete()
        return Response({'status': 'success', 'message': 'Community Event unbookmarked successfully.'}, status=status.HTTP_200_OK)
    else:
        # Bookmark does not exist, so create it (toggle on)
        BookmarkedCommunityEvents.objects.create(student=student, community_event=community_event)
        return Response({'status': 'success', 'message': 'Community Event bookmarked successfully.'}, status=status.HTTP_201_CREATED)


@api_view(['POST']) # Changed from GET to POST for a toggling action
@kinde_auth_required # Uncomment this if you have the kinde_auth_required decorator defined
def toggle_bookmark_post(request, kinde_user_id=None):
    """
    Toggles bookmark status for a general Post:
    - If bookmarked, it is unbookmarked (deleted).
    - If not bookmarked, it is bookmarked (created).

    Args:
        request (Request): The request object.
        -   POST data:
            -   post_id (int): The ID of the Post to toggle bookmark for.

    Returns:
        Response: A Response indicating the outcome of the toggle operation.
            -   status: 'success' or 'error'
            -   message: A descriptive message
            -   status_code: 200 for unbookmarked, 201 for bookmarked, 400 for bad request, 404 for not found.
    """
    post_id = request.data.get('post_id')

    if not post_id:
        return JsonResponse({'status': 'error', 'message': 'post_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get the authenticated student based on your Kinde setup
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated student not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        post = Posts.objects.get(pk=post_id)
    except Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Post not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Attempt to get the existing bookmark
    existing_bookmark = BookmarkedPosts.objects.filter(student=student, post=post)

    if existing_bookmark.exists():
        # Bookmark exists, so delete it (toggle off)
        existing_bookmark.delete()
        return JsonResponse({'status': 'success', 'message': 'Post unbookmarked successfully.'}, status=status.HTTP_200_OK)
    else:
        # Bookmark does not exist, so create it (toggle on)
        BookmarkedPosts.objects.create(student=student, post=post)
        return JsonResponse({'status': 'success', 'message': 'Post bookmarked successfully.'}, status=status.HTTP_201_CREATED)


@api_view(['POST']) # Changed from GET to POST for a toggling action
@kinde_auth_required # Uncomment this if you have the kinde_auth_required decorator defined
def toggle_bookmark_community_post(request, kinde_user_id=None):
    """
    Toggles bookmark status for a Community Post:
    - If bookmarked, it is unbookmarked (deleted).
    - If not bookmarked, it is bookmarked (created).

    Args:
        request (Request): The request object.
        -   POST data:
            -   community_post_id (int): The ID of the Community_Posts to toggle bookmark for.

    Returns:
        Response: A Response indicating the outcome of the toggle operation.
            -   status: 'success' or 'error'
            -   message: A descriptive message
            -   status_code: 200 for unbookmarked, 201 for bookmarked, 400 for bad request, 404 for not found.
    """
    community_post_id = request.data.get('community_post_id')


    if not community_post_id:
        return JsonResponse({'status': 'error', 'message': 'community_post_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get the authenticated student based on your Kinde setup
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated student not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        community_post = Community_Posts.objects.get(pk=community_post_id)
    except Community_Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community Post not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Attempt to get the existing bookmark
    existing_bookmark = BookmarkedCommunityPosts.objects.filter(student=student, community_post=community_post)

    if existing_bookmark.exists():
        # Bookmark exists, so delete it (toggle off)
        existing_bookmark.delete()
        return JsonResponse({'status': 'success', 'message': 'Community Post unbookmarked successfully.'}, status=status.HTTP_200_OK)
    else:
        # Bookmark does not exist, so create it (toggle on)
        BookmarkedCommunityPosts.objects.create(student=student, community_post=community_post)
        return JsonResponse({'status': 'success', 'message': 'Community Post bookmarked successfully.'}, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@kinde_auth_required
def eventrsvpcount(request, kinde_user_id=None):
    event_id = request.query_params.get('event_id')

    try:
        event = Student_Events.objects.get(pk=event_id)
    except Student_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Event not found.'}, status=404)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = _get_pending_deletion_student_ids()
    
    rsvp_count = EventRSVP.objects.filter(event=event).exclude(student__id__in=pending_deletion_ids).count()
    return JsonResponse({'status': 'success', 'rsvp_count': rsvp_count}, status=200)

@api_view(['GET'])
@kinde_auth_required
def communityeventrsvpcount(request, kinde_user_id=None):
    event_id = request.query_params.get('event_id')

    try:
        event = Community_Events.objects.get(pk=event_id)
    except Community_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community Event not found.'}, status=404)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = _get_pending_deletion_student_ids()
    
    rsvp_count = CommunityEventRSVP.objects.filter(event=event).exclude(student__id__in=pending_deletion_ids).count()
    return JsonResponse({'status': 'success', 'rsvp_count': rsvp_count}, status=200)
@api_view(['POST'])
@kinde_auth_required # Uncomment this if you have the kinde_auth_required decorator defined
def toggle_block_student(request, kinde_user_id=None):
    data = request.data
    target_student_id = data.get('target_student_id')

    if not target_student_id:
        return Response({'status': 'error', 'message': 'target_student_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    # 1. Get the authenticated student (the blocker)
    try:
        requester = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return Response({'status': 'error', 'message': 'Authenticated student profile not found.'}, status=status.HTTP_404_NOT_FOUND)

    # 2. Get the target student
    try:
        target_student = Student.objects.get(pk=target_student_id)
    except Student.DoesNotExist:
        return Response({'status': 'error', 'message': 'Target student not found.'}, status=status.HTTP_404_NOT_FOUND)

    # 3. Prevent blocking oneself
    if requester.pk == target_student.pk:
        return Response({'status': 'error', 'message': 'You cannot block yourself.'}, status=status.HTTP_400_BAD_REQUEST)

    # 4. Check for existing block relationship
    existing_block = Block.objects.filter(blocker=requester, blocked=target_student)

    if existing_block.exists():
        # Block exists, so delete it (unblock)
        existing_block.delete()
        message = 'Student unblocked successfully.'
        response_status = status.HTTP_200_OK # OK
    else:
        # Block does not exist, so create it (block)
        # First, remove any existing accepted friendship between the two students
        Friendship.objects.filter(
            (Q(sender=requester, receiver=target_student) | Q(sender=target_student, receiver=requester)),
            status='accepted'
        ).delete()

        Block.objects.create(
            blocker=requester,
            blocked=target_student,
        )
        message = 'Student blocked successfully.'
        response_status = status.HTTP_201_CREATED # Created

    return Response({'status': 'success', 'message': message}, status=response_status)
@csrf_exempt
@kinde_auth_required
async def get_user_likes(request, kinde_user_id=None):
    """
    Returns a list of posts and community posts liked by the authenticated user.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=50)
    fetch_size = max(offset + limit, limit)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    post_likes_qs = (
        PostLike.objects.filter(student=student)
        .exclude(post__student__id__in=pending_deletion_ids)
        .select_related('post', 'post__student')
        .prefetch_related(
            'post__student__student_interest', 'post__images', 'post__likes', 'post__comments',
            Prefetch('post__bookmarkedposts_set', queryset=BookmarkedPosts.objects.select_related('student')),
            'post__student_mentions', 'post__community_mentions',
        )
        .order_by('-liked_at')
    )

    community_likes_qs = (
        LikeCommunityPost.objects.filter(student=student)
        .exclude(event__poster__id__in=pending_deletion_ids)
        .select_related('event', 'event__community', 'event__poster')
        .prefetch_related(
            'event__community__community_interest', 'event__images', 'event__videos',
            'event__likecommunitypost_set', 'event__community_posts_comment_set',
            Prefetch('event__bookmarkedcommunityposts_set', queryset=BookmarkedCommunityPosts.objects.select_related('student')),
            'event__student_mentions', 'event__community_mentions',
        )
        .order_by('-liked_at')
    )

    total_post_likes, total_community_likes = await asyncio.gather(
        sync_to_async(post_likes_qs.count)(),
        sync_to_async(community_likes_qs.count)()
    )

    total_count = total_post_likes + total_community_likes

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    post_likes_slice, community_likes_slice = await asyncio.gather(
        sync_to_async(list)(post_likes_qs[:fetch_size]),
        sync_to_async(list)(community_likes_qs[:fetch_size])
    )

    combined_entries = []
    for like in post_likes_slice:
        combined_entries.append(('post', like.liked_at, like))
    for like in community_likes_slice:
        combined_entries.append(('community_post', like.liked_at, like))

    combined_entries.sort(key=lambda entry: entry[1], reverse=True)
    page_entries = combined_entries[offset:offset + limit]

    serializer_context = {
        'request': request,
        'kinde_user_id': kinde_user_id
    }
    
    post_items = [entry[2].post for entry in page_entries if entry[0] == 'post']
    community_items = [entry[2].event for entry in page_entries if entry[0] == 'community_post']

    def _serialize_posts(posts):
        return PostSerializer(posts, many=True, context=serializer_context).data

    def _serialize_community_posts(events):
        return CommunityPostSerializer(events, many=True, context=serializer_context).data

    if post_items:
        post_serialized = await sync_to_async(_serialize_posts)(post_items)
    else:
        post_serialized = []

    if community_items:
        community_serialized = await sync_to_async(_serialize_community_posts)(community_items)
    else:
        community_serialized = []

    post_iter = iter(post_serialized)
    community_iter = iter(community_serialized)
    results = []

    for kind, liked_at, like_obj in page_entries:
        if kind == 'post':
            data = next(post_iter)
            data['kind'] = 'post'
            data['liked_at'] = liked_at.isoformat()
        else:
            data = next(community_iter)
            data['kind'] = 'community_post'
            data['liked_at'] = liked_at.isoformat()
        results.append(data)

    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': results,
        'count': len(results),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)


@csrf_exempt
@kinde_auth_required
async def get_user_rsvps(request, kinde_user_id=None):
    """
    Returns a list of events (both student and community) the authenticated user has RSVP'd to.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=50)
    fetch_size = max(offset + limit, limit)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    student_rsvp_qs = (
        EventRSVP.objects.filter(student=student)
        .exclude(event__student__id__in=pending_deletion_ids)  # Exclude RSVPs to events from users with pending deletion
        .select_related('event', 'event__student')
        .prefetch_related('event__student__student_interest', 'event__images', 'event__eventrsvp')
        .order_by('-rsvp_at')
    )
    
    community_rsvp_qs = (
        CommunityEventRSVP.objects.filter(student=student)
        .exclude(event__poster__id__in=pending_deletion_ids)  # Exclude RSVPs to events from users with pending deletion
        .select_related('event', 'event__community', 'event__poster')
        .prefetch_related('event__community__community_interest', 'event__images', 'event__communityeventrsvp')
        .order_by('-rsvp_at')
    )
    
    total_student_rsvp, total_community_rsvp = await asyncio.gather(
        sync_to_async(student_rsvp_qs.count)(),
        sync_to_async(community_rsvp_qs.count)()
    )

    total_count = total_student_rsvp + total_community_rsvp

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)

    student_rsvp_slice, community_rsvp_slice = await asyncio.gather(
        sync_to_async(list)(student_rsvp_qs[:fetch_size]),
        sync_to_async(list)(community_rsvp_qs[:fetch_size])
    )

    combined_entries = []
    for rsvp in student_rsvp_slice:
        combined_entries.append(('student_event', rsvp.rsvp_at, rsvp))
    for rsvp in community_rsvp_slice:
        combined_entries.append(('community_event', rsvp.rsvp_at, rsvp))

    combined_entries.sort(key=lambda entry: entry[1], reverse=True)
    page_entries = combined_entries[offset:offset + limit]

    serializer_context = {
        'request': request,
        'kinde_user_id': kinde_user_id
    }
    
    student_events = [entry[2].event for entry in page_entries if entry[0] == 'student_event']
    community_events = [entry[2].event for entry in page_entries if entry[0] == 'community_event']

    def _serialize_student_events(events):
        return StudentEventSerializer(events, many=True, context=serializer_context).data

    def _serialize_community_events(events):
        return CommunityEventsSerializer(events, many=True, context=serializer_context).data

    if student_events:
        student_serialized = await sync_to_async(_serialize_student_events)(student_events)
    else:
        student_serialized = []

    if community_events:
        community_serialized = await sync_to_async(_serialize_community_events)(community_events)
    else:
        community_serialized = []

    student_iter = iter(student_serialized)
    community_iter = iter(community_serialized)
    results = []

    for kind, rsvp_at, rsvp_obj in page_entries:
        if kind == 'student_event':
            data = next(student_iter)
            data['kind'] = 'student_event'
            data['rsvp_at'] = rsvp_at.isoformat()
        else:
            data = next(community_iter)
            data['kind'] = 'community_event'
            data['rsvp_at'] = rsvp_at.isoformat()
        results.append(data)

    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': results,
        'count': len(results),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)


@csrf_exempt
@kinde_auth_required
async def get_blocked_users(request, kinde_user_id=None):
    """
    Returns a list of users that the authenticated user has blocked.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    # Get all users that the current user has blocked
    blocked_relations = await sync_to_async(list)(
        Block.objects.filter(blocker=student)
        .select_related('blocked')
        .order_by('-timestamp')
    )
    
    # Extract the blocked users
    blocked_users = [relation.blocked for relation in blocked_relations]
    
    # Serialize with proper context
    serializer_context = {
        'request': request,
        'kinde_user_id': kinde_user_id
    }
    
    blocked_users_data = await sync_to_async(lambda: StudentNameSerializer(
        blocked_users, many=True, context=serializer_context
    ).data)()
    
    # Add blocked_at timestamp to each user
    for i, user_data in enumerate(blocked_users_data):
        user_data['blocked_at'] = blocked_relations[i].timestamp.isoformat()
    
    return JsonResponse({
        'status': 'success',
        'results': blocked_users_data,
        'count': len(blocked_users_data)
    }, status=200)


@csrf_exempt
@kinde_auth_required
async def get_muted_users(request, kinde_user_id=None):
    """
    Returns a list of users that the authenticated user has muted.
    """
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    # Get all users that the current user has muted
    muted_relations = await sync_to_async(list)(
        MutedStudents.objects.filter(student=student)
        .select_related('muted_student')
        .order_by('-muted_at')
    )
    
    # Extract the muted users
    muted_users = [relation.muted_student for relation in muted_relations]
    
    # Serialize with proper context
    serializer_context = {
        'request': request,
        'kinde_user_id': kinde_user_id
    }
    
    muted_users_data = await sync_to_async(lambda: StudentNameSerializer(
        muted_users, many=True, context=serializer_context
    ).data)()
    
    # Add muted_at timestamp to each user
    for i, user_data in enumerate(muted_users_data):
        user_data['muted_at'] = muted_relations[i].muted_at.isoformat()
    
    return JsonResponse({
        'status': 'success',
        'results': muted_users_data,
        'count': len(muted_users_data)
    }, status=200)


@csrf_exempt
@kinde_auth_required
@parser_classes([MultiPartParser, FormParser])
async def upload_chat_image(request, kinde_user_id=None):
    """
    Handles multiple image uploads for chat.
    Creates multiple DirectMessage or CommunityChatMessage objects.
    Supports optional reply to an existing message.
    """
    images = request.FILES.getlist('images')  # multiple files allowed
    chat_type = request.POST.get('chat_type')  # 'direct' or 'community'
    target_id = request.POST.get('target_id')  # other user's kinde ID or community PK
    message_text = request.POST.get('message', '')
    reply_id = request.POST.get('reply', None)  # Optional reply message ID

    if not images or not chat_type or not target_id:
        return JsonResponse({
            'status': 'error',
            'message': 'Images, chat_type, and target_id are required.'
        }, status=400)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Auth failed'}, status=401)

    try:
        sender_student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Sender not found'}, status=404)

    messages_created = []
    room_group_name = None
    serializer_class = None

    try:
        # --- Resolve reply object if given ---
        reply_obj = None
        if reply_id:
            try:
                if chat_type == 'direct':
                    reply_obj = await DirectMessage.objects.aget(pk=reply_id)
                elif chat_type == 'community':
                    reply_obj = await CommunityChatMessage.objects.aget(pk=reply_id)
            except (DirectMessage.DoesNotExist, CommunityChatMessage.DoesNotExist):
                reply_obj = None  # ignore invalid reply_id

        if chat_type == 'direct':
            # Direct chat
            receiver_student = await Student.objects.aget(kinde_user_id=target_id)
            user_pks = sorted([str(sender_student.pk), str(receiver_student.pk)])
            room_group_name = f'direct_chat_{user_pks[0]}_{user_pks[1]}'
            serializer_class = DirectMessageSerializer

            for image_file in images:
                msg = await create_direct_message(
                    sender_student,
                    receiver_student,
                    message_text,
                    image_file,
                    reply=reply_obj
                )
                messages_created.append(msg)

        elif chat_type == 'community':
            # Community chat
            community = await Communities.objects.aget(pk=target_id)
            room_group_name = f'community_chat_{community.pk}'
            serializer_class = CommunityChatMessageSerializer

            for image_file in images:
                msg = await create_community_message(
                    community,
                    sender_student,
                    message_text,
                    image_file,
                    reply=reply_obj
                )
                messages_created.append(msg)

        else:
            return JsonResponse({'status': 'error', 'message': 'Invalid chat_type'}, status=400)

        # --- Broadcast all messages ---
        channel_layer = get_channel_layer()
        if channel_layer:
            for msg in messages_created:
                # serializer must run in sync thread to avoid async DB calls in .data
                serialized = await sync_to_async(serializer_class)(
                    msg, context={'request': request}
                )
                serialized_data = await database_sync_to_async(lambda: serialized.data)()
                await channel_layer.group_send(
                    room_group_name,
                    {
                        'type': 'chat_message',
                        'message': serialized_data,
                        'sender_kinde_id': sender_student.kinde_user_id
                    }
                )

        return JsonResponse({
            'status': 'success',
            'messages': [
                {
                    'message_id': msg.pk,
                    'image_url': msg.image.url if msg.image else None
                }
                for msg in messages_created
            ]
        }, status=201)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@csrf_exempt
@kinde_auth_required
async def send_sharable(request, kinde_user_id=None):
    data = json.loads(request.body.decode('utf-8'))
    
    # Updated: Now accepting lists for both direct and community recipients
    friend_ids = data.get('friend_ids', [])  # Friend primary key IDs
    community_ids = data.get('community_ids', [])       # List of Community PKs for community chats
    sharable_id = data.get('id')
    message_text = data.get('message', '')  # Renamed to avoid confusion with the object
    sharable_type = data.get('type')

    if not sharable_id:
        return JsonResponse({'error': 'Sharable ID is required.'}, status=status.HTTP_400_BAD_REQUEST)

    # Ensure at least one recipient type is provided
    if not friend_ids and not community_ids:
        return JsonResponse({'error': 'At least one direct message recipient (friend_ids) or community (community_ids) is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = await Student.objects.aget(kinde_user_id=kinde_user_id)  # Fixed: Use aget for async
    except Student.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)
    
    all_results = {'direct_messages': [], 'community_messages': []}

    # Define sharable object retrieval and processing logic
    if sharable_type == 'post':
        try:
            post = await Posts.objects.aget(pk=sharable_id)  # Fixed: Use aget for async
        except Posts.DoesNotExist:
            return JsonResponse({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process direct messages
        for friend_id in friend_ids:
            try:
                friend = await Student.objects.aget(pk=friend_id)  # Use pk for friend ID
                
                msg = await create_direct_message_for_sharable(
                    sender_obj=user,
                    receiver_obj=friend,
                    message_text=message_text,
                    post=post
                )
                
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Student.DoesNotExist:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': 'Friend not found'
                })
            except Exception as e:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': f'Failed to send direct message: {str(e)}'
                })

        # Process community messages
        for community_pk in community_ids:
            try:
                community = await Communities.objects.aget(pk=community_pk)  # Fixed: Use aget for async
                
                # Check if user is a member of the community
                is_member = await Membership.objects.filter(community=community, user=user).aexists()  # Fixed: Use aexists for async
                if not is_member:
                    all_results['community_messages'].append({
                        'recipient_community_id': community_pk,
                        'status': 'error',
                        'message': 'User is not a member of this community.'
                    })
                    continue

                msg = await create_community_message_for_sharable(
                    community_obj=community,
                    student_obj=user,
                    message_text=message_text,
                    post=post 
                )
                
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Communities.DoesNotExist:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': 'Community not found'
                })
            except Exception as e:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': f'Failed to send community message: {str(e)}'
                })
    
    elif sharable_type == 'community_post':
        try:
            community_post = await Community_Posts.objects.aget(pk=sharable_id)  # Fixed: Use aget for async
        except Community_Posts.DoesNotExist:
            return JsonResponse({'error': 'Community post not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process direct messages
        for friend_id in friend_ids:
            try:
                friend = await Student.objects.aget(pk=friend_id)  # Use pk for friend ID
                
                msg = await create_direct_message_for_sharable(
                    sender_obj=user,
                    receiver_obj=friend,
                    message_text=message_text,
                    community_post=community_post
                )
                
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Student.DoesNotExist:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': 'Friend not found'
                })
            except Exception as e:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': f'Failed to send direct message: {str(e)}'
                })

        # Process community messages
        for community_pk in community_ids:
            try:
                community = await Communities.objects.aget(pk=community_pk)  # Fixed: Use aget
                
                is_member = await Membership.objects.filter(community=community, user=user).aexists()  # Fixed: Use aexists
                if not is_member:
                    all_results['community_messages'].append({
                        'recipient_community_id': community_pk,
                        'status': 'error',
                        'message': 'User is not a member of this community.'
                    })
                    continue

                msg = await create_community_message_for_sharable(
                    community_obj=community,
                    student_obj=user,
                    message_text=message_text,
                    community_post=community_post 
                )
                
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Communities.DoesNotExist:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': 'Community not found'
                })
            except Exception as e:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': f'Failed to send community message: {str(e)}'
                })
    
    elif sharable_type == 'community_event':
        try:
            community_event = await Community_Events.objects.aget(pk=sharable_id)  # Fixed: Use aget
        except Community_Events.DoesNotExist:
            return JsonResponse({'error': 'Community event not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process direct messages
        for friend_id in friend_ids:
            try:
                friend = await Student.objects.aget(pk=friend_id)  # Use pk for friend ID
                
                msg = await create_direct_message_for_sharable(
                    sender_obj=user,
                    receiver_obj=friend,
                    message_text=message_text,
                    community_event=community_event
                )
                
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Student.DoesNotExist:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': 'Friend not found'
                })
            except Exception as e:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': f'Failed to send direct message: {str(e)}'
                })

        # Process community messages
        for community_pk in community_ids:
            try:
                community = await Communities.objects.aget(pk=community_pk)  # Fixed: Use aget
                
                is_member = await Membership.objects.filter(community=community, user=user).aexists()  # Fixed: Use aexists
                if not is_member:
                    all_results['community_messages'].append({
                        'recipient_community_id': community_pk,
                        'status': 'error',
                        'message': 'User is not a member of this community.'
                    })
                    continue

                msg = await create_community_message_for_sharable(
                    community_obj=community,
                    student_obj=user,
                    message_text=message_text,
                    community_event=community_event 
                )
                
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Communities.DoesNotExist:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': 'Community not found'
                })
            except Exception as e:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': f'Failed to send community message: {str(e)}'
                })
    
    elif sharable_type == 'student_event':
        try:
            student_event = await Student_Events.objects.aget(pk=sharable_id)  # Fixed: Use aget
        except Student_Events.DoesNotExist:
            return JsonResponse({'error': 'Student event not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process direct messages
        for friend_id in friend_ids:
            try:
                friend = await Student.objects.aget(pk=friend_id)  # Use pk for friend ID
                
                msg = await create_direct_message_for_sharable(
                    sender_obj=user,
                    receiver_obj=friend,
                    message_text=message_text,
                    student_event=student_event
                )
                
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Student.DoesNotExist:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': 'Friend not found'
                })
            except Exception as e:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': f'Failed to send direct message: {str(e)}'
                })

        # Process community messages
        for community_pk in community_ids:
            try:
                community = await Communities.objects.aget(pk=community_pk)  # Fixed: Use aget
                
                is_member = await Membership.objects.filter(community=community, user=user).aexists()  # Fixed: Use aexists
                if not is_member:
                    all_results['community_messages'].append({
                        'recipient_community_id': community_pk,
                        'status': 'error',
                        'message': 'User is not a member of this community.'
                    })
                    continue

                msg = await create_community_message_for_sharable(
                    community_obj=community,
                    student_obj=user,
                    message_text=message_text,
                    student_event=student_event 
                )
                
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Communities.DoesNotExist:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': 'Community not found'
                })
            except Exception as e:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': f'Failed to send community message: {str(e)}'
                })
    
    elif sharable_type == 'community_profile':
        try:
            community_profile = await Communities.objects.aget(pk=sharable_id)  # Fixed: Use aget
        except Communities.DoesNotExist:
            return JsonResponse({'error': 'Community profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process direct messages
        for friend_id in friend_ids:
            try:
                friend = await Student.objects.aget(pk=friend_id)  # Use pk for friend ID
                
                msg = await create_direct_message_for_sharable(
                    sender_obj=user,
                    receiver_obj=friend,
                    message_text=message_text,
                    community_profile=community_profile
                )
                
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Student.DoesNotExist:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': 'Friend not found'
                })
            except Exception as e:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': f'Failed to send direct message: {str(e)}'
                })

        # Process community messages
        for community_pk in community_ids:
            try:
                community = await Communities.objects.aget(pk=community_pk)  # Fixed: Use aget
                
                is_member = await Membership.objects.filter(community=community, user=user).aexists()  # Fixed: Use aexists
                if not is_member:
                    all_results['community_messages'].append({
                        'recipient_community_id': community_pk,
                        'status': 'error',
                        'message': 'User is not a member of this community.'
                    })
                    continue

                msg = await create_community_message_for_sharable(
                    community_obj=community,
                    student_obj=user,
                    message_text=message_text,
                    community_profile=community_profile 
                )
                
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Communities.DoesNotExist:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': 'Community not found'
                })
            except Exception as e:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': f'Failed to send community message: {str(e)}'
                })
    
    elif sharable_type == 'student_profile':
        try:
            student_profile = await Student.objects.aget(pk=sharable_id)  # Fixed: Use aget
        except Student.DoesNotExist:
            return JsonResponse({'error': 'Student profile not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process direct messages
        for friend_id in friend_ids:
            try:
                friend = await Student.objects.aget(pk=friend_id)  # Use pk for friend ID
                
                msg = await create_direct_message_for_sharable(
                    sender_obj=user,
                    receiver_obj=friend,
                    message_text=message_text,
                    student_profile=student_profile
                )
                
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Student.DoesNotExist:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': 'Friend not found'
                })
            except Exception as e:
                all_results['direct_messages'].append({
                    'recipient_id': friend_id,
                    'status': 'error',
                    'message': f'Failed to send direct message: {str(e)}'
                })

        # Process community messages
        for community_pk in community_ids:
            try:
                community = await Communities.objects.aget(pk=community_pk)  # Fixed: Use aget
                
                is_member = await Membership.objects.filter(community=community, user=user).aexists()  # Fixed: Use aexists
                if not is_member:
                    all_results['community_messages'].append({
                        'recipient_community_id': community_pk,
                        'status': 'error',
                        'message': 'User is not a member of this community.'
                    })
                    continue

                msg = await create_community_message_for_sharable(
                    community_obj=community,
                    student_obj=user,
                    message_text=message_text,
                    student_profile=student_profile 
                )
                
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'success',
                    'message_id': msg.id
                })
                
            except Communities.DoesNotExist:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': 'Community not found'
                })
            except Exception as e:
                all_results['community_messages'].append({
                    'recipient_community_id': community_pk,
                    'status': 'error',
                    'message': f'Failed to send community message: {str(e)}'
                })
    
    else:
        return JsonResponse({'error': 'Invalid sharable type'}, status=status.HTTP_400_BAD_REQUEST)
            
    return JsonResponse({
        'status': 'success',
        'sent_messages': all_results
    }, status=status.HTTP_200_OK)


@csrf_exempt
@kinde_auth_required
async def mark_messages_as_read(request, kinde_user_id=None):
    """
    Mark direct messages as read. Called when the current user opens a chat with another user.
    Request body: { "receiver_kinde_id": "kp_xxx", "message_ids": [123, 124, 125] }
    - receiver_kinde_id = Kinde ID of the other user in the conversation (the one whose messages we are marking as read).
    - message_ids = IDs of direct messages the current user has just read.
    """
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed.'}, status=status.HTTP_401_UNAUTHORIZED)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON.'}, status=status.HTTP_400_BAD_REQUEST)
    receiver_kinde_id = data.get('receiver_kinde_id')
    message_ids = data.get('message_ids')
    if not receiver_kinde_id:
        return JsonResponse({'status': 'error', 'message': 'receiver_kinde_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
    if not message_ids or not isinstance(message_ids, list):
        return JsonResponse({'status': 'error', 'message': 'message_ids must be a non-empty list.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
        other_user = await Student.objects.aget(kinde_user_id=receiver_kinde_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    if me.pk == other_user.pk:
        return JsonResponse({'status': 'error', 'message': 'Cannot mark messages with yourself.'}, status=status.HTTP_400_BAD_REQUEST)
    now = timezone.now()

    @sync_to_async
    def _mark_read():
        valid = list(
            DirectMessage.objects.filter(
                id__in=message_ids,
                sender=other_user,
                receiver=me,
            ).values_list('id', flat=True)
        )
        DirectMessage.objects.filter(
            id__in=valid,
            is_read=False,
        ).update(is_read=True, read_at=now)
        return valid

    marked_ids = await _mark_read()
    if not marked_ids:
        return HttpResponse(status=204)

    read_at_iso = now.isoformat()
    if read_at_iso.endswith('+00:00'):
        read_at_iso = read_at_iso[:-6] + 'Z'
    user_pks = sorted([str(me.pk), str(other_user.pk)])
    room_name = f'direct_chat_{user_pks[0]}_{user_pks[1]}'
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(room_name, {
            'type': 'message_read_event',
            'message_ids': list(marked_ids),
            'read_at': read_at_iso,
            'reader_kinde_id': kinde_user_id,
        })
    return HttpResponse(status=204)


@csrf_exempt
@kinde_auth_required
async def get_direct_messages_history(request, other_user_kinde_id=None, kinde_user_id=None):
    """
    Retrieves the chat message history between the authenticated user and another user.
    Optimized for high concurrency (500+ concurrent users).
    
    Pagination Parameters:
    - limit: Number of messages to return (default: 50, max: 200)
    - before: Message ID to fetch messages before (for loading older messages)
    - after: Message ID to fetch messages after (for loading newer messages)
    - before_timestamp: ISO timestamp to fetch messages before
    - after_timestamp: ISO timestamp to fetch messages after
    """
    # 1. Validate required parameters early
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    if not other_user_kinde_id:
        return JsonResponse({'status': 'error', 'message': 'Other user Kinde ID is required in the URL.'}, status=status.HTTP_400_BAD_REQUEST)

    # 2. Fetch both users concurrently
    try:
        me, other_user = await asyncio.gather(
            Student.objects.aget(kinde_user_id=kinde_user_id),
            Student.objects.aget(kinde_user_id=other_user_kinde_id),
            return_exceptions=True
        )
        
        # Check if either query failed
        if isinstance(me, Exception):
            return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
        if isinstance(other_user, Exception):
            return JsonResponse({'status': 'error', 'message': 'Other user not found.'}, status=status.HTTP_404_NOT_FOUND)
            
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': 'Database error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # 3. Prevent fetching messages with oneself
    if me.pk == other_user.pk:
        return JsonResponse({'status': 'info', 'message': 'Cannot fetch direct messages with yourself.'}, status=status.HTTP_200_OK)

    # 4. Parse pagination parameters
    limit, offset = _parse_pagination_params(request)

    before_id = request.GET.get('before')
    after_id = request.GET.get('after')
    before_timestamp = request.GET.get('before_timestamp')
    after_timestamp = request.GET.get('after_timestamp')

    # 5. Build base queryset - NOW MATCHES SERIALIZER FIELDS
    messages_queryset = DirectMessage.objects.filter(
        Q(sender=me, receiver=other_user) | Q(sender=other_user, receiver=me)
    ).select_related(
        # Core message relationships
        'sender',                    # For sender field (StudentChatSerializer)
        'receiver',                  # For receiver field (StudentChatSerializer)
        
        # Reply relationships
        'reply',                     # For reply field (DirectMessageParentSerializer)
        'reply__sender',             # For nested sender in reply
        'reply__receiver',           # For nested receiver in reply
        
        # Content relationships
        'post',                      # For post field (PostNameSerializer)
        'post__student',             # For post.student.profile_image (student_profile_picture)
        'community_post',            # For community_post field (CommunityPostNameSerializer)
        'community_post__community', # For community_post.community.community_image
        'student_event',             # For student_event field (StudentEventNameSerializer)
        'student_event__student',    # For student_event.student.profile_image (student_profile_picture)
        'community_event',           # For community_event field (CommunityEventsNameSerializer)
        'community_event__community', # For community_event.community.community_image
        
        # Profile relationships - NOW PROPERLY INCLUDED
        'student_profile',           # For student_profile field (StudentNameSerializer)
        'community_profile',         # For community_profile field (CommunityNameSerializer)
    ).order_by('-timestamp', '-id')

    # 6. Apply pagination filters
    try:
        # Handle message ID-based pagination
        if before_id:
            try:
                before_message = await DirectMessage.objects.aget(id=before_id)
                messages_queryset = messages_queryset.filter(
                    Q(timestamp__lt=before_message.timestamp) |
                    Q(timestamp=before_message.timestamp, id__lt=before_message.id)
                )
            except DirectMessage.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'Before message not found.'}, status=status.HTTP_400_BAD_REQUEST)
        
        elif after_id:
            try:
                after_message = await DirectMessage.objects.aget(id=after_id)
                messages_queryset = messages_queryset.filter(
                    Q(timestamp__gt=after_message.timestamp) |
                    Q(timestamp=after_message.timestamp, id__gt=after_message.id)
                ).order_by('timestamp', 'id')
            except DirectMessage.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'After message not found.'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Handle timestamp-based pagination
        elif before_timestamp:
            before_dt = datetime.fromisoformat(before_timestamp.replace('Z', '+00:00'))
            messages_queryset = messages_queryset.filter(timestamp__lt=before_dt)
        
        elif after_timestamp:
            after_dt = datetime.fromisoformat(after_timestamp.replace('Z', '+00:00'))
            messages_queryset = messages_queryset.filter(timestamp__gt=after_dt).order_by('timestamp', 'id')

    except ValueError as e:
        return JsonResponse({'status': 'error', 'message': f'Invalid timestamp format: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)

    # 7. Execute the query asynchronously - IMPROVED METHOD
    try:
        @sync_to_async
        def fetch_messages():
            return list(messages_queryset[offset:offset + limit + 1])
        
        messages_list = await fetch_messages()
            
    except Exception as e:
        # Enhanced error logging for debugging
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Database query failed: {type(e).__name__}: {str(e)}")
        return JsonResponse({'status': 'error', 'message': 'Failed to fetch messages.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # 8. Check if there are more messages
    has_more = len(messages_list) > limit
    if has_more:
        messages_list = messages_list[:limit]

    # 9. Reverse order if loading older messages (default behavior)
    if not after_id and not after_timestamp:
        messages_list.reverse()

    # 10. Build pagination metadata
    pagination_info = {
        'has_more': has_more,
        'limit': limit,
        'count': len(messages_list)
    }
    
    if messages_list:
        first_message = messages_list[0]
        last_message = messages_list[-1]
        
        pagination_info.update({
            'first_message_id': first_message.id,
            'last_message_id': last_message.id,
            'first_timestamp': first_message.timestamp.isoformat(),
            'last_timestamp': last_message.timestamp.isoformat(),
        })
        
        # Build pagination URLs
        base_url = request.build_absolute_uri().split('?')[0]
        if has_more and not (after_id or after_timestamp):
            pagination_info['next_url'] = f"{base_url}?before={first_message.id}&limit={limit}"
        pagination_info['newer_url'] = f"{base_url}?after={last_message.id}&limit={limit}"

    # 11. Serialize messages - IMPROVED METHOD
    try:
        @sync_to_async
        def serialize_messages():
            serializer_context = {'request': request}
            serializer = DirectMessageSerializer(messages_list, many=True, context=serializer_context)
            return serializer.data
        
        serializer_data = await serialize_messages()
        
    except Exception as e:
        # Enhanced error logging for debugging
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Serialization failed: {type(e).__name__}: {str(e)}")
        return JsonResponse({'status': 'error', 'message': 'Failed to serialize messages.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return JsonResponse({
        'status': 'success',
        'messages': serializer_data,
        'pagination': pagination_info
    }, status=status.HTTP_200_OK)

@csrf_exempt
@kinde_auth_required
async def get_community_messages_history(request, community_id=None, kinde_user_id=None):
    """
    Retrieves the chat message history for a specific community,
    with advanced cursor-based pagination.
    Optimized for high concurrency (500+ concurrent users).
    
    Pagination Parameters:
    - limit: Number of messages to return (default: 50, max: 200)
    - before: Message ID to fetch messages before (for loading older messages)
    - after: Message ID to fetch messages after (for loading newer messages)
    - before_timestamp: ISO timestamp to fetch messages before
    - after_timestamp: ISO timestamp to fetch messages after
    """
    # 1. Validate required parameters early
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    if not community_id:
        return JsonResponse({'status': 'error', 'message': 'Community ID is required in the URL.'}, status=status.HTTP_400_BAD_REQUEST)

    # 2. Fetch user and community concurrently
    try:
        me, community = await asyncio.gather(
            Student.objects.aget(kinde_user_id=kinde_user_id),
            Communities.objects.aget(pk=community_id),
            return_exceptions=True
        )
        
        # Check if either query failed
        if isinstance(me, Exception):
            return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
        if isinstance(community, Exception):
            return JsonResponse({'status': 'error', 'message': 'Community not found.'}, status=status.HTTP_404_NOT_FOUND)
            
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': 'Database error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # 3. Check if the user is a member of the community (Access control)
    try:
        is_member = await Membership.objects.filter(user=me, community=community).aexists()
        if not is_member:
            return JsonResponse({'status': 'error', 'message': 'You are not a member of this community.'}, status=status.HTTP_403_FORBIDDEN)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': 'Failed to verify membership.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # 4. Parse pagination parameters
    limit, offset = _parse_pagination_params(request)
        
    before_id = request.GET.get('before')
    after_id = request.GET.get('after')
    before_timestamp = request.GET.get('before_timestamp')
    after_timestamp = request.GET.get('after_timestamp')
    
    # 5. Build base queryset
    messages_queryset = CommunityChatMessage.objects.filter(
    community=community
    ).select_related(
        'student',
        'community',
        'student_profile',
        'community_profile',
        'reply',
        'reply__student',
        'post',
        'community_post',
        'student_event', 
        'community_event',
    ).prefetch_related('read_by').order_by('-sent_at', '-id')
    
    # 6. Apply pagination filters
    try:
        # Handle message ID-based pagination
        if before_id:
            try:
                before_message = await CommunityChatMessage.objects.aget(id=before_id)
                messages_queryset = messages_queryset.filter(
                    Q(sent_at__lt=before_message.sent_at) |
                    Q(sent_at=before_message.sent_at, id__lt=before_message.id)
                )
            except CommunityChatMessage.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'Before message not found.'}, status=status.HTTP_400_BAD_REQUEST)
        
        elif after_id:
            try:
                after_message = await CommunityChatMessage.objects.aget(id=after_id)
                messages_queryset = messages_queryset.filter(
                    Q(sent_at__gt=after_message.sent_at) |
                    Q(sent_at=after_message.sent_at, id__gt=after_message.id)
                ).order_by('sent_at', 'id')  # Oldest first when loading newer messages
            except CommunityChatMessage.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'After message not found.'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Handle timestamp-based pagination
        elif before_timestamp:
            before_dt = datetime.fromisoformat(before_timestamp.replace('Z', '+00:00'))
            if timezone.is_naive(before_dt):
                before_dt = timezone.make_aware(before_dt)
            messages_queryset = messages_queryset.filter(sent_at__lt=before_dt)
        
        elif after_timestamp:
            after_dt = datetime.fromisoformat(after_timestamp.replace('Z', '+00:00'))
            if timezone.is_naive(after_dt):
                after_dt = timezone.make_aware(after_dt)
            messages_queryset = messages_queryset.filter(sent_at__gt=after_dt).order_by('sent_at', 'id')

    except ValueError as e:
        return JsonResponse({'status': 'error', 'message': f'Invalid timestamp format: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)

    # 7. Execute the query asynchronously with limit + 1
    try:
        # Convert queryset to list asynchronously
        messages_list = await sync_to_async(list)(messages_queryset[offset:offset + limit + 1])
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': 'Failed to fetch messages.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # 8. Check if there are more messages
    has_more = len(messages_list) > limit
    if has_more:
        messages_list = messages_list[:limit]

    # 9. Reverse order if loading older messages (default behavior)
    if not after_id and not after_timestamp:
        messages_list.reverse()

    # 10. Build pagination metadata
    pagination_info = {
        'has_more': has_more,
        'limit': limit,
        'count': len(messages_list)
    }
    
    if messages_list:
        first_message = messages_list[0]
        last_message = messages_list[-1]
        
        pagination_info.update({
            'first_message_id': first_message.id,
            'last_message_id': last_message.id,
            'first_timestamp': first_message.sent_at.isoformat(),
            'last_timestamp': last_message.sent_at.isoformat(),
        })
        
        # Build pagination URLs
        base_url = request.build_absolute_uri().split('?')[0]
        if has_more and not (after_id or after_timestamp):
            pagination_info['next_url'] = f"{base_url}?before={first_message.id}&limit={limit}"
        pagination_info['newer_url'] = f"{base_url}?after={last_message.id}&limit={limit}"

    # 11. Serialize messages asynchronously
    try:
        serializer_context = {'request': request, 'kinde_user_id': kinde_user_id}
        serializer = CommunityChatMessageSerializer(messages_list, many=True, context=serializer_context)
        serializer_data = await sync_to_async(lambda: serializer.data)()
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': 'Failed to serialize messages.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return JsonResponse({
        'status': 'success',
        'community_messages': serializer_data,
        'pagination': pagination_info
    }, status=status.HTTP_200_OK)

@api_view(['GET'])
@kinde_auth_required
def get_all_unified_chats(request, kinde_user_id=None):
    """
    Retrieves a unified list of all direct message, community chat, and group chat
    conversations for the authenticated user, sorted by the timestamp of the last message.
    """
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        me = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)

    def get_sharable_type(message_obj):
        """Helper function to determine the type of sharable content in a message"""
        if message_obj.post:
            return "post"
        elif message_obj.community_post:
            return "community post"
        elif message_obj.student_event:
            return "student event"
        elif message_obj.community_event:
            return "community event"
        elif message_obj.student_profile:
            return "student profile"
        elif message_obj.community_profile:
            return "community profile"
        else:
            return "message"

    unified_conversations = []
    serializer_context = {'request': request} # Pass request context for image URLs

    # --- 1. Get Direct Message Conversations ---
    all_my_direct_messages = DirectMessage.objects.filter(
        Q(sender=me) | Q(receiver=me)
    ).annotate(
        other_participant_pk=Case(
            When(sender=me, then=F('receiver_id')),
            default=F('sender_id'),
            output_field=models.IntegerField()
        )
    ).order_by('-timestamp')

    latest_dm_by_conversation = {}
    for message in all_my_direct_messages:
        other_pk = message.other_participant_pk
        if other_pk not in latest_dm_by_conversation or message.timestamp > latest_dm_by_conversation[other_pk].timestamp:
            latest_dm_by_conversation[other_pk] = message

    # Pre-fetch all other participant Students for DM conversations
    other_dm_pks = list(latest_dm_by_conversation.keys())
    other_dm_students = {s.pk: s for s in Student.objects.filter(pk__in=other_dm_pks)}

    for other_pk, last_dm in latest_dm_by_conversation.items():
        other_student = other_dm_students.get(other_pk)
        if other_student:
            # For direct messages: is_read is True if the message was sent by current user OR if it's marked as read in DB
            if last_dm.sender == me:
                is_read = True  # User sent it, so it's "read"
            else:
                # User is the receiver, check the is_read field from database
                is_read = last_dm.is_read
            unified_conversations.append({
                'conversation_type': 'direct_chat',
                'conversation_target_id': other_student.kinde_user_id, # Use Kinde ID for direct chat
                'display_name': other_student.name,
                'display_avatar_url': other_student.profile_image.url if other_student.profile_image else None, # Add avatar URL field to Student model if exists
                'display_bio': other_student.bio,
                'last_message_text': last_dm.message,
                'last_message_image_url': last_dm.image_url,
                'last_message_timestamp': last_dm.timestamp,
                'last_message_sender_name': last_dm.sender.name if last_dm.sender == other_student else me.name,
                'last_message_type': get_sharable_type(last_dm),
                'is_read': is_read,
            })

   # --- 2. Corrected Get Community Chat Conversations ---
    # Step A: Get all communities the user is a member of
    member_communities = list(Communities.objects.filter(membership__user=me, membership__role__in=['admin', 'secondary_admin']).order_by('-membership__date_joined'))

    # Step B: Get ALL chat messages for ALL of those communities in one go
    community_pks = [c.pk for c in member_communities]
    all_community_messages = CommunityChatMessage.objects.filter(
        community_id__in=community_pks
    ).order_by('-sent_at').select_related('student', 'community').prefetch_related('read_by')

    # Step C: Use a Python dictionary to find the latest message for each community
    latest_comm_message_by_community = {}
    for message in all_community_messages:
        community_id = message.community_id
        if community_id not in latest_comm_message_by_community:
            # Since we ordered all_community_messages by '-sent_at', the first one we find is the latest
            latest_comm_message_by_community[community_id] = message

    for community in member_communities:
        last_comm_message_obj = latest_comm_message_by_community.get(community.pk)
        # For community messages: is_read is True if the message was sent by current user OR if user is in read_by
        if not last_comm_message_obj:
            is_read = True  # No messages, so nothing to read
        elif last_comm_message_obj.student_id == me.id:
            is_read = True  # User sent it, so it's "read"
        else:
            # User didn't send it, check if user is in read_by ManyToMany field from database
            is_read = last_comm_message_obj.read_by.filter(id=me.id).exists()
        unified_conversations.append({
            'conversation_type': 'community_chat',
            'conversation_target_id': str(community.pk),
            'display_name': community.community_name,
            'display_avatar_url': community.community_image.url if community.community_image else None,
            'display_bio': community.community_bio,
            'last_message_text': last_comm_message_obj.message if last_comm_message_obj else None,
            'last_message_image_url': last_comm_message_obj.image_url if last_comm_message_obj else None,
            'last_message_timestamp': last_comm_message_obj.sent_at if last_comm_message_obj else timezone.make_aware(datetime.min),
            'last_message_sender_name': last_comm_message_obj.student.name if last_comm_message_obj else None,
            'last_message_type': get_sharable_type(last_comm_message_obj) if last_comm_message_obj else "message",
            'is_read': is_read,
        })

    # --- 3. Get Group Chat Conversations ---
    member_groups = list(
        GroupChat.objects.filter(
            is_active=True,
            memberships__member=me,
        ).distinct().order_by('name')
    )
    group_pks = [g.pk for g in member_groups]
    all_group_messages = GroupChatMessage.objects.filter(
        group_id__in=group_pks
    ).order_by('-sent_at').select_related('student', 'group').prefetch_related('read_by')

    latest_group_message_by_group = {}
    for message in all_group_messages:
        gid = message.group_id
        if gid not in latest_group_message_by_group:
            latest_group_message_by_group[gid] = message

    for group in member_groups:
        last_group_message_obj = latest_group_message_by_group.get(group.pk)
        if not last_group_message_obj:
            is_read = True
        elif last_group_message_obj.student_id == me.id:
            is_read = True
        else:
            is_read = last_group_message_obj.read_by.filter(id=me.id).exists()
        unified_conversations.append({
            'conversation_type': 'group_chat',
            'conversation_target_id': str(group.pk),
            'display_name': group.name,
            'display_avatar_url': group.image.url if group.image else None,
            'display_bio': group.description,
            'last_message_text': last_group_message_obj.message if last_group_message_obj else None,
            'last_message_image_url': last_group_message_obj.image_url if last_group_message_obj else None,
            'last_message_timestamp': last_group_message_obj.sent_at if last_group_message_obj else timezone.make_aware(datetime.min),
            'last_message_sender_name': last_group_message_obj.student.name if last_group_message_obj else None,
            'last_message_type': get_sharable_type(last_group_message_obj) if last_group_message_obj else "message",
            'is_read': is_read,
        })

    # --- 4. Sort the Unified List by Latest Message Timestamp ---
    # datetime.min ensures conversations with no messages appear at the bottom
    unified_conversations.sort(key=lambda x: x['last_message_timestamp'], reverse=True)

    # 5. Serialize the unified list
    serializer = UnifiedConversationItemSerializer(unified_conversations, many=True, context=serializer_context)

    return JsonResponse({'status': 'success', 'conversations': serializer.data}, status=status.HTTP_200_OK)




@csrf_exempt
@parser_classes([MultiPartParser, FormParser]) # These are essential for receiving file uploads
async def test_upload_storage(request):
    """
    A temporary endpoint to test if file storage to GCS is working at all.
    This view does NOT require Kinde authentication.
    """
    if request.method != "POST":
        return JsonResponse({'status': 'error', 'message': 'Invalid method'}, status=405)

    if not request.FILES.get('image'):
        return JsonResponse({'status': 'error', 'message': 'No image file provided in "image" field.'}, status=400)
    
    try:
        # --- DEBUGGING STEP ---
        default_storage = DefaultStorage()
        logger.info(f"DEBUG: Active DEFAULT_FILE_STORAGE class: {default_storage.__class__.__name__}")
        logger.info(f"DEBUG: Storage backend details: {default_storage}")
        # --- END DEBUGGING STEP ---

        temp_image_instance = await database_sync_to_async(TempImage.objects.create)(
            image=request.FILES['image']
        )
        
        return JsonResponse({
            'status': 'success',
            'message': 'Test image uploaded successfully.',
            'temp_image_id': temp_image_instance.pk,
            'temp_image_url': temp_image_instance.image.url
        }, status=201)
    except Exception as e:
        logger.error(f"Storage test failed with exception: {e}")
        import traceback
        traceback.print_exc(file=sys.stdout) # Ensure traceback goes to stdout
        return JsonResponse({'status': 'error', 'message': f'Storage test failed: {str(e)}'}, status=500)

# ... (Rest of your views.py file) ...

# ... all imports from post_feed ...




#block/mute, works



@csrf_exempt
@kinde_auth_required
async def eventsfeed(request, kinde_user_id=None):
    """
    Retrieves a paginated, personalized, and trending-sorted feed of student and community events.
    Factors: Popularity, User Interests, Friend Activity, User Location.
    """
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        # Convert Student.objects.get to async
        student = await Student.objects.select_related('student_location__region').aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)

    # --- Fetch User-Specific Data for Scoring (async) ---
    
    # FIX: Use sync_to_async instead of async list comprehensions
    user_interest_ids = await sync_to_async(list)(student.student_interest.values_list('id', flat=True))
    
    # User's accepted friends' IDs - convert to async
    friendship_queryset = Friendship.objects.filter(
        Q(sender=student, status='accepted') | Q(receiver=student, status='accepted')
    ).annotate(
        friend_id=Case(
            When(sender=student, then=F('receiver_id')),
            default=F('sender_id'),
            output_field=IntegerField()
        )
    )
    # FIX: Use sync_to_async instead of async list comprehension
    accepted_friend_ids = await sync_to_async(list)(friendship_queryset.values_list('friend_id', flat=True))

    # User's location/region
    user_location_id = student.student_location.id if student.student_location else None
    user_region_id = student.student_location.region.id if student.student_location and student.student_location.region else None

    # If user has no location set, location scoring is skipped (all location_score = 0)
    # --- End User-Specific Data ---

    user_community_ids = await sync_to_async(list)(
        Membership.objects.filter(user=student).values_list('community_id', flat=True)
    )

    relationship_snapshot = await sync_to_async(get_relationship_snapshot)(student.id)
    blocked_student_ids = set(relationship_snapshot.get('blocking', [])) | set(relationship_snapshot.get('blocked_by', []))
    muted_student_ids = set(relationship_snapshot.get('muted_students', []))
    muted_community_ids = set(relationship_snapshot.get('muted_communities', []))
    community_that_blocked_me_ids = set(relationship_snapshot.get('blocked_by_communities', []))

    blocked_student_ids = list(blocked_student_ids)
    muted_student_ids = list(muted_student_ids)
    muted_community_ids = list(muted_community_ids)
    community_that_blocked_me_ids = list(community_that_blocked_me_ids)
        
    # --- Pagination Parameters ---
    limit, offset = _parse_pagination_params(request)

    # Get student IDs with pending deletion requests (to exclude their content)
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()

    # --- Base Query for Student Events ---
    #student_events_queryset = Student_Events.objects.filter(dateposted__gte=recent_date).select_related(
    student_events_queryset = Student_Events.objects.all().select_related(
        'student', # Host of the event
        'student__student_location',
        'student__student_location__region'
    ).prefetch_related(
        Prefetch('student_events_discussion_set'), # For discussion_count
        Prefetch('eventrsvp'), # For RSVP counts (going, interested, not_going)
        # Prefetch interests of the host
        Prefetch('student__student_interest'),
        Prefetch('images', queryset=Student_Events_Image.objects.only('id', 'created_at', 'image')),
        Prefetch('videos', queryset=Student_Events_Video.objects.only('id', 'created_at', 'video')),
        Prefetch('bookmarkedstudentevents_set'),
        # FIX: Add prefetching for mentions to avoid additional queries
        Prefetch('student_mentions'),
        Prefetch('community_mentions')
    ).exclude(
        Q(student__id__in=blocked_student_ids) | 
        Q(student__id__in=muted_student_ids) |
        Q(student__id__in=pending_deletion_ids)  # Exclude events from users with pending deletion
    ).annotate(
        rsvp_count=Count('eventrsvp', distinct=True),
        discussion_count=Count('student_events_discussion', distinct=True)
    ).annotate(
        # Score Components for Student Events
        **get_popularity_score_annotations('date', rsvp_related_name='eventrsvp', like_related_name=None, comment_related_name='student_events_discussion_set'),
        **get_interest_overlap_annotations(user_interest_ids, 'student__student_interest'),
        **get_friend_activity_annotations(accepted_friend_ids, None, 'eventrsvp'),
        **get_location_match_annotations(user_region_id, 'student__student_location'),
        **get_author_friend_annotations(accepted_friend_ids, 'student'),
    ).annotate(
        final_score=ExpressionWrapper(
            (F('popularity_score') * W_POPULARITY) +
            (F('interest_match_score') * W_INTEREST) +
            (F('friend_activity_score') * W_FRIEND_ACTIVITY) +
            (F('location_score') * W_LOCATION) +
            (F('author_friend_score') * W_AUTHOR_FRIEND),
            output_field=fields.FloatField()
        )
    ).order_by(
        '-final_score',
        '-date'
    )[offset:offset + limit]

    # --- Base Query for Community Events ---
    #community_events_queryset = Community_Events.objects.filter(dateposted__gte=recent_date).select_related(
    community_events_queryset = Community_Events.objects.all().select_related(
        'community', # The community hosting the event
        'community__location',
        'community__location__region',
        'poster' # The student who posted the event
    ).prefetch_related(
        Prefetch('community_events_discussion_set'), # For discussion_count
        Prefetch('communityeventrsvp'),
        Prefetch('bookmarkedcommunityevents_set'),

        # Prefetch interests of the community
        Prefetch('community__community_interest'),
        Prefetch('images', queryset=Community_Events_Image.objects.only('id', 'created_at', 'image')),
        Prefetch('videos', queryset=Community_Events_Video.objects.only('id', 'created_at', 'video')),
        # FIX: Add prefetching for mentions
        Prefetch('student_mentions'),
        Prefetch('community_mentions')
    ).exclude(
        Q(poster__id__in=blocked_student_ids) | 
        Q(poster__id__in=muted_student_ids) | 
        Q(community__id__in=muted_community_ids) | 
        Q(community__id__in=community_that_blocked_me_ids) |
        Q(poster__id__in=pending_deletion_ids)  # Exclude community events from users with pending deletion
    ).annotate(
        rsvp_count=Count('communityeventrsvp', distinct=True),
        discussion_count=Count('community_events_discussion', distinct=True)
    ).annotate(
        # Score Components for Community Events
        **get_popularity_score_annotations('date', rsvp_related_name='communityeventrsvp', like_related_name=None, comment_related_name='community_events_discussion_set'),
        **get_interest_overlap_annotations(user_interest_ids, 'community__community_interest'),
        **get_friend_activity_annotations(accepted_friend_ids, None, 'communityeventrsvp'),
        **get_location_match_annotations(user_region_id, 'community__location'),
        **get_community_membership_annotations(user_community_ids, 'community'),
    ).annotate(
        final_score=ExpressionWrapper(
            (F('popularity_score') * W_POPULARITY) +
            (F('interest_match_score') * W_INTEREST) +
            (F('friend_activity_score') * W_FRIEND_ACTIVITY) +
            (F('location_score') * W_LOCATION) +
            (F('community_member_score') * W_MEMBER_COMMUNITY),
            output_field=fields.FloatField()
        )
    ).order_by(
        '-final_score',
        '-date'
    )[offset:offset + limit]

    # --- Targeted Advertisements (by user location and region) ---
    ads_queryset = Advertisements.objects.filter(
        is_active=True
    ).filter(
        Q(ad_locations=user_location_id) | Q(ad_regions=user_region_id)
    )

    # --- Execute Queries Concurrently ---
    # Convert querysets to lists asynchronously and concurrently
    student_events_list, community_events_list, ads_list = await asyncio.gather(
        sync_to_async(list)(student_events_queryset),
        sync_to_async(list)(community_events_queryset),
        sync_to_async(list)(ads_queryset)
    )

    # --- Serialize Results ---
    serializer_context = {'kinde_user_id': kinde_user_id, 'request': request}
    
    # Run serialization in sync_to_async since DRF serializers are sync
    student_event_serializer_data, community_event_serializer_data = await asyncio.gather(
        sync_to_async(lambda: StudentEventSerializer(student_events_list, many=True, context=serializer_context).data)(),
        sync_to_async(lambda: CommunityEventsSerializer(community_events_list, many=True, context=serializer_context).data)()
    )

    # --- Serialize ads ---
    ads_data = [
        {
            'id': ad.id,
            'company_name': ad.company_name,
            'ad_header': ad.ad_header,
            'ad_body': ad.ad_body,
            'ad_image_url': (ad.ad_media.url if getattr(ad, 'ad_media', None) else None),
            'ad_link': ad.ad_link,
        }
        for ad in ads_list
    ]

    # --- Build mixed feed with periodic ads ---
    for item in student_event_serializer_data:
        item['kind'] = 'student_event'
    for item in community_event_serializer_data:
        item['kind'] = 'community_event'

    mixed_events = []
    i, j = 0, 0
    while i < len(student_event_serializer_data) or j < len(community_event_serializer_data):
        if i < len(student_event_serializer_data):
            mixed_events.append(student_event_serializer_data[i])
            i += 1
        if j < len(community_event_serializer_data):
            mixed_events.append(community_event_serializer_data[j])
            j += 1

    # Prepare ads with kind
    prepared_ads = []
    for ad in ads_data:
        ad_with_kind = dict(ad)
        ad_with_kind['kind'] = 'ad'
        prepared_ads.append(ad_with_kind)

    # Inject an ad after every 4 items
    if prepared_ads:
        injected_feed = []
        count = 0
        ad_idx = 0
        for item in mixed_events:
            injected_feed.append(item)
            count += 1
            if count % 4 == 0:
                injected_feed.append(prepared_ads[ad_idx])
                ad_idx = (ad_idx + 1) % len(prepared_ads)
    else:
        injected_feed = mixed_events

    return JsonResponse({
        'feed': injected_feed,
        'next_offset_student_events': offset + limit if len(student_events_list) == limit else None,
        'next_offset_community_events': offset + limit if len(community_events_list) == limit else None
    }, status=status.HTTP_200_OK)
#block/mute, works
@csrf_exempt
@kinde_auth_required
async def post_feed(request, kinde_user_id=None):
    """
    Retrieves a paginated, personalized, and trending-sorted feed of posts and community posts.
    Factors: Popularity, User Interests, Friend Activity, User Location.
    """
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        # Convert Student.objects.get to async
        student = await Student.objects.select_related('student_location__region').aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)

    # --- Fetch User-Specific Data for Scoring (async) ---
    
    # FIX: Use sync_to_async instead of async list comprehensions
    user_interest_ids = await sync_to_async(list)(student.student_interest.values_list('id', flat=True))
    
    # User's accepted friends' IDs - convert to async
    friendship_queryset = Friendship.objects.filter(
        Q(sender=student, status='accepted') | Q(receiver=student, status='accepted')
    ).annotate(
        friend_id=Case(
            When(sender=student, then=F('receiver_id')),
            default=F('sender_id'),
            output_field=IntegerField()
        )
    )
    # FIX: Use sync_to_async instead of async list comprehension
    accepted_friend_ids = await sync_to_async(list)(friendship_queryset.values_list('friend_id', flat=True))

    # User's location/region
    user_location_id = student.student_location.id if student.student_location else None
    user_region_id = student.student_location.region.id if student.student_location and student.student_location.region else None
    
    # If user has no location set, location scoring is skipped (all location_score = 0)
    # --- End User-Specific Data ---

    user_community_ids = await sync_to_async(list)(
        Membership.objects.filter(user=student).values_list('community_id', flat=True)
    )

    relationship_snapshot = await sync_to_async(get_relationship_snapshot)(student.id)
    blocked_student_ids = set(relationship_snapshot.get('blocking', [])) | set(relationship_snapshot.get('blocked_by', []))
    muted_student_ids = set(relationship_snapshot.get('muted_students', []))
    muted_community_ids = set(relationship_snapshot.get('muted_communities', []))
    community_that_blocked_me_ids = set(relationship_snapshot.get('blocked_by_communities', []))

    blocked_student_ids = list(blocked_student_ids)
    muted_student_ids = list(muted_student_ids)
    muted_community_ids = list(muted_community_ids)
    community_that_blocked_me_ids = list(community_that_blocked_me_ids)

    # --- Pagination Parameters ---
    limit, offset = _parse_pagination_params(request)

    # Get student IDs with pending deletion requests (to exclude their content)
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()

    # --- Base Query for Regular Posts ---
    # CRITICAL OPTIMIZATION: Limit to recent posts (last 90 days) before expensive scoring calculations
    # from django.utils import timezone
    # from datetime import timedelta
    # recent_date = timezone.now() - timedelta(days=90)
    
    # Apply all scoring annotations first
    #posts_queryset = Posts.objects.filter(post_date__gte=recent_date).select_related(
    posts_queryset = Posts.objects.all().select_related(
        'student', # Fetch the related Student object
        'student__student_location', # Fetch student's location
        'student__student_location__region' # Fetch student's region
    ).prefetch_related(
        # Optimize related counts needed by serializer
        Prefetch('likes'), # For like_count and is_liked
        Prefetch('comments'), # For comment_count
        Prefetch('bookmarkedposts_set'), # For isBookmarked
        # Prefetch interests of the poster (for interest matching)
        Prefetch('student__student_interest'),
        Prefetch('images', queryset=PostImages.objects.only('id', 'created_at', 'image')),
        Prefetch('videos', queryset=PostVideos.objects.only('id', 'created_at', 'video')), # Prefetch PostVideos objects
        # FIX: Add prefetching for mentions to avoid additional queries
        Prefetch('student_mentions'),
        Prefetch('community_mentions')
    ).exclude(
        Q(student__id__in=blocked_student_ids) | 
        Q(student__id__in=muted_student_ids) |
        Q(student__id__in=pending_deletion_ids)  # Exclude posts from users with pending deletion
    ).annotate(
        like_count=Count('likes', distinct=True),
        comment_count=Count('comments', distinct=True)
    ).annotate(
        # Unpack the dictionaries returned by scoring functions
        **get_popularity_score_annotations('post_date', 'likes', 'comments'),
        **get_interest_overlap_annotations(user_interest_ids, 'student__student_interest'),
        **get_friend_activity_annotations(accepted_friend_ids, 'likes', None),
        **get_location_match_annotations(user_region_id, 'student__student_location'),
        **get_author_friend_annotations(accepted_friend_ids, 'student'),
    ).annotate(
        final_score=ExpressionWrapper(
            (F('popularity_score') * W_POPULARITY) +
            (F('interest_match_score') * W_INTEREST) +
            (F('friend_activity_score') * W_FRIEND_ACTIVITY) +
            (F('location_score') * W_LOCATION) +
            (F('author_friend_score') * W_AUTHOR_FRIEND),
            output_field=fields.FloatField()
        )
    ).order_by(
        '-final_score',
        '-post_date'
    )[offset:offset + limit]

    # --- Base Query for Community Posts ---
    # Apply all scoring annotations
    #cposts_queryset = Community_Posts.objects.filter(post_date__gte=recent_date).select_related(
    cposts_queryset = Community_Posts.objects.all().select_related(
        'community', # Fetch the related Community object
        'community__location', # Fetch community's location
        'community__location__region', # Fetch community's region
        'poster' # Fetch the poster Student object
    ).prefetch_related(
        # Optimize related counts needed by serializer
        Prefetch('likecommunitypost_set'), # For like_count
        Prefetch('community_posts_comment_set'), # For comment_count
        # Prefetch interests of the community (for interest matching)
        Prefetch('community__community_interest'),
        Prefetch('bookmarkedcommunityposts_set'),
        Prefetch('images', queryset=Community_Posts_Image.objects.only('id', 'created_at', 'image')),
        Prefetch('videos', queryset=Community_Posts_Video.objects.only('id', 'created_at', 'video')),
        # FIX: Add prefetching for mentions
        Prefetch('student_mentions'),
        Prefetch('community_mentions')
    ).exclude(
        Q(poster__id__in=blocked_student_ids) | 
        Q(poster__id__in=muted_student_ids) | 
        Q(community__id__in=muted_community_ids) | 
        Q(community__id__in=community_that_blocked_me_ids) |
        Q(poster__id__in=pending_deletion_ids)  # Exclude community posts from users with pending deletion
    ).annotate(
        like_count=Count('likecommunitypost', distinct=True),
        comment_count=Count('community_posts_comment', distinct=True)
    ).annotate(
        # Unpack the dictionaries returned by scoring functions
        **get_popularity_score_annotations('post_date', 'likecommunitypost', 'community_posts_comment'),
        **get_interest_overlap_annotations(user_interest_ids, 'community__community_interest'),
        **get_friend_activity_annotations(accepted_friend_ids, 'likecommunitypost', None),
        **get_location_match_annotations(user_region_id, 'community__location'),
        **get_community_membership_annotations(user_community_ids, 'community'),
    ).annotate(
        final_score=ExpressionWrapper(
            (F('popularity_score') * W_POPULARITY) +
            (F('interest_match_score') * W_INTEREST) +
            (F('friend_activity_score') * W_FRIEND_ACTIVITY) +
            (F('location_score') * W_LOCATION) +
            (F('community_member_score') * W_MEMBER_COMMUNITY),
            output_field=fields.FloatField()
        )
    ).order_by(
        '-final_score',
        '-post_date'
    )[offset:offset + limit]

    # --- Targeted Advertisements (by user location and region) ---
    ads_queryset = Advertisements.objects.filter(
        is_active=True
    ).filter(
        Q(ad_locations=user_location_id) | Q(ad_regions=user_region_id)
    )

    # --- Execute Queries Concurrently ---
    # Convert querysets to lists asynchronously and concurrently
    posts_list, cposts_list, ads_list = await asyncio.gather(
        sync_to_async(list)(posts_queryset),
        sync_to_async(list)(cposts_queryset),
        sync_to_async(list)(ads_queryset)
    )

    # --- Serialize Results ---
    serializer_context = {'kinde_user_id': kinde_user_id, 'request': request}
    
    # Run serialization in sync_to_async since DRF serializers are sync
    post_serializer_data, community_post_serializer_data = await asyncio.gather(
        sync_to_async(lambda: PostSerializer(posts_list, many=True, context=serializer_context).data)(),
        sync_to_async(lambda: CommunityPostSerializer(cposts_list, many=True, context=serializer_context).data)()
    )

    # --- Serialize ads ---
    ads_data = [
        {
            'id': ad.id,
            'company_name': ad.company_name,
            'ad_header': ad.ad_header,
            'ad_body': ad.ad_body,
            'ad_image_url': (ad.ad_media.url if getattr(ad, 'ad_media', None) else None),
            'ad_link': ad.ad_link,
        }
        for ad in ads_list
    ]

    # --- Build mixed feed with periodic ads ---
    for item in post_serializer_data:
        item['kind'] = 'post'
    for item in community_post_serializer_data:
        item['kind'] = 'community_post'

    mixed_posts = []
    i, j = 0, 0
    while i < len(post_serializer_data) or j < len(community_post_serializer_data):
        if i < len(post_serializer_data):
            mixed_posts.append(post_serializer_data[i])
            i += 1
        if j < len(community_post_serializer_data):
            mixed_posts.append(community_post_serializer_data[j])
            j += 1

    # Prepare ads with kind
    prepared_ads = []
    for ad in ads_data:
        ad_with_kind = dict(ad)
        ad_with_kind['kind'] = 'ad'
        prepared_ads.append(ad_with_kind)

    # Inject an ad after every 4 items
    if prepared_ads:
        injected_feed = []
        count = 0
        ad_idx = 0
        for item in mixed_posts:
            injected_feed.append(item)
            count += 1
            if count % 4 == 0:
                injected_feed.append(prepared_ads[ad_idx])
                ad_idx = (ad_idx + 1) % len(prepared_ads)
    else:
        injected_feed = mixed_posts

    return JsonResponse({
        'feed': injected_feed,
        'next_offset_posts': offset + limit if len(posts_list) == limit else None, # For client to request next page
        'next_offset_community_posts': offset + limit if len(cposts_list) == limit else None
    }, status=status.HTTP_200_OK)






@api_view(['GET'])
@kinde_auth_required
def get_community_of_students_where_admin(request, kinde_user_id=None):
    """
    Returns a list of communities where the authenticated user is an admin or secondary admin.
    """
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
         
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)

    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    communities_qs = (
        Communities.objects.filter(
        membership__user=student,
        membership__role__in=['admin', 'secondary_admin']
        )
        .select_related('location')
        .prefetch_related('community_interest', 'student_mentions', 'community_mentions')
        .distinct()
        .order_by('-membership__date_joined', '-id')
    )

    total_count = communities_qs.count()

    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'communities': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=status.HTTP_200_OK)

    communities = list(communities_qs[offset:offset + limit])
    community_ids = [community.id for community in communities]

    if community_ids:
        community_id_set = set(community_ids)

        user_memberships = set(
            Membership.objects.filter(
                user=student,
                community_id__in=community_ids
            ).values_list('community_id', flat=True)
        )

        relationship_snapshot = get_relationship_snapshot(student.id)
        all_muted_communities = set(relationship_snapshot.get('muted_communities', []))
        all_blocked_by_communities = set(relationship_snapshot.get('blocked_by_communities', []))
        user_community_roles = dict(
            Membership.objects.filter(
                user=student,
                community_id__in=community_ids
            ).values_list('community_id', 'role')
        )

        user_muted_communities = all_muted_communities & community_id_set
        user_blocked_by_communities = all_blocked_by_communities & community_id_set

        all_memberships = Membership.objects.filter(
            community_id__in=community_ids
        ).select_related('user', 'community')
    else:
        user_memberships = set()
        user_community_roles = {}
        user_muted_communities = set()
        user_blocked_by_communities = set()
        all_memberships = Membership.objects.none()

    # Add the bulk data to serializer context
    serializer_context = {
        'request': request, 
        'kinde_user_id': kinde_user_id,
        'user_memberships': user_memberships,
        'user_community_roles': user_community_roles,
        'user_muted_communities': user_muted_communities,
        'user_blocked_by_communities': user_blocked_by_communities
    }
    
    serializer = CommunitySerializer(communities, many=True, context=serializer_context)
    next_offset = offset + limit if (offset + limit) < total_count else None
     
    return JsonResponse({
        'status': 'success',
        'communities': serializer.data,
        'count': len(serializer.data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=status.HTTP_200_OK)



async def _get_direct_chat_matches(me, query):
    """Get direct chat partners matching the search query."""
    # Optimized query to get unique chat partners with message history
    other_user_pks = await sync_to_async(list)(
        DirectMessage.objects.filter(
            Q(sender=me) | Q(receiver=me)
        ).values_list(
            Case(
                When(sender=me, then=F('receiver_id')),
                default=F('sender_id'),
                output_field=IntegerField()
            ),
            flat=True
        ).distinct()
    )
    
    if not other_user_pks:
        return []
    
    # Filter users by search query with case-insensitive search
    return await sync_to_async(list)(
        Student.objects.filter(
            pk__in=other_user_pks
        ).filter(
            Q(name__icontains=query) | 
            Q(username__icontains=query)
        ).select_related('student_location')
        .only('kinde_user_id', 'name', 'username', 'bio', 'avatar_url', 'is_online')
        .order_by('name')
    )


async def _get_community_chat_matches(me, query):
    """Get community chats matching the search query."""
    # Get communities the user is a member of that have messages
    community_pks_with_messages = await sync_to_async(list)(
        CommunityChatMessage.objects.filter(
            community__membership__user=me
        ).values_list('community_id', flat=True).distinct()
    )
    
    if not community_pks_with_messages:
        return []
    
    # Filter communities by search query
    return await sync_to_async(list)(
        Communities.objects.filter(
            pk__in=community_pks_with_messages
        ).filter(
            Q(community_name__icontains=query) | 
            Q(community_bio__icontains=query)
        ).annotate(
            member_count=Count('membership')
        ).only('pk', 'community_name', 'community_bio', 'avatar_url')
        .order_by('community_name'))


    

@csrf_exempt
@kinde_auth_required
async def search_chatlist(request, kinde_user_id=None):
    """
    Search the user's chat list (direct and community chats) by name or community name.
    """
    query = request.GET.get('q', '').strip()
    
    if not kinde_user_id:
        return JsonResponse({
            'status': 'error', 
            'message': 'Authentication failed: Kinde User ID not provided.'
        }, status=status.HTTP_401_UNAUTHORIZED)
    
    if not query:
        return JsonResponse({
            'status': 'error', 
            'message': 'No search query provided.'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error', 
            'message': 'Authenticated user not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    
    try:
        # --- Direct Chat Partners ---
        # Get user IDs that the current user has exchanged messages with
        other_user_pks = await sync_to_async(list)(
            DirectMessage.objects.filter(
                Q(sender=me) | Q(receiver=me)
            ).values_list(
                Case(
                    When(sender=me, then=F('receiver_id')),
                    default=F('sender_id'),
                    output_field=IntegerField()
                ),
                flat=True
            ).distinct()
        )
        
        # Filter those users by search query
        if other_user_pks:
            direct_chat_users = await sync_to_async(list)(
                Student.objects.filter(
                    pk__in=other_user_pks
                ).filter(
                    Q(name__icontains=query) | Q(username__icontains=query)
                ).select_related('student_location').order_by('name')
            )
        else:
            direct_chat_users = []
        
        # --- Community Chats ---
        # Get community IDs where user is a member and has messages
        community_pks_with_messages = await sync_to_async(list)(
            CommunityChatMessage.objects.filter(
                community__membership__user=me
            ).values_list('community_id', flat=True).distinct()
        )
        
        # Filter those communities by search query
        if community_pks_with_messages:
            communities = await sync_to_async(list)(
                Communities.objects.filter(
                    pk__in=community_pks_with_messages
                ).filter(
                    Q(community_name__icontains=query) | Q(community_bio__icontains=query)
                ).order_by('community_name')
            )
        else:
            communities = []
        
        # Build results list
        chatlist = []
        
        for user in direct_chat_users:
            chatlist.append({
                'type': 'direct_chat',
                'target_id': user.kinde_user_id,
                'display_name': user.name,
                'display_username': user.username,
                'display_bio': user.bio or '',
            })
        
        for community in communities:
            chatlist.append({
                'type': 'community_chat',
                'target_id': str(community.pk),
                'display_name': community.community_name,
                'display_bio': community.community_bio or '',
            })
        
        # Sort alphabetically by display name
        chatlist.sort(key=lambda x: x['display_name'].lower())
        
        return JsonResponse({
            'status': 'success', 
            'results': chatlist
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error in search_chatlist: {str(e)}", exc_info=True)
        return JsonResponse({
            'status': 'error', 
            'message': 'An error occurred while searching.'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['POST'])  # Changed to POST - more appropriate for notifications
@kinde_auth_required
def notify_user(request, kinde_user_id=None):
    """Send a test notification to a specific token"""
    try:
        token = request.data.get("token")
        title = request.data.get("title", "Hello from Django!")
        body = request.data.get("body", "This is a test notification")
        
        if not token:
            return JsonResponse({"error": "Token is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        # Verify token exists and is active
        try:
            device_token = DeviceToken.objects.get(token=token, is_active=True)
        except DeviceToken.DoesNotExist:
            return JsonResponse({"error": "Invalid or inactive token"}, status=status.HTTP_400_BAD_REQUEST)
        
        success = send_push_notification(token, title, body)
        
        if success:
            return JsonResponse({"status": "success", "message": "Notification sent"}, status=status.HTTP_200_OK)
        else:
            return JsonResponse({"status": "error", "message": "Failed to send notification"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
    except Exception as e:
        logger.error(f"Error sending notification: {str(e)}")
        return JsonResponse({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['POST'])
@kinde_auth_required
def save_device_token(request, kinde_user_id=None):
    """
    Save or update device token for the authenticated user.
    Ensures only one active token per user and deactivates any existing tokens with the same value.
    """
    try:
        token = request.data.get("token")
        device_type = request.data.get("device_type", "android")
        device_id = request.data.get("device_id")  # Optional unique device identifier
        device_name = request.data.get("device_name")  # Optional device name
        
        if not token:
            return JsonResponse({"error": "Token is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        if not kinde_user_id:
            return JsonResponse({"error": "Authentication failed"}, status=status.HTTP_401_UNAUTHORIZED)
        
        try:
            # Only fetch the fields we actually need for this operation
            user = Student.objects.only("id").get(kinde_user_id=kinde_user_id)
        except Student.DoesNotExist:
            return JsonResponse({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
        
        # Use atomic transaction to ensure data consistency
        with transaction.atomic():
            # Step 1: Deactivate ALL tokens with this exact token value (security measure)
            # This prevents the same token from being active for multiple users
            deactivated_count = DeviceToken.objects.filter(
                token=token,
                is_active=True
            ).exclude(user=user).update(is_active=False)
            
            if deactivated_count > 0:
                logger.info(
                    "Deactivated %s token(s) with value %s... that belonged to other users",
                    deactivated_count,
                    token[:20],
                )
            
            # Step 2: Deactivate all other active tokens for this user (only one active token per user)
            # This ensures the user only has one active device token at a time
            other_tokens_deactivated = DeviceToken.objects.filter(
                user=user,
                is_active=True
            ).exclude(token=token).update(is_active=False)
            
            if other_tokens_deactivated > 0:
                logger.info(f"Deactivated {other_tokens_deactivated} other active token(s) for user {user.id}")
            
            # Step 3: If this exact token is already active for this user, avoid a redundant write
            existing_active = DeviceToken.objects.filter(
                user=user,
                token=token,
                is_active=True,
            ).first()

            if existing_active:
                logger.debug(
                    "Device token already active for user %s; skipping redundant write (token_id=%s)",
                    user.id,
                    existing_active.id,
                )
                return JsonResponse(
                    {
                        "message": "Token already active",
                        "token_id": existing_active.id,
                        "action": "unchanged",
                        "is_active": True,
                    },
                    status=status.HTTP_200_OK,
                )
            
            # Step 4: Get or create the token for this user
            # If token exists for this user, update it; otherwise create new
            device_token, created = DeviceToken.objects.update_or_create(
                token=token,
                user=user,
                defaults={
                    'device_type': device_type,
                    'device_id': device_id,
                    'device_name': device_name,
                    'is_active': True,
                    'updated_at': timezone.now()
                }
            )
            
            action = "created" if created else "updated"
            logger.info(
                "Device token %s for user %s: token_id=%s, device_type=%s",
                action,
                user.id,
                device_token.id,
                device_type,
            )
            
            return JsonResponse({
                "message": f"Token {action} successfully",
                "token_id": device_token.id,
                "action": action,
                "is_active": device_token.is_active
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error saving device token: {str(e)}", exc_info=True)
        return JsonResponse({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['DELETE'])
@kinde_auth_required
def remove_device_token(request, kinde_user_id=None):
    """Remove/deactivate device token"""
    try:
        token = request.data.get("token")
        
        if not token:
            return JsonResponse({"error": "Token is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        user = Student.objects.get(kinde_user_id=kinde_user_id)
        
        # Deactivate the token instead of deleting (better for analytics)
        updated_count = DeviceToken.objects.filter(
            user=user, 
            token=token
        ).update(is_active=False)
        
        if updated_count > 0:
            return JsonResponse({"message": "Token removed successfully"}, status=status.HTTP_200_OK)
        else:
            return JsonResponse({"error": "Token not found"}, status=status.HTTP_404_NOT_FOUND)
            
    except Student.DoesNotExist:
        return JsonResponse({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error removing device token: {str(e)}")
        return JsonResponse({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# FCM helper function (you'll need to implement this)
# Additional utility functions for multi-device support

@api_view(['GET'])
@kinde_auth_required
def get_user_devices(request, kinde_user_id=None):
    """Get all active devices for the authenticated user"""
    try:
        user = Student.objects.get(kinde_user_id=kinde_user_id)
        devices = DeviceToken.objects.filter(user=user, is_active=True).order_by('-updated_at')
        
        device_data = []
        for device in devices:
            device_data.append({
                'id': device.id,
                'device_type': device.device_type,
                'device_name': device.device_name or f"{device.device_type.title()} Device",
                'device_id': device.device_id,
                'created_at': device.created_at,
                'updated_at': device.updated_at,
                'token': device.token[:20] + "..." if device.token else None  # Partial token for security
            })
        
        return JsonResponse({
            "devices": device_data,
            "total_devices": len(device_data)
        }, status=status.HTTP_200_OK)
        
    except Student.DoesNotExist:
        return JsonResponse({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error getting user devices: {str(e)}")
        return JsonResponse({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['POST'])
@kinde_auth_required
def notify_all_user_devices(request, kinde_user_id=None):
    """Send notification to all active devices of a user"""
    try:
        target_kinde_user_id = request.data.get("target_user_id", kinde_user_id)  # Default to self
        title = request.data.get("title", "Notification")
        body = request.data.get("body", "You have a new notification")
        data = request.data.get("data", {})
        
        target_user = Student.objects.get(kinde_user_id=target_kinde_user_id)
        active_tokens = DeviceToken.objects.filter(user=target_user, is_active=True)
        
        if not active_tokens.exists():
            return JsonResponse({
                "status": "warning", 
                "message": "No active devices found for user"
            }, status=status.HTTP_200_OK)
        
        successful_sends = 0
        failed_sends = 0
        
        for device_token in active_tokens:
            success = send_push_notification(device_token.token, title, body, data)
            if success:
                successful_sends += 1
            else:
                failed_sends += 1
        
        return JsonResponse({
            "status": "completed",
            "message": f"Notification sent to {successful_sends}/{active_tokens.count()} devices",
            "successful_sends": successful_sends,
            "failed_sends": failed_sends,
            "total_devices": active_tokens.count()
        }, status=status.HTTP_200_OK)
        
    except Student.DoesNotExist:
        return JsonResponse({"error": "Target user not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error sending notifications to all devices: {str(e)}")
        return JsonResponse({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['POST'])
@kinde_auth_required
def sign_out_user(request, kinde_user_id=None):
    """Deactivate all device tokens for the authenticated user when they sign out"""
    try:
        if not kinde_user_id:
            return JsonResponse({"error": "Authentication failed"}, status=status.HTTP_401_UNAUTHORIZED)
        
        user = Student.objects.get(kinde_user_id=kinde_user_id)
        
        # Option 1: Deactivate all tokens for this user (recommended)
        deactivated_count = DeviceToken.objects.filter(
            user=user, 
            is_active=True
        ).update(is_active=False)
        
        # Option 2: Only deactivate the current device token (if provided)
        current_token = request.data.get("token")
        if current_token:
            DeviceToken.objects.filter(
                user=user, 
                token=current_token
            ).update(is_active=False)
            
            return JsonResponse({
                "message": "Current device token deactivated successfully"
            }, status=status.HTTP_200_OK)
        
        return JsonResponse({
            "message": f"All {deactivated_count} device tokens deactivated successfully"
        }, status=status.HTTP_200_OK)
        
    except Student.DoesNotExist:
        return JsonResponse({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error during sign out: {str(e)}")
        return JsonResponse({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



@api_view(['GET'])
@kinde_auth_required
def get_pending_friend_requests(request, kinde_user_id=None):
    """Retrieve pending friend requests for the authenticated user"""
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
    
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = _get_pending_deletion_student_ids()

    pending_qs = (
        Friendship.objects.filter(receiver=student, status='pending')
        .exclude(sender__id__in=pending_deletion_ids)
        .select_related('sender', 'sender__university', 'sender__student_location')
        .prefetch_related(
            'sender__student_interest',
            Prefetch('sender__membership_set', queryset=Membership.objects.select_related('community')),
        )
        .order_by('-created_at')
    )

    total_count = pending_qs.count()
    pending_requests = list(pending_qs[offset:offset + limit])

    # Get requester's friends for mutual friends calculation (single query)
    requester_friends = set(
        Student.objects.filter(
            Q(sent_requests__receiver=student, sent_requests__status='accepted') |
            Q(received_requests__sender=student, received_requests__status='accepted')
        ).distinct().values_list('id', flat=True)
    )

    # Bulk: get friends of each pending sender (one query), then compute mutual in Python
    mutual_friends_data = {}
    if pending_requests:
        sender_ids = [f.sender_id for f in pending_requests]
        # All accepted friendships where sender or receiver is one of our senders
        friendships_for_senders = Friendship.objects.filter(
            status='accepted'
        ).filter(
            Q(sender_id__in=sender_ids) | Q(receiver_id__in=sender_ids)
        ).values_list('sender_id', 'receiver_id')

        sender_friends_map = {sid: set() for sid in sender_ids}
        for sid, rid in friendships_for_senders:
            if sid in sender_ids and rid != sid:
                sender_friends_map[sid].add(rid)
            if rid in sender_ids and sid != rid:
                sender_friends_map[rid].add(sid)

        all_mutual_ids = set()
        sender_first_mutual = {}
        for sid in sender_ids:
            mutual = requester_friends & sender_friends_map.get(sid, set())
            if mutual:
                first_id = next(iter(mutual))
                sender_first_mutual[sid] = first_id
                all_mutual_ids.add(first_id)

        if all_mutual_ids:
            first_mutual_students = {
                s.id: {
                    'id': s.id,
                    'kinde_user_id': s.kinde_user_id,
                    'name': s.name,
                    'username': getattr(s, 'username', ''),
                    'bio': getattr(s, 'bio', ''),
                    'profile_image': s.profile_image.url if s.profile_image else None,
                    'is_online': getattr(s, 'is_online', False),
                }
                for s in Student.objects.filter(id__in=all_mutual_ids).only(
                    'id', 'kinde_user_id', 'name', 'username', 'bio', 'profile_image'
                )
            }
            for sid in sender_ids:
                first_id = sender_first_mutual.get(sid)
                if first_id and first_id in first_mutual_students:
                    mutual_friends_data[sid] = [first_mutual_students[first_id]]

    relationship_snapshot = get_relationship_snapshot(student.id)
    user_muted_student_ids = set(relationship_snapshot.get('muted_students', []))
    user_blocked_student_ids = set(relationship_snapshot.get('blocking', []))

    serializer_context = {
        'kinde_user_id': kinde_user_id,
        'request': request,
        'student_id': student.id,
        'mutual_friends': mutual_friends_data,
        'user_muted_student_ids': user_muted_student_ids,
        'user_blocked_student_ids': user_blocked_student_ids,
    }
    serializer = FriendshipSerializer(pending_requests, many=True, context=serializer_context)

    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'pending_friend_requests': serializer.data,
        'count': len(serializer.data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@kinde_auth_required
def accept_friendship_request(request, kinde_user_id=None):
    """Accept a friend request"""

    friendship_request_id = request.data.get('friend_request_id')
    
    
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
    
    try:
        friendship_request = Friendship.objects.get(id=friendship_request_id, receiver=student, status='pending')
    except Friendship.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Friend request not found or already processed.'}, status=status.HTTP_404_NOT_FOUND)
    
    # Accept the friend request
    friendship_request.status = 'accepted'
    friendship_request.save()
    
    # Optionally, create a reciprocal friendship entry

    
    serializer_context = {'kinde_user_id': kinde_user_id, 'request': request}
    serializer = FriendshipSerializer(friendship_request, context=serializer_context)
    
    return JsonResponse({
        'status': 'success',
        'message': 'Friend request accepted.',
        'friendship': serializer.data
    }, status=status.HTTP_200_OK)

@api_view(['POST'])
@kinde_auth_required
def decline_friendship_request(request, kinde_user_id=None):
    """Decline a friend request"""
    friendship_request_id = request.data.get('friend_request_id')

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        friendship_request = Friendship.objects.get(id=friendship_request_id, receiver=student, status='pending')
    except Friendship.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Friend request not found or already processed.'}, status=status.HTTP_404_NOT_FOUND)
    
    # Decline the friend request by deleting it
    friendship_request.delete()

    return JsonResponse({
        'status': 'success',
        'message': 'Friend request declined.'
    }, status=status.HTTP_200_OK)



@api_view(['POST'])
@kinde_auth_required
def ignore_friendship_request(request, kinde_user_id=None):
    """Ignore a friend request"""


    friendship_request_id = request.data.get('friend_request_id')
    
    
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    
    
    try:
        student = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
    
    try:
        friendship_request = Friendship.objects.get(id=friendship_request_id, receiver=student, status='pending')
    except Friendship.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Friend request not found or already processed.'}, status=status.HTTP_404_NOT_FOUND)
    
    # Ignore the friend request by setting its status to 'ignored'
    friendship_request.status = 'ignored'
    friendship_request.save()
    
    serializer_context = {'kinde_user_id': kinde_user_id, 'request': request}
    serializer = FriendshipSerializer(friendship_request, context=serializer_context)
    
    return JsonResponse({
        'status': 'success',
        'message': 'Friend request ignored.',
        'friendship': serializer.data
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@kinde_auth_required
def toggle_friend_request(request, kinde_user_id=None):
    """Send or cancel a friend request"""
    target_user_id = request.data.get('target_user_id')
    
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        sender = Student.objects.get(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
    
    try:
        receiver = Student.objects.get(pk=target_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Target user not found.'}, status=status.HTTP_404_NOT_FOUND)

    you = Block.objects.filter(blocker=receiver, blocked=sender)
    me = Block.objects.filter(blocker=sender, blocked=receiver)
    if you.exists() or me.exists():
        return JsonResponse({'status': 'error', 'message': 'You cannot send a friend request to this user.'}, status=status.HTTP_403_FORBIDDEN)

    
    if sender == receiver:
        return JsonResponse({'status': 'error', 'message': 'You cannot send a friend request to yourself.'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Check for existing friendship or pending request
    existing_friendship = Friendship.objects.filter(
        (Q(sender=sender) & Q(receiver=receiver)) | (Q(sender=receiver) & Q(receiver=sender))
    ).first()
    
    if existing_friendship:
        # Determine if user is the sender or receiver
        is_sender = existing_friendship.sender == sender
        is_receiver = existing_friendship.receiver == sender
        
        # Allow deletion regardless of who created the friendship
        # User can remove the friendship if they are either the sender or receiver
        if is_sender or is_receiver:
            existing_friendship.delete()
            return JsonResponse({'status': 'success', 'message': 'Friendship removed.'}, status=status.HTTP_200_OK)
        
        # This should not happen due to the filter, but keeping as safety
        return JsonResponse({'status': 'info', 'message': 'Friend request already exists or cannot be modified.'}, status=status.HTTP_200_OK)
    else:
        # Create a new friend request
        new_request = Friendship.objects.create(sender=sender, receiver=receiver, status='pending')
        
        serializer_context = {'kinde_user_id': kinde_user_id, 'request': request}
        serializer = FriendshipSerializer(new_request, context=serializer_context)
        
        return JsonResponse({
            'status': 'success',
            'message': 'Friend request sent.',
            'friendship': serializer.data
        }, status=status.HTTP_201_CREATED)


#block/mute
@csrf_exempt
@kinde_auth_required
async def radar(request, kinde_user_id=None):
    """
    Retrieves a paginated list of random items (user, community, post, or event)
    that matches the given region and interests.
    GET: use query params region_id, interests_ids (comma-separated, e.g. interests_ids=1,2,3), limit, offset.
    POST: use JSON body { "region_id": ..., "interests_ids": [1,2,3] }.
    """
    if request.method == 'GET':
        region_id = request.GET.get('region_id')
        interests_ids_str = request.GET.get('interests_ids', '')
        interests_ids_str = [x.strip() for x in interests_ids_str.split(',') if x.strip()] if interests_ids_str else []
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        region_id = data.get('region_id')
        interests_ids_str = data.get('interests_ids', [])
    limit, offset = _parse_pagination_params(request)

    if not all([region_id, interests_ids_str]):
        return JsonResponse({'status': 'error', 'message': 'Region ID and Interests IDs are required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
        region = await Region.objects.aget(pk=region_id)
        interests_ids = [int(i) for i in interests_ids_str]
        
        if not await sync_to_async(Interests.objects.filter(pk__in=interests_ids).count)() == len(interests_ids):
             return JsonResponse({'status': 'error', 'message': 'One or more interests not found.'}, status=status.HTTP_404_NOT_FOUND)

    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=status.HTTP_404_NOT_FOUND)
    except (Region.DoesNotExist, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid region or interest ID format.'}, status=status.HTTP_400_BAD_REQUEST)
    
    # get cached relationship data
    relationship_snapshot = await sync_to_async(get_relationship_snapshot)(student.id)
    blocked_student_ids = list(
        set(relationship_snapshot.get('blocking', [])) | set(relationship_snapshot.get('blocked_by', []))
    )
    community_that_blocked_me_ids = list(relationship_snapshot.get('blocked_by_communities', []))

    # --- 1. Build a QuerySet for each model matching the criteria ---
    matching_querysets = {
        'user': Student.objects.filter(
            student_location__region=region,
            student_interest__id__in=interests_ids,
            is_verified=True
        ).select_related(
            'university', 'student_location', 'student_location__region'
        ).exclude(
            id__in=blocked_student_ids
        ).prefetch_related(
            'student_interest'
        ).distinct(),
        
        'community': Communities.objects.filter(
            location__region=region,
            community_interest__id__in=interests_ids
        ).select_related(
            'location', 'location__region'
        ).exclude(
            id__in=community_that_blocked_me_ids
        ).prefetch_related(
            'community_interest'
        ).distinct(),
        
        'post': Posts.objects.filter(
            Q(student__student_location__region=region) & Q(student__student_interest__id__in=interests_ids)
        ).select_related(
            'student', 'student__student_location', 'student__student_location__region'
        ).exclude(
            student__id__in=blocked_student_ids
        ).prefetch_related(
            'student__student_interest',
            'images', # Prefetch images for the post
            Prefetch('likes') # Prefetch likes for the post
        ).distinct(),
        
        'student_event': Student_Events.objects.filter(
            Q(student__student_location__region=region) & Q(student__student_interest__id__in=interests_ids)
        ).select_related(
            'student', 'student__student_location', 'student__student_location__region'
        ).exclude(
            student__id__in=blocked_student_ids
        ).prefetch_related(
            'student__student_interest',
            'images', # Assuming this is the related_name for event images
            'eventrsvp' # Prefetch RSVPs for the event
        ).distinct(),
        
        'community_event': Community_Events.objects.filter(
            Q(community__location__region=region) & Q(community__community_interest__id__in=interests_ids)
        ).select_related(
            'community', 'community__location', 'community__location__region', 'poster'
        ).exclude(
            community__id__in=community_that_blocked_me_ids
        ).prefetch_related(
            'community__community_interest',
            'images', # Assuming this is the related_name for community event images
            'communityeventrsvp' # Prefetch community event RSVPs
        ).distinct(),

        'community_post': Community_Posts.objects.filter(
            Q(community__location__region=region) & Q(community__community_interest__id__in=interests_ids)
        ).select_related(
            'community', 'community__location', 'community__location__region', 'poster'
        ).exclude(
            community__id__in=community_that_blocked_me_ids
        ).prefetch_related(
            'community__community_interest',
            'images', # Prefetch images for the community post
            Prefetch('likecommunitypost_set') # Prefetch likes for the community post
        ).distinct(),
    }

    # --- 2. Fetch all matching items from all querysets and merge them ---
    all_items = []
    
    for item_type, queryset in matching_querysets.items():
        items_list = await sync_to_async(list)(queryset)
        for item in items_list:
            item.item_type = item_type
        all_items.extend(items_list)
        
    if not all_items:
        return JsonResponse({'status': 'no_content', 'message': 'No items found matching the criteria.'}, status=status.HTTP_200_OK)

    # --- 3. Shuffle the combined list to ensure randomness ---
    await sync_to_async(random.shuffle)(all_items)

    # --- 4. Apply pagination to the shuffled list ---
    paginated_items = all_items[offset:offset + limit]

    # --- 5. Serialize the paginated list ---
    # Get user's memberships for CommunitySerializer
    user_memberships = await sync_to_async(set)(
        Membership.objects.filter(user=student).values_list('community_id', flat=True)
    )
    
    # Get user's roles in communities
    user_community_roles = await sync_to_async(dict)(
        Membership.objects.filter(user=student).values_list('community_id', 'role')
    )
    
    results = []
    serializer_context = {
        'request': request, 
        'kinde_user_id': kinde_user_id,
        'user_memberships': user_memberships,
        'user_community_roles': user_community_roles
    }

    async def serialize_item(item, item_type, serializer_class, context):
        """Helper function to serialize an item asynchronously"""
        def _serialize():
            serializer = serializer_class(item, context=context)
            return serializer.data
        
        serialized_data = await sync_to_async(_serialize)()
        return {'item_type': item_type, 'item_data': serialized_data}

    for item in paginated_items:
        item_type = item.item_type
        if item_type == 'user':
            serializer_class = StudentNameSerializer
        elif item_type == 'community':
            serializer_class = CommunitySerializer
        elif item_type == 'post':
            serializer_class = PostNameSerializer
        elif item_type == 'student_event':
            serializer_class = StudentEventNameSerializer
        elif item_type == 'community_event':
            serializer_class = CommunityEventsNameSerializer
        elif item_type == 'community_post':
            serializer_class = CommunityPostNameSerializer
        else:
            continue
        
        # Use the helper function to serialize asynchronously
        serialized_item = await serialize_item(item, item_type, serializer_class, serializer_context)
        results.append(serialized_item)

    return JsonResponse({
        'status': 'success',
        'results': results,
        'next_offset': offset + limit if len(paginated_items) == limit else None,
        'count': len(results)
    }, status=status.HTTP_200_OK)



@csrf_exempt
@kinde_auth_required
async def get_all_countries(request, kinde_user_id=None):
    """
    Returns a list of all supported countries with their allowed email domains.
    """
    try:
        countries = await sync_to_async(list)(Country.objects.order_by('name').all())
        serializer = await sync_to_async(CountrySerializer)(countries, many=True)
        serializer_data = await sync_to_async(lambda: serializer.data)()

        return JsonResponse({'status': 'success', 'countries': serializer_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Failed to retrieve countries: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@kinde_auth_required
async def get_all_regions(request, kinde_user_id=None):
    """
    Returns a list of all available regions.
    Supports optional ?country=GB filter.
    """
    try:
        qs = Region.objects.order_by('region')
        # TODO: remove GB hardcode once Canada/US expansion launches
        country_code = request.GET.get('country', 'GB')
        qs = qs.filter(country__code=country_code)
        regions = await sync_to_async(list)(qs.all())
        serializer = await sync_to_async(RegionSerializer)(regions, many=True)
        serializer_data = await sync_to_async(lambda: serializer.data)()

        return JsonResponse({'status': 'success', 'regions': serializer_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Failed to retrieve regions: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@kinde_auth_required
async def get_all_interests(request, kinde_user_id=None):
    """
    Returns a list of all available interests.
    """
    try:
        interests = await sync_to_async(list)(Interests.objects.all())
        serializer = await sync_to_async(InterestsSerializer)(interests, many=True)
        serializer_data = await sync_to_async(lambda: serializer.data)()
        
        return JsonResponse({'status': 'success', 'interests': serializer_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Failed to retrieve interests: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@csrf_exempt
@kinde_auth_required
async def get_all_universities(request, kinde_user_id=None):
    """
    Returns a list of all available universities.
    Supports optional ?country=US filter.
    """
    try:
        qs = University.objects.all()
        # TODO: remove GB hardcode once Canada/US expansion launches
        country_code = request.GET.get('country', 'GB')
        qs = qs.filter(country__code=country_code)
        universities = await sync_to_async(list)(qs)
        serializer = await sync_to_async(UniversitySerializer)(universities, many=True)
        serializer_data = await sync_to_async(lambda: serializer.data)()

        return JsonResponse({'status': 'success', 'universities': serializer_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Failed to retrieve universities: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@csrf_exempt
@kinde_auth_required
async def get_all_courses(request, kinde_user_id=None):
    """
    Returns a list of all available courses.
    """
    try:
        courses = await sync_to_async(list)(Courses.objects.all())
        serializer = await sync_to_async(CourseSerializer)(courses, many=True)
        serializer_data = await sync_to_async(lambda: serializer.data)()

        return JsonResponse({'status': 'success', 'courses': serializer_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Failed to retrieve courses: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@csrf_exempt
@kinde_auth_required
async def get_all_locations(request, kinde_user_id=None):
    """
    Returns a list of all available locations.
    Supports optional ?country=CA filter.
    """
    try:
        qs = Location.objects.select_related('region').order_by('location')
        # TODO: remove GB hardcode once Canada/US expansion launches
        country_code = request.GET.get('country', 'GB')
        qs = qs.filter(region__country__code=country_code)
        locations = await sync_to_async(list)(qs.all())
        serializer = await sync_to_async(LocationSerializer)(locations, many=True)
        serializer_data = await sync_to_async(lambda: serializer.data)()

        return JsonResponse({'status': 'success', 'locations': serializer_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Failed to retrieve locations: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

# File: your_app_name/views.py

#works
@csrf_exempt
@kinde_auth_required
async def get_community_info(request, kinde_user_id=None):
    if request.method == 'GET':
        community_id = request.GET.get('community_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_id = data.get('community_id')

    if not community_id:
        return JsonResponse({'status': 'error', 'message': 'community_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community = await Communities.objects.aget(pk=community_id)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'User or community not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community.id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'You are blocked from viewing this community.'}, status=403)
    
    # Get user's memberships (set of community IDs)
    user_memberships = await sync_to_async(set)(
        Membership.objects.filter(user=requester).values_list('community_id', flat=True)
    )
    
    # Get user's roles in communities
    user_community_roles = await sync_to_async(dict)(
        Membership.objects.filter(user=requester).values_list('community_id', 'role')
    )

    # Get friends in community for the serializer
    friends = await sync_to_async(list)(
        Student.objects.filter(
            Q(sent_requests__receiver=requester, sent_requests__status='accepted') |
            Q(received_requests__sender=requester, received_requests__status='accepted')
        ).distinct()
    )
    
    # Get community members
    community_members = await sync_to_async(list)(
        Student.objects.filter(
            membership__community=community
        )
    )
    
    # Find friends who are also community members (optimized - only first friend details + count)
    friends_in_community = []
    first_friend_added = False
    for friend in friends:
        if friend in community_members:
            # Only get full details for the first friend
            if not first_friend_added:
                membership = await sync_to_async(lambda: Membership.objects.filter(
                    user=friend, 
                    community=community
                ).first())()
                
                friend_data = {
                    'id': friend.id,
                    'kinde_user_id': friend.kinde_user_id,
                    'name': friend.name,
                    'username': getattr(friend, 'username', ''),
                    'bio': getattr(friend, 'bio', ''),
                    'profile_image': friend.profile_image.url if friend.profile_image else None,
                    'is_online': getattr(friend, 'is_online', False),
                    'role': membership.role if membership else 'member',
                    'joined_at': membership.created_at.isoformat() if membership and hasattr(membership, 'created_at') else None
                }
                friends_in_community.append(friend_data)
                first_friend_added = True
            else:
                # For remaining friends, just append a placeholder to keep count accurate
                friends_in_community.append({'id': friend.id})

    community_info_data = await sync_to_async(lambda: CommunitySerializer(
        community, 
        context={
            'request': request, 
            'kinde_user_id': kinde_user_id,
            'user_memberships': user_memberships,
            'user_community_roles': user_community_roles,
            'friends_in_community': {community.id: friends_in_community}
        }
    ).data)()
    return JsonResponse(community_info_data, status=200)

@csrf_exempt
@kinde_auth_required
async def get_mutual_friends(request, kinde_user_id=None):
    """
    Get mutual friends between the authenticated user and another student.
    GET: use query param student_id. POST: use JSON body { "student_id": ... }.
    """
    if request.method == 'GET':
        student_id = request.GET.get('student_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        student_id = data.get('student_id')

    if not student_id:
        return JsonResponse({'status': 'error', 'message': 'student_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        target_student = await Student.objects.aget(pk=student_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    # Don't show mutual friends if they're the same person
    if requester == target_student:
        return JsonResponse({
            'status': 'success',
            'mutual_friends': [],
            'total_mutual_friends': 0
        }, status=200)

    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()

    # Get friends of the requester
    requester_friends = await sync_to_async(set)(
        Student.objects.filter(
            Q(sent_requests__receiver=requester, sent_requests__status='accepted') |
            Q(received_requests__sender=requester, received_requests__status='accepted')
        ).exclude(id__in=pending_deletion_ids).distinct().values_list('id', flat=True)
    )

    # Get friends of the target student
    target_friends = await sync_to_async(set)(
        Student.objects.filter(
            Q(sent_requests__receiver=target_student, sent_requests__status='accepted') |
            Q(received_requests__sender=target_student, received_requests__status='accepted')
        ).exclude(id__in=pending_deletion_ids).distinct().values_list('id', flat=True)
    )

    # Find mutual friends (intersection)
    mutual_friend_ids = requester_friends & target_friends

    if not mutual_friend_ids:
        return JsonResponse({
            'status': 'success',
            'mutual_friends': [],
            'total_mutual_friends': 0
        }, status=200)

    # Get mutual friends details
    mutual_friends = await sync_to_async(list)(
        Student.objects.filter(id__in=mutual_friend_ids).exclude(id__in=pending_deletion_ids)
    )

    mutual_friends_data = []
    for friend in mutual_friends:
        friend_data = {
            'id': friend.id,
            'kinde_user_id': friend.kinde_user_id,
            'name': friend.name,
            'username': getattr(friend, 'username', ''),
            'bio': getattr(friend, 'bio', ''),
            'profile_image': friend.profile_image.url if friend.profile_image else None,
            'is_online': getattr(friend, 'is_online', False)
        }
        mutual_friends_data.append(friend_data)

    return JsonResponse({
        'status': 'success',
        'mutual_friends': mutual_friends_data,
        'total_mutual_friends': len(mutual_friends_data)
    }, status=200)

@csrf_exempt
@kinde_auth_required
async def get_friends_in_community(request, kinde_user_id=None):
    """
    Get list of friends who are members of a specific community.
    GET: use query param community_id. POST: use JSON body { "community_id": ... }.
    """
    if request.method == 'GET':
        community_id = request.GET.get('community_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_id = data.get('community_id')

    if not community_id:
        return JsonResponse({'status': 'error', 'message': 'community_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community = await Communities.objects.aget(pk=community_id)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'User or community not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community.id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'You are blocked from viewing this community.'}, status=403)

    # Get all friends of the requester (accepted friendships)
    friends = await sync_to_async(list)(
        Student.objects.filter(
            Q(sent_requests__receiver=requester, sent_requests__status='accepted') |
            Q(received_requests__sender=requester, received_requests__status='accepted'),
            is_verified=True
        ).distinct()
    )

    # Get community members
    community_members = await sync_to_async(list)(
        Student.objects.filter(
            membership__community=community,
            is_verified=True
        )
    )

    # Find friends who are also community members
    friends_in_community = []
    for friend in friends:
        if friend in community_members:
            # Get the friend's membership details
            membership = await sync_to_async(lambda: Membership.objects.filter(
                user=friend, 
                community=community
            ).first())()
            
            friend_data = {
                'id': friend.id,
                'kinde_user_id': friend.kinde_user_id,
                'name': friend.name,
                'username': getattr(friend, 'username', ''),
                'bio': getattr(friend, 'bio', ''),
                'profile_image': friend.profile_image.url if friend.profile_image else None,
                'is_online': getattr(friend, 'is_online', False),
                'role': membership.role if membership else 'member',
                'joined_at': membership.created_at.isoformat() if membership and hasattr(membership, 'created_at') else None
            }
            friends_in_community.append(friend_data)

    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)
    total_count = len(friends_in_community)
    paginated_friends = friends_in_community[offset:offset + limit]
    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': paginated_friends,
        'count': len(paginated_friends),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)
@csrf_exempt
@kinde_auth_required
async def get_community_members(request, kinde_user_id=None):
    """
    Get all members of a community with friends highlighted and admin roles shown.
    GET: use query param community_id. POST: use JSON body { "community_id": ... }.
    """
    if request.method == 'GET':
        community_id = request.GET.get('community_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_id = data.get('community_id')

    if not community_id:
        return JsonResponse({'status': 'error', 'message': 'community_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community = await Communities.objects.aget(pk=community_id)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'User or community not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community.id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'You are blocked from viewing this community.'}, status=403)

    # Get all friends of the requester (accepted friendships)
    friends = await sync_to_async(list)(
        Student.objects.filter(
            Q(sent_requests__receiver=requester, sent_requests__status='accepted') |
            Q(received_requests__sender=requester, received_requests__status='accepted')
        ).distinct()
    )

    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    # Get all community members with their membership details
    memberships_qs = Membership.objects.filter(community=community, user__is_verified=True).select_related('user').order_by('-role', 'user__name')
    total_count = await sync_to_async(memberships_qs.count)()
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    memberships = await sync_to_async(list)(memberships_qs[offset:offset + limit])

    # Create friends set for quick lookup
    friends_set = {friend.id for friend in friends}

    # Process members
    members_data = []
    friends_in_community = []
    admins = []
    secondary_admins = []
    regular_members = []

    for membership in memberships:
        member = membership.user
        is_friend = member.id in friends_set
        
        member_data = {
            'id': member.id,
            'kinde_user_id': member.kinde_user_id,
            'name': member.name,
            'username': getattr(member, 'username', ''),
            'bio': getattr(member, 'bio', ''),
            'profile_image': member.profile_image.url if member.profile_image else None,
            'is_online': getattr(member, 'is_online', False),
            'role': membership.role,
            'joined_at': membership.created_at.isoformat() if hasattr(membership, 'created_at') else None,
            'is_friend': is_friend,
            'is_me': member.id == requester.id
        }
        
        members_data.append(member_data)
        
        # Categorize members
        if is_friend:
            friends_in_community.append(member_data)
        
        if membership.role == 'admin':
            admins.append(member_data)
        elif membership.role == 'secondary_admin':
            secondary_admins.append(member_data)
        else:  # member
            regular_members.append(member_data)

    next_offset = offset + limit if (offset + limit) < total_count else None

    return JsonResponse({
        'status': 'success',
        'results': members_data,
        'count': len(members_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None,
        'summary': {
            'total_members': total_count,
            'friends_in_page': len(friends_in_community),
            'admins_in_page': len(admins),
            'total_secondary_admins': len(secondary_admins),
            'total_regular_members': len(regular_members)
        }
    }, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_student_info(request, kinde_user_id=None):
    if request.method == 'GET':
        student_id = request.GET.get('student_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        student_id = data.get('student_id')

    if not student_id:
        return JsonResponse({'status': 'error', 'message': 'student_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        target_student = await Student.objects.aget(pk=student_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    # Check if target student has a pending deletion request
    from .models import DataDeletionRequest
    has_pending_deletion = await DataDeletionRequest.objects.filter(
        student=target_student,
        is_cancelled=False,
        deleted_at__isnull=True
    ).aexists()
    
    if has_pending_deletion:
        return JsonResponse({'status': 'error', 'message': 'This user is unavailable.'}, status=403)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_ids = set(requester_snapshot.get('blocked_by', []))
    
    # Check if target blocked requester - just an in-memory lookup, no DB query
    if target_student.id in blocked_by_ids:
        return JsonResponse({'status': 'error', 'message': 'This user is unavailable.'}, status=403)
    
    # Get mutual friends data (only first one for efficiency)
    # Get friends of the requester
    requester_friends = await sync_to_async(set)(
        Student.objects.filter(
            Q(sent_requests__receiver=requester, sent_requests__status='accepted') |
            Q(received_requests__sender=requester, received_requests__status='accepted')
        ).distinct().values_list('id', flat=True)
    )

    # Get friends of the target student
    target_friends = await sync_to_async(set)(
        Student.objects.filter(
            Q(sent_requests__receiver=target_student, sent_requests__status='accepted') |
            Q(received_requests__sender=target_student, received_requests__status='accepted')
        ).distinct().values_list('id', flat=True)
    )

    # Find mutual friends (intersection)
    mutual_friend_ids = requester_friends & target_friends
    
    # Get only the first mutual friend for efficiency
    mutual_friends_data = []
    if mutual_friend_ids:
        first_mutual_friend = await Student.objects.filter(id__in=mutual_friend_ids).afirst()
        if first_mutual_friend:
            mutual_friends_data = [{
                'id': first_mutual_friend.id,
                'kinde_user_id': first_mutual_friend.kinde_user_id,
                'name': first_mutual_friend.name,
                'username': getattr(first_mutual_friend, 'username', ''),
                'bio': getattr(first_mutual_friend, 'bio', ''),
                'profile_image': first_mutual_friend.profile_image.url if first_mutual_friend.profile_image else None,
                'is_online': getattr(first_mutual_friend, 'is_online', False)
            }]
        
    student_data = await sync_to_async(lambda: StudentSerializer(
        target_student, 
        context={
            'request': request, 
            'kinde_user_id': kinde_user_id,
            'mutual_friends': {target_student.id: mutual_friends_data}
        }
    ).data)()
    if requester.id == target_student.id:
        # Total raffle entries for this user (as referrer + as referred)
        entries_count = await sync_to_async(
            lambda: VerifiedReferral.objects.filter(
                Q(referrer=target_student) | Q(referred=target_student)
            ).count()
        )()
        student_data['verified_referrals_count'] = entries_count
        student_data['referral_code_used'] = target_student.referral_code_used
    return JsonResponse(student_data, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_student_posts(request, kinde_user_id=None):
    if request.method == 'GET':
        student_id = request.GET.get('student_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        student_id = data.get('student_id')

    if not student_id:
        return JsonResponse({'status': 'error', 'message': 'student_id is required.'}, status=400)
    
    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        target_student = await Student.objects.aget(pk=student_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)

    # Check if target student has a pending deletion request
    from .models import DataDeletionRequest
    has_pending_deletion = await DataDeletionRequest.objects.filter(
        student=target_student,
        is_cancelled=False,
        deleted_at__isnull=True
    ).aexists()
    
    if has_pending_deletion:
        return JsonResponse({'status': 'error', 'message': 'Content unavailable.'}, status=403)
    
    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_ids = set(requester_snapshot.get('blocked_by', []))
    
    # Check if target blocked requester - just an in-memory lookup, no DB query
    if target_student.id in blocked_by_ids:
        return JsonResponse({'status': 'error', 'message': 'Content unavailable.'}, status=403)

    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    # Get student's personal posts (prefetch for comment_count, like_count, isBookmarked in serializer)
    student_posts_qs = Posts.objects.filter(student=target_student).select_related('student').prefetch_related(
        'images', 'videos', 'likes', 'comments',
        Prefetch('bookmarkedposts_set', queryset=BookmarkedPosts.objects.select_related('student')),
        'student__student_interest',
        'student_mentions', 'community_mentions',
    ).order_by('-post_date')

    # Get community posts made by the student (prefetch for comment_count, like_count, isBookmarked)
    community_posts_qs = Community_Posts.objects.filter(poster=target_student).select_related('community', 'poster').prefetch_related(
        'images', 'videos', 'likecommunitypost_set', 'community_posts_comment_set',
        Prefetch('bookmarkedcommunityposts_set', queryset=BookmarkedCommunityPosts.objects.select_related('student')),
        'community__community_interest',
        'student_mentions', 'community_mentions',
    ).order_by('-post_date', '-post_time')
    
    # Get counts
    student_posts_count = await sync_to_async(student_posts_qs.count)()
    community_posts_count = await sync_to_async(community_posts_qs.count)()
    total_count = student_posts_count + community_posts_count
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    # Fetch all posts (we'll paginate after combining)
    student_posts = await sync_to_async(list)(student_posts_qs)
    community_posts = await sync_to_async(list)(community_posts_qs)
    
    # Serialize both types of posts
    student_posts_data = await sync_to_async(lambda: PostSerializer(
        student_posts, 
        many=True, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    community_posts_data = await sync_to_async(lambda: CommunityPostSerializer(
        community_posts, 
        many=True, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Combine both types of posts with type indicators
    all_posts = []
    
    # Add student posts with type indicator
    for post in student_posts_data:
        post['kind'] = 'post'
        all_posts.append(post)
    
    # Add community posts with type indicator
    for post in community_posts_data:
        post['kind'] = 'community_post'
        all_posts.append(post)
    
    # Sort by creation date (most recent first)
    all_posts.sort(key=lambda x: x.get('post_date', ''), reverse=True)
    
    # Apply pagination
    paginated_posts = all_posts[offset:offset + limit]
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': paginated_posts,
        'count': len(paginated_posts),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_student_events(request, kinde_user_id=None):
    if request.method == 'GET':
        student_id = request.GET.get('student_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        student_id = data.get('student_id')

    if not student_id:
        return JsonResponse({'status': 'error', 'message': 'student_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        target_student = await Student.objects.aget(pk=student_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)
    
    # Check if target student has a pending deletion request
    from .models import DataDeletionRequest
    has_pending_deletion = await DataDeletionRequest.objects.filter(
        student=target_student,
        is_cancelled=False,
        deleted_at__isnull=True
    ).aexists()
    
    if has_pending_deletion:
        return JsonResponse({'status': 'error', 'message': 'Content unavailable.'}, status=403)
    
    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_ids = set(requester_snapshot.get('blocked_by', []))
    
    # Check if target blocked requester - just an in-memory lookup, no DB query
    if target_student.id in blocked_by_ids:
        return JsonResponse({'status': 'error', 'message': 'Content unavailable.'}, status=403)

    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)

    # Get student's personal events (prefetch for rsvp_count, comment_count, isBookmarked in serializer)
    student_events_qs = Student_Events.objects.filter(student=target_student).select_related('student').prefetch_related(
        'images', 'videos',
        'eventrsvp', 'eventrsvp__student',
        'student_events_discussion_set',
        Prefetch('bookmarkedstudentevents_set', queryset=BookmarkedStudentEvents.objects.select_related('student')),
        'student__student_interest',
        'student_mentions', 'community_mentions',
    ).order_by('-dateposted')

    # Get community events created by the student (prefetch for rsvp_count, comment_count in serializer)
    community_events_qs = Community_Events.objects.filter(poster=target_student).select_related('community', 'poster').prefetch_related(
        'images', 'videos',
        'communityeventrsvp', 'communityeventrsvp__student',
        'community_events_discussion_set',
        Prefetch('bookmarkedcommunityevents_set', queryset=BookmarkedCommunityEvents.objects.select_related('student')),
        'community__community_interest',
        'student_mentions', 'community_mentions',
    ).order_by('-dateposted')
    
    # Get counts
    student_events_count = await sync_to_async(student_events_qs.count)()
    community_events_count = await sync_to_async(community_events_qs.count)()
    total_count = student_events_count + community_events_count
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    # Fetch all events (we'll paginate after combining)
    student_events = await sync_to_async(list)(student_events_qs)
    community_events = await sync_to_async(list)(community_events_qs)
    
    # Serialize both types of events
    student_events_data = await sync_to_async(lambda: StudentEventSerializer(
        student_events, 
        many=True, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    community_events_data = await sync_to_async(lambda: CommunityEventsSerializer(
        community_events, 
        many=True, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Combine both types of events with type indicators
    all_events = []
    
    # Add student events with type indicator
    for event in student_events_data:
        event['kind'] = 'student_event'
        all_events.append(event)
    
    # Add community events with type indicator
    for event in community_events_data:
        event['kind'] = 'community_event'
        all_events.append(event)
    
    # Sort by creation date (most recent first)
    all_events.sort(key=lambda x: x.get('dateposted', ''), reverse=True)
    
    # Apply pagination
    paginated_events = all_events[offset:offset + limit]
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': paginated_events,
        'count': len(paginated_events),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

@csrf_exempt
@kinde_auth_required
async def get_community_posts(request, kinde_user_id=None):
    if request.method == 'GET':
        community_id = request.GET.get('community_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_id = data.get('community_id')

    if not community_id:
        return JsonResponse({'status': 'error', 'message': 'community_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community = await Communities.objects.aget(pk=community_id)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Community or user not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community.id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'You are blocked from viewing this community.'}, status=403)

    
    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)
    
    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    posts_qs = Community_Posts.objects.filter(community=community).exclude(poster__id__in=pending_deletion_ids).select_related('community', 'poster').prefetch_related('images', 'likecommunitypost_set', 'community__community_interest').order_by('-post_date', '-post_time')
    
    total_count = await sync_to_async(posts_qs.count)()
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    paginated_posts = await sync_to_async(list)(posts_qs[offset:offset + limit])
    
    posts_data = await sync_to_async(lambda: CommunityPostSerializer(
        paginated_posts, 
        many=True, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Add kind field to each post
    for post in posts_data:
        post['kind'] = 'community_post'
    
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': posts_data,
        'count': len(posts_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_community_events(request, kinde_user_id=None):
    if request.method == 'GET':
        community_id = request.GET.get('community_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_id = data.get('community_id')

    if not community_id:
        return JsonResponse({'status': 'error', 'message': 'community_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community = await Communities.objects.aget(pk=community_id)
    except (Student.DoesNotExist, Communities.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Community or user not found.'}, status=404)
    
    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community.id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'You are blocked from viewing this community.'}, status=403)

    
    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)
    
    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    events_qs = Community_Events.objects.filter(community=community).exclude(poster__id__in=pending_deletion_ids).select_related('community', 'poster').prefetch_related('images', 'communityeventrsvp', 'community__community_interest').order_by('-dateposted')
    
    total_count = await sync_to_async(events_qs.count)()
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    paginated_events = await sync_to_async(list)(events_qs[offset:offset + limit])
    
    community_data = await sync_to_async(lambda: CommunityEventsSerializer(
        paginated_events, 
        many=True, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Add kind field to each event
    for event in community_data:
        event['kind'] = 'community_event'
    
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': community_data,
        'count': len(community_data),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_community_event(request, kinde_user_id=None):
    if request.method == 'GET':
        community_event_id = request.GET.get('community_event_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_event_id = data.get('community_event_id')

    if not community_event_id:
        return JsonResponse({'status': 'error', 'message': 'community_event_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community_event = await Community_Events.objects.select_related('community', 'poster').prefetch_related('images', 'communityeventrsvp').aget(pk=community_event_id)
    except (Student.DoesNotExist, Community_Events.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Community Event or user not found.'}, status=404)
    
    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community_event.community_id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'This event is unavailable.'}, status=403)
        
    community_event_data = await sync_to_async(lambda: CommunityEventsSerializer(
        community_event, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Add kind field
    community_event_data['kind'] = 'community_event'
    
    return JsonResponse(community_event_data, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_student_event(request, kinde_user_id=None):
    if request.method == 'GET':
        student_event_id = request.GET.get('student_event_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        student_event_id = data.get('student_event_id')

    if not student_event_id:
        return JsonResponse({'status': 'error', 'message': 'student_event_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        student_event = await Student_Events.objects.select_related('student').prefetch_related('images', 'eventrsvp').aget(pk=student_event_id)
    except (Student.DoesNotExist, Student_Events.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Student Event or user not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_ids = set(requester_snapshot.get('blocked_by', []))
    
    # Check if event creator blocked requester - just an in-memory lookup, no DB query
    if student_event.student_id in blocked_by_ids:
        return JsonResponse({'status': 'error', 'message': 'This event is unavailable.'}, status=403)
    
    student_event_data = await sync_to_async(lambda: StudentEventSerializer(
        student_event, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Add kind field
    student_event_data['kind'] = 'student_event'
    
    return JsonResponse(student_event_data, status=200)

#####################################################

#works
@csrf_exempt
@kinde_auth_required
async def get_post(request, kinde_user_id=None):
    """
    Get a single post by ID.
    GET: use query param post_id. POST: use JSON body { "post_id": ... }.
    """
    if request.method == 'GET':
        post_id = request.GET.get('post_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        post_id = data.get('post_id')

    if not post_id:
        return JsonResponse({'status': 'error', 'message': 'post_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        post = await Posts.objects.select_related('student').aget(pk=post_id)
    except (Student.DoesNotExist, Posts.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Post or user not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_ids = set(requester_snapshot.get('blocked_by', []))
    
    # Check if post author blocked requester - just an in-memory lookup, no DB query
    if post.student_id in blocked_by_ids:
        return JsonResponse({'status': 'error', 'message': 'This post is unavailable.'}, status=403)
    
    post_data = await sync_to_async(lambda: PostSerializer(
        post, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Add kind field
    post_data['kind'] = 'post'
    
    return JsonResponse(post_data, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_community_post(request, kinde_user_id=None):
    """
    Get a single community post by ID.
    GET: use query param community_post_id. POST: use JSON body { "community_post_id": ... }.
    """
    if request.method == 'GET':
        community_post_id = request.GET.get('community_post_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_post_id = data.get('community_post_id')

    if not community_post_id:
        return JsonResponse({'status': 'error', 'message': 'community_post_id is required.'}, status=400)

    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community_post = await Community_Posts.objects.select_related('community', 'poster').aget(pk=community_post_id)
    except (Student.DoesNotExist, Community_Posts.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Community post or user not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(requester.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community_post.community_id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'This post is unavailable.'}, status=403)
    
    community_post_data = await sync_to_async(lambda: CommunityPostSerializer(
        community_post, 
        context={'request': request, 'kinde_user_id': kinde_user_id}
    ).data)()
    
    # Add kind field
    community_post_data['kind'] = 'community_post'
    
    return JsonResponse(community_post_data, status=200)

#works
@csrf_exempt
@kinde_auth_required
async def get_community_post_comments(request, kinde_user_id=None):
    if request.method == 'GET':
        post_id = request.GET.get('community_post_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        post_id = data.get('community_post_id')

    if not post_id:
        return JsonResponse({'status': 'error', 'message': 'community_post_id is required.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community_post = await Community_Posts.objects.select_related('community').aget(pk=post_id)
    except (Student.DoesNotExist, Community_Posts.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Community Post or user not found.'}, status=404)

    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(me.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community_post.community_id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'This post is unavailable.'}, status=403)
    

    

    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)
    
    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    comments_queryset = Community_Posts_Comment.objects.filter(
        community_post=community_post,
        parent__isnull=True
    ).exclude(student__id__in=pending_deletion_ids).select_related('student', 'community_post__community').prefetch_related('replies__student', 'student_mentions', 'community_mentions').order_by('sent_at')

    total_count = await sync_to_async(comments_queryset.count)()
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    paginated_comments = await sync_to_async(list)(comments_queryset[offset:offset + limit])
    
    serializer_context = {'kinde_user_id': kinde_user_id}
    compostcomments = await sync_to_async(lambda: CommunityPostCommentSerializer(paginated_comments, many=True, context=serializer_context).data)()
    
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': compostcomments,
        'count': len(compostcomments),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=status.HTTP_200_OK)

#works
@csrf_exempt
@kinde_auth_required
async def get_student_events_discussion(request, kinde_user_id=None):
    if request.method == 'GET':
        student_event_id = request.GET.get('student_event_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        student_event_id = data.get('student_event_id')

    if not student_event_id:
        return JsonResponse({'status': 'error', 'message': 'student_event_id is required.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
        student_event = await Student_Events.objects.aget(pk=student_event_id)
    except (Student.DoesNotExist, Student_Events.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Student or event not found.'}, status=404)
    
    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(me.id)
    blocked_by_ids = set(requester_snapshot.get('blocked_by', []))
    
    # Check if event creator blocked requester - just an in-memory lookup, no DB query
    if student_event.student_id in blocked_by_ids:
        return JsonResponse({'status': 'error', 'message': 'This event is unavailable.'}, status=403)
    
    # Block Logic - Get IDs of users I blocked + users who blocked me
    
    
    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)
    
    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    discussions_queryset = Student_Events_Discussion.objects.filter(
        student_event=student_event,
        parent__isnull=True
    ).exclude(student__id__in=pending_deletion_ids).select_related('student').prefetch_related('replies__student', 'student_mentions', 'community_mentions').order_by('sent_at')

    total_count = await sync_to_async(discussions_queryset.count)()
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    paginated_discussions = await sync_to_async(list)(discussions_queryset[offset:offset + limit])

    serializer_context = {'kinde_user_id': kinde_user_id}
    sed = await sync_to_async(lambda: StudentEventDiscussionSerializer(paginated_discussions, many=True, context=serializer_context).data)()
    
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': sed,
        'count': len(sed),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=status.HTTP_200_OK)

#works
@csrf_exempt
@kinde_auth_required
async def get_community_events_discussion(request, kinde_user_id=None):
    if request.method == 'GET':
        community_event_id = request.GET.get('community_event_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        community_event_id = data.get('community_event_id')

    if not community_event_id:
        return JsonResponse({'status': 'error', 'message': 'community_event_id is required.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
        community_event = await Community_Events.objects.aget(pk=community_event_id)
    except (Student.DoesNotExist, Community_Events.DoesNotExist):
        return JsonResponse({'status': 'error', 'message': 'Community Event or user not found.'}, status=404)
    
    # Get requester's relationship snapshot once (cached) - then use in-memory lookups
    from .cache_utils import get_relationship_snapshot
    requester_snapshot = await sync_to_async(get_relationship_snapshot)(me.id)
    blocked_by_community_ids = set(requester_snapshot.get('blocked_by_communities', []))
    
    # Check if community blocked requester - just an in-memory lookup, no DB query
    if community_event.community_id in blocked_by_community_ids:
        return JsonResponse({'status': 'error', 'message': 'This event is unavailable.'}, status=403)

    # Pagination
    limit, offset = _parse_pagination_params(request, default_limit=20, max_limit=100)
    
    # Get student IDs with pending deletion requests
    pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
    
    discussions_queryset = Community_Events_Discussion.objects.filter(
        community_event=community_event,
        parent__isnull=True
    ).exclude(student__id__in=pending_deletion_ids).select_related('student', 'community_event__community').prefetch_related('replies__student', 'student_mentions', 'community_mentions').order_by('sent_at')

    total_count = await sync_to_async(discussions_queryset.count)()
    
    if total_count == 0:
        return JsonResponse({
            'status': 'success',
            'results': [],
            'count': 0,
            'total_count': 0,
            'limit': limit,
            'offset': offset,
            'next_offset': None,
            'has_next': False
        }, status=200)
    
    paginated_discussions = await sync_to_async(list)(discussions_queryset[offset:offset + limit])

    serializer_context = {'kinde_user_id': kinde_user_id}
    ced = await sync_to_async(lambda: CommunityEventDiscussionSerializer(paginated_discussions, many=True, context=serializer_context).data)()
    
    next_offset = offset + limit if (offset + limit) < total_count else None
    
    return JsonResponse({
        'status': 'success',
        'results': ced,
        'count': len(ced),
        'total_count': total_count,
        'limit': limit,
        'offset': offset,
        'next_offset': next_offset,
        'has_next': next_offset is not None
    }, status=status.HTTP_200_OK)

@csrf_exempt
@kinde_auth_required
async def toggle_block_student(request, kinde_user_id=None):
    data = json.loads(request.body.decode('utf-8'))
    target_student_id = data.get('target_student_id')

    if not target_student_id:
        return JsonResponse({
            'status': 'error', 
            'message': 'target_student_id is required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # 1. Get the authenticated student (the blocker) - FIXED: use kinde_user_id
    try:
        requester = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error', 
            'message': 'Authenticated student profile not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    # 2. Get the target student
    try:
        target_student = await Student.objects.aget(pk=target_student_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error', 
            'message': 'Target student not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    # 3. Prevent blocking oneself
    if requester.pk == target_student.pk:
        return JsonResponse({
            'status': 'error', 
            'message': 'You cannot block yourself.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # 4. Check for existing block relationship
    existing_block = await Block.objects.filter(
        blocker=requester, 
        blocked=target_student
    ).aexists()

    if existing_block:
        # Block exists, so delete it (unblock)
        await Block.objects.filter(
            blocker=requester, 
            blocked=target_student
        ).adelete()
        message = 'Student unblocked successfully.'
        action = 'unblocked'
        response_status = status.HTTP_200_OK
    else:
        # Block does not exist, so create it (block)
        # First, remove any existing accepted friendship between the two students
        await Friendship.objects.filter(
            (Q(sender=requester, receiver=target_student) | 
             Q(sender=target_student, receiver=requester)),
        ).adelete()

        await Block.objects.acreate(
            blocker=requester,
            blocked=target_student,
        )
        message = 'Student blocked successfully.'
        action = 'blocked'
        response_status = status.HTTP_200_OK  # FIXED: consistent status code

    return JsonResponse({
        'status': 'success', 
        'message': message,
        'action': action
    }, status=response_status)


@csrf_exempt
@kinde_auth_required
async def toggle_community_blocks_user(request, kinde_user_id=None):
    data = json.loads(request.body.decode('utf-8'))
    student_id = data.get('student_id')
    community_id = data.get('community_id')

    if not all([community_id, student_id]):
        return JsonResponse({
            'status': 'error', 
            'message': 'student_id and community_id are required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error', 
            'message': 'User not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    
    try:
        target_student = await Student.objects.aget(pk=student_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error', 
            'message': 'Target student not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    try:
        community = await Communities.objects.aget(pk=community_id)
    except Communities.DoesNotExist:
        return JsonResponse({
            'status': 'error', 
            'message': 'Community not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    
    # Prevent self-blocking
    if student.pk == target_student.pk:
        return JsonResponse({
            'status': 'error', 
            'message': 'Cannot block yourself.'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Check permissions (fixed to use async)
    has_permission = await Membership.objects.filter(
        user=student, 
        community=community, 
        role__in=['admin', 'secondary_admin']
    ).aexists()
    
    if not has_permission:
        return JsonResponse({
            'status': 'error', 
            'message': 'You do not have permission to block users in this community.'
        }, status=status.HTTP_403_FORBIDDEN)

    # Check if block already exists
    existing_block = await BlockedByCommunities.objects.filter(
        blocked_student=target_student, 
        community=community
    ).aexists()

    if existing_block:
        # Block exists, so delete it (unblock)
        await BlockedByCommunities.objects.filter(
            blocked_student=target_student, 
            community=community
        ).adelete()
        return JsonResponse({
            'status': 'success', 
            'message': 'Student unblocked from community successfully.',
            'action': 'unblocked'
        }, status=status.HTTP_200_OK)
    
    else:
        # Block does not exist, so create it (block)
        # Remove membership if it exists
        membership_exists = await Membership.objects.filter(
            user=target_student, 
            community=community
        ).aexists()
        
        if membership_exists:
            await Membership.objects.filter(
                user=target_student, 
                community=community
            ).adelete()

        # Create the block
        await BlockedByCommunities.objects.acreate(
            blocked_student=target_student, 
            community=community
        )
        
        return JsonResponse({
            'status': 'success', 
            'message': 'Student blocked from community successfully.',
            'action': 'blocked'
        }, status=status.HTTP_200_OK)


MODEL_MAP = {
    'student': Student,
    'post': Posts,
    'community_post': Community_Posts,
    'direct_message': DirectMessage,
    'community_event': Community_Events,
    'student_event': Student_Events,
    'post_comment': PostComment,
    'community_post_comment': Community_Posts_Comment,
    'community_event_comments': Community_Events_Discussion,
    'student_event_comments': Student_Events_Discussion,
    # Add other reportable models here
}
@csrf_exempt
@kinde_auth_required
async def create_report(request, kinde_user_id=None):
    """
    Async view for users to report content or other users.
    Automatically copies content to prevent loss if original gets deleted.
    """
    data = json.loads(request.body.decode('utf-8'))

    
    # Required fields
    report_type = data.get('report_type')
    
    # Optional fields
    content_type_str = data.get('content_type')  # e.g., 'post', 'student', 'community_post'
    object_id = data.get('object_id')
    description = data.get('description', '')
    
    # Validation
    if not report_type:
        return JsonResponse({
            'status': 'error',
            'message': 'report_type is required.'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Validate report type
    valid_report_types = [choice[0] for choice in Report.REPORT_TYPE_CHOICES]
    if report_type not in valid_report_types:
        return JsonResponse({
            'status': 'error',
            'message': f'Invalid report_type. Must be one of: {", ".join(valid_report_types)}'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Get the reporter
    try:
        reporter = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Reporter profile not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    
    content_type_obj = None
    content_object = None
    report_copy = None
    
    # If reporting specific content
    if content_type_str and object_id:
        # Validate content type
        if content_type_str not in MODEL_MAP:
            return JsonResponse({
                'status': 'error',
                'message': f'Invalid content_type. Must be one of: {", ".join(MODEL_MAP.keys())}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            object_id = int(object_id)
        except (ValueError, TypeError):
            return JsonResponse({
                'status': 'error',
                'message': 'object_id must be a valid integer.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get the content type and object
        model_class = MODEL_MAP[content_type_str]
        content_type_obj = await sync_to_async(lambda: ContentType.objects.get_for_model(model_class))()
        
        try:
            content_object = await model_class.objects.aget(pk=object_id)
        except model_class.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'message': f'{content_type_str.replace("_", " ").title()} not found.'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Prevent self-reporting (for student reports)
        if content_type_str == 'student' and content_object.pk == reporter.pk:
            return JsonResponse({
                'status': 'error',
                'message': 'You cannot report yourself.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Create a copy of the content to preserve evidence
        report_copy = await _create_content_copy(content_object, content_type_str)
    
    elif content_type_str or object_id:
        # If only one is provided, both are required
        return JsonResponse({
            'status': 'error',
            'message': 'Both content_type and object_id are required when reporting specific content.'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Create the report
    try:
        report = await Report.objects.acreate(
            reporter=reporter,
            content_type=content_type_obj,
            object_id=object_id if content_type_obj else None,
            report_type=report_type,
            description=description,
            report_copy=report_copy,
            status='pending'
        )
        
        return JsonResponse({
            'status': 'success',
            'message': 'Report submitted successfully.',
            'report_id': report.id,
            'data': {
                'report_type': report.report_type,
                'content_type': content_type_str if content_type_str else None,
                'object_id': object_id if object_id else None,
                'description': report.description,
                'created_at': report.created_at.isoformat(),
                'status': report.status
            }
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Failed to create report: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


async def _create_content_copy(content_object, content_type_str):
    """
    Helper function to create a copy of content for the report.
    This preserves evidence in case the original content gets deleted.
    """
    try:
        if content_type_str == 'student':
            return f"User: {getattr(content_object, 'name', 'N/A')} (ID: {content_object.pk})"
        
        elif content_type_str in ['post', 'community_post']:
            title = getattr(content_object, 'title', '')
            body = getattr(content_object, 'body', '')
            author = getattr(content_object, 'author', None)
            author_name = getattr(author, 'name', 'Unknown') if author else 'Unknown'
            
            copy_text = f"Title: {title}\nAuthor: {author_name}\nContent: {body}"
            return copy_text[:2000]  # Truncate to fit field limit
        
        elif content_type_str == 'direct_message':
            sender = getattr(content_object, 'sender', None)
            content = getattr(content_object, 'content', '')
            sender_name = getattr(sender, 'name', 'Unknown') if sender else 'Unknown'
            
            copy_text = f"From: {sender_name}\nMessage: {content}"
            return copy_text[:2000]
        
        elif content_type_str in ['community_event', 'student_event']:
            title = getattr(content_object, 'title', '')
            description = getattr(content_object, 'description', '')
            organizer = getattr(content_object, 'organizer', None)
            organizer_name = getattr(organizer, 'name', 'Unknown') if organizer else 'Unknown'
            
            copy_text = f"Event: {title}\nOrganizer: {organizer_name}\nDescription: {description}"
            return copy_text[:2000]
        
        elif content_type_str in ['post_comment', 'community_post_comment', 
                                  'community_event_comments', 'student_event_comments']:
            content = getattr(content_object, 'content', '') or getattr(content_object, 'body', '')
            author = getattr(content_object, 'author', None) or getattr(content_object, 'user', None)
            author_name = getattr(author, 'name', 'Unknown') if author else 'Unknown'
            
            copy_text = f"Comment by {author_name}: {content}"
            return copy_text[:2000]
        
        else:
            # Generic fallback
            return f"Content of type {content_type_str} (ID: {content_object.pk})"
    
    except Exception as e:
        # If anything goes wrong, return a basic copy
        return f"Content of type {content_type_str} (ID: {content_object.pk}) - Copy failed: {str(e)}"


# Additional helper view to get valid report types and content types

async def get_report_options(request):
    """
    Returns valid report types and content types for the frontend.
    """
    return JsonResponse({
        'status': 'success',
        'data': {
            'report_types': [
                {'value': choice[0], 'label': choice[1]} 
                for choice in Report.REPORT_TYPE_CHOICES
            ],
            'content_types': [
                {'value': key, 'label': key.replace('_', ' ').title()} 
                for key in MODEL_MAP.keys()
            ]
        }
    }, status=status.HTTP_200_OK)


@csrf_exempt
@kinde_auth_required
async def toggle_mute_student(request, kinde_user_id=None):
    """
    Toggles muting/unmuting a student.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request method.'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    try:
        data = json.loads(request.body)
        target_student_id = data.get('target_student_id')
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON data.'}, status=status.HTTP_400_BAD_REQUEST)

    if not target_student_id:
        return JsonResponse({
            'status': 'error',
            'message': 'target_student_id is required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        requester = await sync_to_async(Student.objects.get)(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Authenticated student profile not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    try:
        target_student = await sync_to_async(Student.objects.get)(pk=target_student_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Target student not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    
    if requester.pk == target_student.pk:
        return JsonResponse({
            'status': 'error',
            'message': 'You cannot mute yourself.'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Check for existing mute relationship
    existing_mute = await sync_to_async(MutedStudents.objects.filter(
        student=requester,
        muted_student=target_student
    ).exists)()

    if existing_mute:
        # Mute exists, so delete it (unmute)
        await sync_to_async(MutedStudents.objects.filter(
            student=requester,
            muted_student=target_student
        ).delete)()
        message = 'Student unmuted successfully.'
        action = 'unmuted'
        response_status = status.HTTP_200_OK
    else:
        # Mute does not exist, so create it (mute)
        await sync_to_async(MutedStudents.objects.create)(
            student=requester,
            muted_student=target_student,
        )
        message = 'Student muted successfully.'
        action = 'muted'
        response_status = status.HTTP_200_OK

    return JsonResponse({
        'status': 'success',
        'message': message,
        'action': action
    }, status=response_status)

@csrf_exempt
@kinde_auth_required
async def toggle_mute_community(request, kinde_user_id=None):
    """
    Toggles muting/unmuting a community.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request method.'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    try:
        data = json.loads(request.body)
        target_community_id = data.get('target_community_id')
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON data.'}, status=status.HTTP_400_BAD_REQUEST)
        
    if not target_community_id:
        return JsonResponse({
            'status': 'error',
            'message': 'target_community_id is required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        requester = await sync_to_async(Student.objects.get)(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Authenticated student profile not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    try:
        target_community = await sync_to_async(Communities.objects.get)(pk=target_community_id)
    except Communities.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Target community not found.'
        }, status=status.HTTP_404_NOT_FOUND)

    # Check for existing mute relationship
    existing_mute = await sync_to_async(MutedCommunities.objects.filter(
        student=requester,
        community=target_community
    ).exists)()

    if existing_mute:
        # Mute exists, so delete it (unmute)
        await sync_to_async(MutedCommunities.objects.filter(
            student=requester,
            community=target_community
        ).delete)()
        message = 'Community unmuted successfully.'
        action = 'unmuted'
        response_status = status.HTTP_200_OK
    else:
        # Mute does not exist, so create it (mute)
        await sync_to_async(MutedCommunities.objects.create)(
            student=requester,
            community=target_community,
        )
        message = 'Community muted successfully.'
        action = 'muted'
        response_status = status.HTTP_200_OK

    return JsonResponse({
        'status': 'success',
        'message': message,
        'action': action
    }, status=response_status)


@csrf_exempt
@kinde_auth_required
async def comment_on_post(request, kinde_user_id=None):
    """
    Allows an authenticated user to comment on a post and broadcasts the update.
    """
    data = json.loads(request.body.decode('utf-8'))
    post_id = data.get('post_id')
    comment_text = data.get('comment')
    parent_comment_id = data.get('parent_comment_id', None)

    # 1. Validate incoming data
    if not post_id or not comment_text:
        return JsonResponse({"status": "error", "message": "Post ID and comment text are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    character_count = len(comment_text)
    if character_count > 2000:
        return JsonResponse({"status": "error", "message": "Comment is too long. Keep it under 2000 characters."}, status=status.HTTP_400_BAD_REQUEST)

    # 2. Get Authenticated Student
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found in your database.'}, status=status.HTTP_404_NOT_FOUND)

    # 3. Get the Target Post
    try:
        post = await Posts.objects.aget(pk=post_id)
    except Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Post not found.'}, status=status.HTTP_404_NOT_FOUND)
    
    # 4. Handle Parent Comment
    parent_comment_instance = None 
    if parent_comment_id:
        try:
            parent_comment_instance = await PostComment.objects.select_related('post').aget(id=parent_comment_id)
            if parent_comment_instance.post != post:
                return JsonResponse({"status": "error", "message": "Parent comment does not belong to this post."}, status=status.HTTP_400_BAD_REQUEST)
        except PostComment.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Parent comment not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Find mentions before creating comment
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(comment_text)
        
        # Define the database operations as a synchronous function
        def create_comment_with_mentions():
            with transaction.atomic():
                # 5. Create the Comment
                new_comment = PostComment.objects.create(
                    post=post,
                    student=student,
                    comment=comment_text,
                    parent=parent_comment_instance
                )
                
                # Add mentions to the comment
                if mentioned_students.exists():
                    new_comment.student_mentions.set(mentioned_students)
                if mentioned_communities.exists():
                    new_comment.community_mentions.set(mentioned_communities)
                
                return new_comment
        
        # Execute the database operations synchronously within async context
        new_comment = await sync_to_async(create_comment_with_mentions)()
        
        # 6. Prepare data for WebSocket Broadcast
        updated_post = await Posts.objects.annotate(comment_count=Count('comments')).aget(pk=post_id)
        
        # Fetch all top-level comments for the post to send the updated tree structure
        top_level_comments_queryset = PostComment.objects.filter(post=post, parent__isnull=True).select_related('student').prefetch_related('replies__student', 'student_mentions', 'community_mentions').order_by('commented_at')
        top_level_comments = [comment async for comment in top_level_comments_queryset]
        
        serializer_context = {'kinde_user_id': kinde_user_id} 
        
        # Use PostCommentSerializer for PostComment objects
        comment_tree_data = await sync_to_async(lambda: PostCommentSerializer(top_level_comments, many=True, context=serializer_context).data)()
        
        # Serialize the updated post data
        updated_post_data = await sync_to_async(lambda: PostSerializer(updated_post, context=serializer_context).data)()
        updated_post_data['comments'] = comment_tree_data 

        # 7. WebSocket Broadcast
        channel_layer = get_channel_layer()
        if channel_layer:
            try:
                # Broadcast to the general feed
                await channel_layer.group_send(
                    'global_feed_updates', 
                    {
                        'type': 'feed.update', 
                        'data': {
                            'update_type': 'post_commented',
                            'content_type': 'post',
                            'item_id': post_id,
                            'item_data': updated_post_data
                        }
                    }
                )

                # Broadcast to the specific post's detail page
                await channel_layer.group_send(
                    f'post_updates_{post_id}',
                    {
                        'type': 'post_updated',
                        'post_type': 'posts',
                        'post_data': updated_post_data
                    }
                )
                
                # Broadcast to the student's posts list group
                await channel_layer.group_send(
                    f'student_posts_updates_{post.student.id}',
                    {
                        'type': 'post_updated',
                        'post_data': updated_post_data
                    }
                )
                
                # Broadcast to all users' post feeds
                broadcast_post_update_to_feeds(updated_post_data, 'post')
            except Exception as e:
                print(f"Error broadcasting post comment update via WebSocket: {e}")

        # 8. Return response to client
        new_comment_data = await sync_to_async(lambda: PostCommentSerializer(new_comment, context=serializer_context).data)()
        
        return JsonResponse({
            "status": "success", 
            "message": "Comment added successfully.",
            "comment": new_comment_data
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error",
            "message": f"Failed to create comment: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@kinde_auth_required
async def comment_on_community_post(request, kinde_user_id=None):
    """
    Allows an authenticated user to comment on a community post or reply to an existing comment,
    and broadcasts the update.
    """
    data = json.loads(request.body.decode('utf-8'))
    community_post_id = data.get('community_post_id')
    comment_text = data.get('comment_text')
    parent_comment_id = data.get('parent_comment_id', None) 

    # 1. Validate incoming data
    if not community_post_id or not comment_text:
        return JsonResponse({"status": "error", "message": "Community Post ID and comment text are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    character_count = len(comment_text)
    if character_count > 2000:
        return JsonResponse({"status": "error", "message": "Comment is too long. Keep it under 2000 characters."}, status=status.HTTP_400_BAD_REQUEST)

    # 2. Get Authenticated Student
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found in your database.'}, status=status.HTTP_404_NOT_FOUND)

    # 3. Get the Target Community Post
    try:
        community_post = await Community_Posts.objects.aget(pk=community_post_id)
    except Community_Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community post not found.'}, status=status.HTTP_404_NOT_FOUND)
    
    # 4. Handle Parent Comment
    parent_comment_instance = None 
    if parent_comment_id:
        try:
            parent_comment_instance = await Community_Posts_Comment.objects.select_related('community_post').aget(id=parent_comment_id)
            if parent_comment_instance.community_post != community_post:
                return JsonResponse({"status": "error", "message": "Parent comment does not belong to this community post."}, status=status.HTTP_400_BAD_REQUEST)
        except Community_Posts_Comment.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Parent comment not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Find mentions before creating comment
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(comment_text)
        
        # Define the database operations as a synchronous function
        def create_community_comment_with_mentions():
            with transaction.atomic():
                # 5. Create the Comment
                new_comment = Community_Posts_Comment.objects.create(
                    community_post=community_post,
                    student=student,
                    comment_text=comment_text,
                    parent=parent_comment_instance
                )
                
                # Add mentions to the comment
                if mentioned_students.exists():
                    new_comment.student_mentions.set(mentioned_students)
                if mentioned_communities.exists():
                    new_comment.community_mentions.set(mentioned_communities)
                
                return new_comment
        
        # Execute the database operations synchronously within async context
        new_comment = await sync_to_async(create_community_comment_with_mentions)()
        
        # 6. Prepare data for WebSocket Broadcast
        updated_community_post = await Community_Posts.objects.annotate(comment_count=Count('community_posts_comment')).aget(pk=community_post_id)
        
        top_level_comments_queryset = Community_Posts_Comment.objects.filter(community_post=community_post, parent__isnull=True).select_related('student', 'community_post__community').prefetch_related('replies__student', 'student_mentions', 'community_mentions').order_by('sent_at')
        top_level_comments = [comment async for comment in top_level_comments_queryset]
        
        serializer_context = {'kinde_user_id': kinde_user_id} 
        
        comment_tree_data = await sync_to_async(lambda: CommunityPostCommentSerializer(top_level_comments, many=True, context=serializer_context).data)()
        updated_community_post_data = await sync_to_async(lambda: CommunityPostSerializer(updated_community_post, context=serializer_context).data)()
        updated_community_post_data['community_posts_comment'] = comment_tree_data 

        # 7. WebSocket Broadcast
        channel_layer = get_channel_layer()
        if channel_layer:
            try:
                # Broadcast to the general feed
                await channel_layer.group_send(
                    'global_feed_updates', 
                    {
                        'type': 'feed.update', 
                        'data': {
                            'update_type': 'community_post_commented',
                            'content_type': 'community_post',
                            'item_id': community_post_id,
                            'item_data': updated_community_post_data
                        }
                    }
                )

                # Broadcast to the specific community post's detail page
                await channel_layer.group_send(
                    f'community_post_updates_{community_post_id}',
                    {
                        'type': 'community_post_updated',
                        'post_data': updated_community_post_data
                    }
                )
                
                # Broadcast to the community's posts list group
                await channel_layer.group_send(
                    f'community_posts_list_updates_{community_post.community.id}',
                    {
                        'type': 'community_post_updated',
                        'post_data': updated_community_post_data
                    }
                )
                
                # Broadcast to all users' post feeds
                broadcast_post_update_to_feeds(updated_community_post_data, 'community_post')
            except Exception as e:
                print(f"Error broadcasting community post comment update via WebSocket: {e}")

        # 8. Return response to client
        new_comment_data = await sync_to_async(lambda: CommunityPostCommentSerializer(new_comment, context=serializer_context).data)()
        
        return JsonResponse({
            "status": "success", 
            "message": "Comment added successfully.",
            "comment": new_comment_data
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error",
            "message": f"Failed to create comment: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@csrf_exempt
@kinde_auth_required
async def post_student_events_discussion(request, kinde_user_id=None):
    """
    Allows an authenticated user to post a discussion or reply to one for a Student Event.
    Broadcasts the update via WebSockets.
    """
    data = json.loads(request.body.decode('utf-8'))
    student_event_id = data.get('student_event_id')
    discussion_text = data.get('text')
    parent_discussion_id = data.get('parent_discussion_id', None)

    # 1. Validate incoming data
    if not student_event_id or not discussion_text:
        return JsonResponse({"status": "error", "message": "Student Event ID and discussion text are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    character_count = len(discussion_text)
    if character_count > 2000:
        return JsonResponse({"status": "error", "message": "Discussion text is too long. Keep it under 2000 characters."}, status=status.HTTP_400_BAD_REQUEST)

    # 2. Get Authenticated Student
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated student not found in your database.'}, status=status.HTTP_404_NOT_FOUND)

    # 3. Get the Target Student Event
    try:
        student_event = await Student_Events.objects.aget(pk=student_event_id)
    except Student_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student event not found.'}, status=status.HTTP_404_NOT_FOUND)

    # 4. Handle Parent Discussion (for replies)
    parent_discussion_instance = None
    if parent_discussion_id:
        try:
            parent_discussion_instance = await Student_Events_Discussion.objects.select_related('student_event').aget(id=parent_discussion_id)
            if parent_discussion_instance.student_event != student_event:
                return JsonResponse({"status": "error", "message": "Parent discussion does not belong to this student event."}, status=status.HTTP_400_BAD_REQUEST)
        except Student_Events_Discussion.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Parent discussion not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Find mentions before creating discussion
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(discussion_text)
        
        # Define the database operations as a synchronous function
        def create_discussion_with_mentions():
            with transaction.atomic():
                # 5. Create the Discussion
                new_discussion = Student_Events_Discussion.objects.create(
                    student_event=student_event,
                    student=student,
                    discussion_text=discussion_text,
                    parent=parent_discussion_instance
                )
                
                # Add mentions to the discussion
                if mentioned_students.exists():
                    new_discussion.student_mentions.set(mentioned_students)
                if mentioned_communities.exists():
                    new_discussion.community_mentions.set(mentioned_communities)
                
                return new_discussion
        
        # Execute the database operations synchronously within async context
        new_discussion = await sync_to_async(create_discussion_with_mentions)()
        
        # 6. Prepare data for WebSocket Broadcast
        updated_event = await Student_Events.objects.annotate(
            discussion_count=Count('student_events_discussion')
        ).aget(pk=student_event_id)
        
        # Fetch all top-level discussions for the event to send the updated tree
        top_level_discussions_queryset = Student_Events_Discussion.objects.filter(
            student_event=student_event, parent__isnull=True
        ).select_related('student').prefetch_related('replies__student', 'student_mentions', 'community_mentions').order_by('sent_at')
        top_level_discussions = [discussion async for discussion in top_level_discussions_queryset]
        
        serializer_context = {'kinde_user_id': kinde_user_id} 
        
        # Serialize the full discussion tree
        discussion_tree_data = await sync_to_async(lambda: StudentEventDiscussionSerializer(top_level_discussions, many=True, context=serializer_context).data)()
        
        # Serialize the updated event data
        updated_event_data = await sync_to_async(lambda: StudentEventSerializer(updated_event, context=serializer_context).data)()
        
        # Add the serialized discussion tree to the event data for broadcasting
        updated_event_data['student_events_discussion'] = discussion_tree_data

        # 7. WebSocket Broadcast
        channel_layer = get_channel_layer()
        if channel_layer:
            try:
                # Broadcast to the specific event's detail page group
                await channel_layer.group_send(
                    f'event_updates_{student_event_id}',
                    {
                        'type': 'event_updated',
                        'event_type': 'student_event',
                        'event_data': updated_event_data
                    }
                )
                
                # Broadcast to the general feed update group
                await channel_layer.group_send(
                    'global_feed_updates',
                    {
                        'type': 'feed.update',
                        'data': {
                            'update_type': 'event_discussion_added',
                            'content_type': 'student_event',
                            'item_id': student_event_id,
                            'item_data': updated_event_data
                        }
                    }
                )
                
                # Broadcast to the student's events list group
                await channel_layer.group_send(
                    f'student_events_list_updates_{student_event.student.id}',
                    {
                        'type': 'event_updated',
                        'event_data': updated_event_data
                    }
                )
                
                # Broadcast to all users' events feeds
                broadcast_event_update_to_feeds(updated_event_data, 'student_event')
            except Exception as e:
                print(f"Error broadcasting student event discussion update via WebSocket: {e}")

        # 8. Return response to client
        new_discussion_data = await sync_to_async(lambda: StudentEventDiscussionSerializer(new_discussion, context=serializer_context).data)()
        
        return JsonResponse({
            "status": "success", 
            "message": "Discussion posted successfully.",
            "discussion": new_discussion_data
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error",
            "message": f"Failed to create discussion: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
@csrf_exempt
@kinde_auth_required
async def post_community_events_discussion(request, kinde_user_id=None):
    """
    Allows an authenticated user to post a discussion or reply to one for a Community Event.
    Broadcasts the update via WebSockets.
    """
    data = json.loads(request.body.decode('utf-8'))
    community_event_id = data.get('community_event_id')
    discussion_text = data.get('text')
    parent_discussion_id = data.get('parent_discussion_id', None)

    # 1. Validate incoming data
    if not community_event_id or not discussion_text:
        return JsonResponse({"status": "error", "message": "Community Event ID and discussion text are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    character_count = len(discussion_text)
    if character_count > 2000:
        return JsonResponse({"status": "error", "message": "Discussion text is too long. Keep it under 2000 characters."}, status=status.HTTP_400_BAD_REQUEST)

    # 2. Get Authenticated Student
    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication failed: Kinde User ID not provided.'}, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated student not found in your database.'}, status=status.HTTP_404_NOT_FOUND)

    # 3. Get the Target Community Event
    try:
        community_event = await Community_Events.objects.aget(pk=community_event_id)
    except Community_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community Event not found.'}, status=status.HTTP_404_NOT_FOUND)

    # 4. Handle Parent Discussion (for replies)
    parent_discussion_instance = None
    if parent_discussion_id:
        try:
            parent_discussion_instance = await Community_Events_Discussion.objects.select_related('community_event').aget(id=parent_discussion_id)
            if parent_discussion_instance.community_event != community_event:
                return JsonResponse({"status": "error", "message": "Parent discussion does not belong to this community event."}, status=status.HTTP_400_BAD_REQUEST)
        except Community_Events_Discussion.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Parent discussion not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        # Find mentions before creating discussion
        mentioned_students, mentioned_communities = await sync_to_async(find_mentions)(discussion_text)
        
        # Define the database operations as a synchronous function
        def create_community_discussion_with_mentions():
            with transaction.atomic():
                # 5. Create the Discussion
                new_discussion = Community_Events_Discussion.objects.create(
                    community_event=community_event,
                    student=student,
                    discussion_text=discussion_text,
                    parent=parent_discussion_instance
                )
                
                # Add mentions to the discussion
                if mentioned_students.exists():
                    new_discussion.student_mentions.set(mentioned_students)
                if mentioned_communities.exists():
                    new_discussion.community_mentions.set(mentioned_communities)
                
                return new_discussion
        
        # Execute the database operations synchronously within async context
        new_discussion = await sync_to_async(create_community_discussion_with_mentions)()
        
        # 6. Prepare data for WebSocket Broadcast
        updated_event = await Community_Events.objects.annotate(
            discussion_count=Count('community_events_discussion')
        ).aget(pk=community_event_id)
        
        # Fetch all top-level discussions for the event to send the updated tree
        top_level_discussions_queryset = Community_Events_Discussion.objects.filter(
            community_event=community_event, parent__isnull=True
        ).select_related('student', 'community_event__community').prefetch_related('replies__student', 'student_mentions', 'community_mentions').order_by('sent_at')
        top_level_discussions = [discussion async for discussion in top_level_discussions_queryset]
        
        serializer_context = {'kinde_user_id': kinde_user_id} 
        
        # Serialize the full discussion tree
        discussion_tree_data = await sync_to_async(lambda: CommunityEventDiscussionSerializer(top_level_discussions, many=True, context=serializer_context).data)()
        
        # Serialize the updated event data
        updated_event_data = await sync_to_async(lambda: CommunityEventsSerializer(updated_event, context=serializer_context).data)()
        
        # Add the serialized discussion tree to the event data for broadcasting
        updated_event_data['community_events_discussion'] = discussion_tree_data

        # 7. WebSocket Broadcast
        channel_layer = get_channel_layer()
        if channel_layer:
            try:
                # Broadcast to the specific community event's detail page group
                await channel_layer.group_send(
                    f'community_event_updates_{community_event_id}',
                    {
                        'type': 'community_event_updated',
                        'event_data': updated_event_data
                    }
                )
                
                # Broadcast to the general feed update group
                await channel_layer.group_send(
                    'global_feed_updates',
                    {
                        'type': 'feed.update',
                        'data': {
                            'update_type': 'event_discussion_added',
                            'content_type': 'community_event',
                            'item_id': community_event_id,
                            'item_data': updated_event_data
                        }
                    }
                )
                
                # Broadcast to the community's events list group
                await channel_layer.group_send(
                    f'community_events_list_updates_{community_event.community.id}',
                    {
                        'type': 'community_event_updated',
                        'event_data': updated_event_data
                    }
                )
                
                # Broadcast to all users' events feeds
                broadcast_event_update_to_feeds(updated_event_data, 'community_event')
            except Exception as e:
                print(f"Error broadcasting community event discussion update via WebSocket: {e}")

        # 8. Return response to client
        new_discussion_data = await sync_to_async(lambda: CommunityEventDiscussionSerializer(new_discussion, context=serializer_context).data)()
        
        return JsonResponse({
            "status": "success", 
            "message": "Discussion added successfully.",
            "discussion": new_discussion_data
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            "status": "error",
            "message": f"Failed to create discussion: {str(e)}"
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(['GET'])
def get_shareable_content_link(request):
    """
    Generates a shareable deep link for a specific piece of content (post or event).
    """
    content_type = request.GET.get('content_type')
    object_id = request.GET.get('object_id')

    if not all([content_type, object_id]):
        return JsonResponse({'status': 'error', 'message': 'content_type and object_id are required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Check if the object exists
        model_map = {
            'post': Posts,
            'community_post': Community_Posts,
            'student_event': Student_Events,
            'community_event': Community_Events,
        }
        
        model_class = model_map.get(content_type)
        if not model_class:
            return JsonResponse({'status': 'error', 'message': 'Invalid content_type.'}, status=status.HTTP_400_BAD_REQUEST)

        # Get the object to ensure it exists
        get_object_or_404(model_class, pk=object_id)
        
        # Build the deep link URL
        base_url = f"{settings.APP_DOMAIN}/app/{content_type}/{object_id}"
        return JsonResponse({'status': 'success', 'shareable_link': base_url}, status=status.HTTP_200_OK)

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'An unexpected error occurred: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
def get_shareable_profile_link(request):
    """
    Generates a shareable deep link for a user or community profile.
    """
    profile_type = request.GET.get('profile_type')
    profile_id = request.GET.get('profile_id')

    if not all([profile_type, profile_id]):
        return JsonResponse({'status': 'error', 'message': 'profile_type and profile_id are required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        if profile_type == 'user':
            get_object_or_404(Student, pk=profile_id)
            base_url = f"{settings.APP_DOMAIN}/app/profile/user/{profile_id}"
        elif profile_type == 'community':
            get_object_or_404(Communities, pk=profile_id)
            base_url = f"{settings.APP_DOMAIN}/app/profile/community/{profile_id}"
        else:
            return JsonResponse({'status': 'error', 'message': 'Invalid profile_type.'}, status=status.HTTP_400_BAD_REQUEST)
        
        return JsonResponse({'status': 'success', 'shareable_link': base_url}, status=status.HTTP_200_OK)

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'An unexpected error occurred: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# =============================
# Delete endpoints (ownership)
# =============================

@csrf_exempt
@kinde_auth_required
async def delete_post(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        post_id = request.POST.get('post_id') or request.GET.get('post_id')
        if not post_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            post_id = body.get('post_id')
        if not post_id:
            return JsonResponse({'status': 'error', 'message': 'post_id is required.'}, status=400)
        post_obj = await Posts.objects.aget(id=post_id, student=student)
        await sync_to_async(post_obj.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Post not found or not owned by user.'}, status=404)


@csrf_exempt
@kinde_auth_required
async def delete_community_post(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        compost_id = request.POST.get('community_post_id') or request.GET.get('community_post_id')
        if not compost_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            compost_id = body.get('community_post_id')
        if not compost_id:
            return JsonResponse({'status': 'error', 'message': 'community_post_id is required.'}, status=400)
        compost = await Community_Posts.objects.select_related('community').aget(id=compost_id)
        # Require admin or secondary_admin of the community
        is_admin = await Membership.objects.filter(user=student, community=compost.community, role__in=['admin','secondary_admin']).aexists()
        if not is_admin:
            return JsonResponse({'status': 'error', 'message': 'Permission denied. Admins only.'}, status=403)
        await sync_to_async(compost.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except Community_Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community post not found.'}, status=404)


@csrf_exempt
@kinde_auth_required
async def delete_community_event(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        event_id = request.POST.get('community_event_id') or request.GET.get('community_event_id')
        if not event_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            event_id = body.get('community_event_id')
        if not event_id:
            return JsonResponse({'status': 'error', 'message': 'community_event_id is required.'}, status=400)
        ev = await Community_Events.objects.select_related('community').aget(id=event_id)
        # Require admin or secondary_admin of the community
        is_admin = await Membership.objects.filter(user=student, community=ev.community, role__in=['admin','secondary_admin']).aexists()
        if not is_admin:
            return JsonResponse({'status': 'error', 'message': 'Permission denied. Admins only.'}, status=403)
        await sync_to_async(ev.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except Community_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community event not found.'}, status=404)

@csrf_exempt
@kinde_auth_required
async def delete_student_event(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        event_id = request.POST.get('student_event_id') or request.GET.get('student_event_id')
        if not event_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            event_id = body.get('student_event_id')
        if not event_id:
            return JsonResponse({'status': 'error', 'message': 'student_event_id is required.'}, status=400)
        ev = await Student_Events.objects.aget(id=event_id, student=student)
        await sync_to_async(ev.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except Student_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student event not found or not owned by user.'}, status=404)


@csrf_exempt
@kinde_auth_required
async def delete_post_comment(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        comment_id = request.POST.get('post_comment_id') or request.GET.get('post_comment_id')
        if not comment_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            comment_id = body.get('post_comment_id')
        if not comment_id:
            return JsonResponse({'status': 'error', 'message': 'post_comment_id is required.'}, status=400)
        c = await PostComment.objects.select_related('student', 'post__student').aget(id=comment_id)
        
        # Check moderation privileges: comment author OR post creator
        is_comment_author = c.student == student
        is_post_creator = c.post.student == student
        
        if not (is_comment_author or is_post_creator):
            return JsonResponse({'status': 'error', 'message': 'Permission denied. Only comment author or post creator can delete.'}, status=403)
        
        await sync_to_async(c.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except PostComment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Post comment not found.'}, status=404)


@csrf_exempt
@kinde_auth_required
async def delete_community_post_comment(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        comment_id = request.POST.get('community_post_comment_id') or request.GET.get('community_post_comment_id')
        if not comment_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            comment_id = body.get('community_post_comment_id')
        if not comment_id:
            return JsonResponse({'status': 'error', 'message': 'community_post_comment_id is required.'}, status=400)
        c = await Community_Posts_Comment.objects.select_related('student', 'community_post__community', 'community_post__poster').aget(id=comment_id)
        
        # Check moderation privileges: comment author OR post creator OR community admin
        is_comment_author = c.student == student
        is_post_creator = c.community_post.poster == student
        is_community_admin = await Membership.objects.filter(
            user=student, 
            community=c.community_post.community, 
            role__in=['admin','secondary_admin']
        ).aexists()
        
        if not (is_comment_author or is_post_creator or is_community_admin):
            return JsonResponse({'status': 'error', 'message': 'Permission denied. Only comment author, post creator, or community admin can delete.'}, status=403)
        
        await sync_to_async(c.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except Community_Posts_Comment.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community post comment not found.'}, status=404)


@csrf_exempt
@kinde_auth_required
async def delete_student_event_discussion(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        disc_id = request.POST.get('student_event_discussion_id') or request.GET.get('student_event_discussion_id')
        if not disc_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            disc_id = body.get('student_event_discussion_id')
        if not disc_id:
            return JsonResponse({'status': 'error', 'message': 'student_event_discussion_id is required.'}, status=400)
        d = await Student_Events_Discussion.objects.select_related('student', 'student_event__student').aget(id=disc_id)
        
        # Check moderation privileges: discussion author OR event creator
        is_discussion_author = d.student == student
        is_event_creator = d.student_event.student == student
        
        if not (is_discussion_author or is_event_creator):
            return JsonResponse({'status': 'error', 'message': 'Permission denied. Only discussion author or event creator can delete.'}, status=403)
        
        await sync_to_async(d.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except Student_Events_Discussion.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student event discussion not found.'}, status=404)


@csrf_exempt
@kinde_auth_required
async def delete_community_event_discussion(request, kinde_user_id=None):
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)
    try:
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)
    try:
        disc_id = request.POST.get('community_event_discussion_id') or request.GET.get('community_event_discussion_id')
        if not disc_id and request.body:
            import json as _json
            body = _json.loads(request.body.decode('utf-8')) if request.body else {}
            disc_id = body.get('community_event_discussion_id')
        if not disc_id:
            return JsonResponse({'status': 'error', 'message': 'community_event_discussion_id is required.'}, status=400)
        d = await Community_Events_Discussion.objects.select_related('student', 'community_event__community', 'community_event__poster').aget(id=disc_id)
        
        # Check moderation privileges: discussion author OR event creator OR community admin
        is_discussion_author = d.student == student
        is_event_creator = d.community_event.poster == student
        is_community_admin = await Membership.objects.filter(
            user=student, 
            community=d.community_event.community, 
            role__in=['admin','secondary_admin']
        ).aexists()
        
        if not (is_discussion_author or is_event_creator or is_community_admin):
            return JsonResponse({'status': 'error', 'message': 'Permission denied. Only discussion author, event creator, or community admin can delete.'}, status=403)
        
        await sync_to_async(d.delete)()
        return JsonResponse({'status': 'success'}, status=200)
    except Community_Events_Discussion.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community event discussion not found.'}, status=404)


#########################################################################
# Views to get people who liked or RSVPed to posts and events
#########################################################################

@csrf_exempt
@kinde_auth_required
async def get_post_likes(request, kinde_user_id=None):
    """
    Get list of users who liked a post.
    GET: use query param post_id. POST: use JSON body { "post_id": ... }.
    """
    if request.method == 'GET':
        post_id = request.GET.get('post_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        post_id = data.get('post_id')

    if not post_id:
        return JsonResponse({'status': 'error', 'message': 'post_id is required.'}, status=400)

    try:
        # Get the requesting student
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
        
        # Get the post
        post = await Posts.objects.select_related('student').aget(id=post_id)
        
        # Check if the requesting user is the post owner
        # if post.student != student:
        #     return JsonResponse({'status': 'error', 'message': 'Permission denied. Only the post owner can view likes.'}, status=403)
        
        # Get student IDs with pending deletion requests
        pending_deletion_ids = await sync_to_async(_get_pending_deletion_student_ids)()
        
        # Get all likes for this post, excluding users with pending deletion
        likes = await sync_to_async(list)(
            PostLike.objects.filter(post=post).exclude(student__id__in=pending_deletion_ids).select_related('student').order_by('-liked_at')
        )
        
        # Serialize the students who liked
        liked_by_students = [like.student for like in likes]
        students_data = await sync_to_async(lambda: StudentChatSerializer(liked_by_students, many=True).data)()
        
        # Add liked_at timestamp to each student
        for i, student_data in enumerate(students_data):
            student_data['liked_at'] = likes[i].liked_at.isoformat()
        
        return JsonResponse({
            'status': 'success',
            'post_id': post_id,
            'total_likes': len(likes),
            'liked_by': students_data
        }, status=200)
        
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)
    except Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Post not found.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@csrf_exempt
@kinde_auth_required
async def get_community_post_likes(request, kinde_user_id=None):
    """
    Get list of users who liked a community post.
    GET: use query param community_post_id. POST: use JSON body { "community_post_id": ... }.
    """
    if request.method == 'GET':
        post_id = request.GET.get('community_post_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        post_id = data.get('community_post_id')

    if not post_id:
        return JsonResponse({'status': 'error', 'message': 'community_post_id is required.'}, status=400)

    try:
        # Get the requesting student
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
        
        # Get the community post
        community_post = await Community_Posts.objects.select_related('community', 'poster').aget(id=post_id)
        
        # Check if the requesting user is admin or secondary_admin of the community
        # is_admin = await Membership.objects.filter(
        #     user=student,
        #     community=community_post.community,
        #     role__in=['admin', 'secondary_admin']
        # ).aexists()
        
        # if not is_admin:
        #     return JsonResponse({'status': 'error', 'message': 'Permission denied. Only community admins can view likes.'}, status=403)
        
        # Get all likes for this community post
        likes = await sync_to_async(list)(
            LikeCommunityPost.objects.filter(event=community_post).select_related('student').order_by('-liked_at')
        )
        
        # Serialize the students who liked
        liked_by_students = [like.student for like in likes]
        students_data = await sync_to_async(lambda: StudentChatSerializer(liked_by_students, many=True).data)()
        
        # Add liked_at timestamp to each student
        for i, student_data in enumerate(students_data):
            student_data['liked_at'] = likes[i].liked_at.isoformat()
        
        return JsonResponse({
            'status': 'success',
            'community_post_id': post_id,
            'community_name': community_post.community.community_name,
            'total_likes': len(likes),
            'liked_by': students_data
        }, status=200)
        
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)
    except Community_Posts.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community post not found.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)



@csrf_exempt
@kinde_auth_required
async def get_student_event_rsvps(request, kinde_user_id=None):
    """
    Get list of users who RSVPed to a student event.
    GET: use query param student_event_id. POST: use JSON body { "student_event_id": ... }.
    """
    if request.method == 'GET':
        event_id = request.GET.get('student_event_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        event_id = data.get('student_event_id')

    if not event_id:
        return JsonResponse({'status': 'error', 'message': 'student_event_id is required.'}, status=400)

    try:
        # Get the requesting student
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
        
        # Get the student event
        event = await Student_Events.objects.select_related('student').aget(id=event_id)
        
        # Check if the requesting user is the event creator
        # if event.student != student:
        #     return JsonResponse({'status': 'error', 'message': 'Permission denied. Only the event creator can view RSVPs.'}, status=403)
        
        # Get all RSVPs for this event
        rsvps = await sync_to_async(list)(
            EventRSVP.objects.filter(event=event).select_related('student').order_by('-rsvp_at')
        )
        
        # Group RSVPs by status
        rsvps_by_status = {
            'going': [],
            'interested': [],
            'not_going': []
        }
        
        for rsvp in rsvps:
            student_data = await sync_to_async(lambda: StudentChatSerializer(rsvp.student).data)()
            student_data['rsvp_at'] = rsvp.rsvp_at.isoformat()
            student_data['status'] = rsvp.status
            
            # Normalize status to lowercase to handle inconsistent data
            status_key = rsvp.status.lower() if rsvp.status else 'going'
            
            # Only add if status is valid, otherwise default to 'going'
            if status_key in rsvps_by_status:
                rsvps_by_status[status_key].append(student_data)
            else:
                # Handle unexpected status values
                rsvps_by_status['going'].append(student_data)
        
        return JsonResponse({
            'status': 'success',
            'student_event_id': event_id,
            'event_name': event.event_name,
            'total_rsvps': len(rsvps),
            'going_count': len(rsvps_by_status['going']),
            'interested_count': len(rsvps_by_status['interested']),
            'not_going_count': len(rsvps_by_status['not_going']),
            'rsvps': rsvps_by_status
        }, status=200)
        
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)
    except Student_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student event not found.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@csrf_exempt
@kinde_auth_required
async def get_community_event_rsvps(request, kinde_user_id=None):
    """
    Get list of users who RSVPed to a community event.
    GET: use query param community_event_id. POST: use JSON body { "community_event_id": ... }.
    """
    if request.method == 'GET':
        event_id = request.GET.get('community_event_id')
    else:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        event_id = data.get('community_event_id')

    if not event_id:
        return JsonResponse({'status': 'error', 'message': 'community_event_id is required.'}, status=400)

    try:
        # Get the requesting student
        student = await Student.objects.aget(kinde_user_id=kinde_user_id)
        
        # Get the community event
        event = await Community_Events.objects.select_related('community', 'poster').aget(id=event_id)
        
        # Check if the requesting user is admin or secondary_admin of the community
        # is_admin = await Membership.objects.filter(
        #     user=student,
        #     community=event.community,
        #     role__in=['admin', 'secondary_admin']
        # ).aexists()
        
        # if not is_admin:
        #     return JsonResponse({'status': 'error', 'message': 'Permission denied. Only community admins can view RSVPs.'}, status=403)
        
        # Get all RSVPs for this event
        rsvps = await sync_to_async(list)(
            CommunityEventRSVP.objects.filter(event=event).select_related('student').order_by('-rsvp_at')
        )
        
        # Group RSVPs by status
        rsvps_by_status = {
            'going': [],
            'interested': [],
            'not_going': []
        }
        
        for rsvp in rsvps:
            student_data = await sync_to_async(lambda: StudentChatSerializer(rsvp.student).data)()
            student_data['rsvp_at'] = rsvp.rsvp_at.isoformat()
            student_data['status'] = rsvp.status
            
            # Normalize status to lowercase to handle inconsistent data
            status_key = rsvp.status.lower() if rsvp.status else 'going'
            
            # Only add if status is valid, otherwise default to 'going'
            if status_key in rsvps_by_status:
                rsvps_by_status[status_key].append(student_data)
            else:
                # Handle unexpected status values
                rsvps_by_status['going'].append(student_data)
        
        return JsonResponse({
            'status': 'success',
            'community_event_id': event_id,
            'event_name': event.event_name,
            'community_name': event.community.community_name,
            'total_rsvps': len(rsvps),
            'going_count': len(rsvps_by_status['going']),
            'interested_count': len(rsvps_by_status['interested']),
            'not_going_count': len(rsvps_by_status['not_going']),
            'rsvps': rsvps_by_status
        }, status=200)
        
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Student not found.'}, status=404)
    except Community_Events.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Community event not found.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

@api_view(['GET'])
def test_email(request):
    """
    Test endpoint to send an email to suleimanfayzul333@gmail.com
    """
    try:
        from django.core.mail import send_mail
        from django.conf import settings
        
        send_mail(
            'Test Email from Studico',
            'This is a test email sent from the Studico API.',
            settings.DEFAULT_FROM_EMAIL,
            ['suleimanfayzul333@gmail.com'],
            fail_silently=False,
        )
        return JsonResponse({
            'status': 'success',
            'message': 'Test email sent successfully to suleimanfayzul333@gmail.com'
        }, status=200)
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'Failed to send email: {str(e)}'
        }, status=500)



# @csrf_exempt
# async def invalidate_token_cache(request):
#     auth_header = request.headers.get("Authorization", "")
#     if "IDBearer" in auth_header:
#         token = auth_header.split("IDBearer")[1].split(';')[0].strip()
#         invalidate_kinde_token_cache(token)
#     return JsonResponse({'status': 'success', 'message': 'Token cache invalidated'})

@csrf_exempt
@kinde_auth_required
async def get_suggestions(request, kinde_user_id=None):
    """
    Get personalized suggestions for students and communities the user might know,
    want to view, or connect with. Suggestions are based on:
    - Shared interests
    - Same university
    - Same location/region
    - Same course
    - Mutual friends
    - Community popularity and friend membership
    
    Excludes: already friends, blocked/muted users, already joined communities
    """
    if not kinde_user_id:
        return JsonResponse({
            'status': 'error',
            'message': 'Authentication failed: Kinde User ID not provided.'
        }, status=status.HTTP_401_UNAUTHORIZED)
    
    try:
        # Fetch authenticated student
        try:
            student = await Student.objects.select_related(
                'university', 'student_location__region', 'course'
            ).prefetch_related('student_interest').aget(kinde_user_id=kinde_user_id)
        except Student.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'message': 'Authenticated user not found.'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Parse pagination parameters
        limit, offset = _parse_pagination_params(request, default_limit=10, max_limit=50)
        
        # Fetch more items than needed so we can mix and sort them properly
        # We'll fetch limit * 2 items of each type to ensure we have enough for mixing
        fetch_limit = max(limit * 2, 20)  # Fetch at least 20 of each type
        
        # Get user's data for filtering and scoring
        user_interest_ids = await sync_to_async(list)(
            student.student_interest.values_list('id', flat=True)
        )
        user_university_id = student.university.id if student.university else None
        user_location_id = student.student_location.id if student.student_location else None
        user_region_id = (
            student.student_location.region.id 
            if student.student_location and student.student_location.region 
            else None
        )
        user_course_id = student.course.id if student.course else None
        
        # Get relationship snapshot for exclusions
        relationship_snapshot = await sync_to_async(get_relationship_snapshot)(student.id)
        
        # Get accepted friends (exclude these from suggestions)
        friendship_queryset = Friendship.objects.filter(
            Q(sender=student, status='accepted') | Q(receiver=student, status='accepted')
        ).annotate(
            friend_id=Case(
                When(sender=student, then=F('receiver_id')),
                default=F('sender_id'),
                output_field=IntegerField()
            )
        )
        accepted_friend_ids = await sync_to_async(list)(
            friendship_queryset.values_list('friend_id', flat=True)
        )
    
        # Get pending friend requests (exclude these too)
        pending_sent_ids = await sync_to_async(list)(
            Friendship.objects.filter(
                sender=student, status='pending'
            ).values_list('receiver_id', flat=True)
        )
        pending_received_ids = await sync_to_async(list)(
            Friendship.objects.filter(
                receiver=student, status='pending'
            ).values_list('sender_id', flat=True)
        )
    
        # Get blocked/muted IDs
        blocked_student_ids = (
            set(relationship_snapshot.get('blocking', [])) | 
            set(relationship_snapshot.get('blocked_by', []))
        )
        muted_student_ids = set(relationship_snapshot.get('muted_students', []))
    
        # Combine all exclusions for students
        excluded_student_ids = (
            {student.id} |
            set(accepted_friend_ids) |
            set(pending_sent_ids) | 
            set(pending_received_ids) |
            blocked_student_ids |
            muted_student_ids
        )
    
        # Get communities user is already a member of
        user_community_ids = await sync_to_async(list)(
            Membership.objects.filter(user=student).values_list('community_id', flat=True)
        )
    
        # Get muted and blocked communities
        muted_community_ids = set(relationship_snapshot.get('muted_communities', []))
        community_that_blocked_me_ids = set(
            relationship_snapshot.get('blocked_by_communities', [])
        )
    
        excluded_community_ids = (
            set(user_community_ids) |
            muted_community_ids |
            community_that_blocked_me_ids
        )
    
        # --- STUDENT SUGGESTIONS ---
    
        # Base query for student suggestions
        student_suggestions_qs = Student.objects.filter(
            is_verified=True
        ).exclude(
            id__in=excluded_student_ids
        ).select_related(
            'university', 'student_location__region', 'course'
        ).prefetch_related('student_interest')
    
        # Build annotations dynamically based on available data
        annotations = {}
        
        # Shared interests score
        if user_interest_ids:
            annotations['shared_interests_count'] = Count(
                'student_interest',
                filter=Q(student_interest__id__in=user_interest_ids),
                distinct=True
            )
        else:
            annotations['shared_interests_count'] = Value(0, output_field=IntegerField())
        
        # Same university score
        if user_university_id:
            annotations['same_university'] = Case(
                When(university_id=user_university_id, then=Value(1.0)),
                default=Value(0.0),
                output_field=FloatField()
            )
        else:
            annotations['same_university'] = Value(0.0, output_field=FloatField())
        
        # Same location score
        if user_location_id:
            annotations['same_location'] = Case(
                When(student_location_id=user_location_id, then=Value(1.0)),
                default=Value(0.0),
                output_field=FloatField()
            )
        else:
            annotations['same_location'] = Value(0.0, output_field=FloatField())
        
        # Same region score
        if user_region_id:
            annotations['same_region'] = Case(
                When(student_location__region_id=user_region_id, then=Value(1.0)),
                default=Value(0.0),
                output_field=FloatField()
            )
        else:
            annotations['same_region'] = Value(0.0, output_field=FloatField())
        
        # Same course score
        if user_course_id:
            annotations['same_course'] = Case(
                When(course_id=user_course_id, then=Value(1.0)),
                default=Value(0.0),
                output_field=FloatField()
            )
        else:
            annotations['same_course'] = Value(0.0, output_field=FloatField())
        
        # Note: Mutual friends count will be calculated separately after fetching results
        # to avoid complex subquery performance issues
        annotations['mutual_friends_count'] = Value(0, output_field=IntegerField())

        # Popularity signal: total accepted friends this student has
        annotations['total_friends_count'] = ExpressionWrapper(
            Count('sent_requests', filter=Q(sent_requests__status='accepted'), distinct=True) +
            Count('received_requests', filter=Q(received_requests__status='accepted'), distinct=True),
            output_field=FloatField()
        )
        
        # Apply all annotations
        student_suggestions_qs = student_suggestions_qs.annotate(**annotations)
        
        # Calculate final suggestion score
        student_suggestions_qs = student_suggestions_qs.annotate(
            suggestion_score=ExpressionWrapper(
                (F('shared_interests_count') * 3.0) +
                (F('same_university') * 2.0) +
                (F('same_location') * 1.5) +
                (F('same_region') * 1.0) +
                (F('same_course') * 1.5) +
                (F('mutual_friends_count') * 2.0) +
                (F('total_friends_count') * 0.3),
                output_field=FloatField()
            )
        )

        student_suggestions_qs = student_suggestions_qs.order_by('-suggestion_score', 'name')
    
        # Get student suggestions (fetch more than needed for mixing)
        total_student_suggestions = await sync_to_async(student_suggestions_qs.count)()
        student_suggestions_page = await sync_to_async(list)(
            student_suggestions_qs[:fetch_limit]
        )
    
        # Get detailed mutual friends info in batch (OPTIMIZED)
        mutual_friends_map = {}
        if student_suggestions_page and accepted_friend_ids:
            student_ids = [s.id for s in student_suggestions_page]
            
            # Single query to get all mutual friendships
            mutual_friendships = await sync_to_async(list)(
                Friendship.objects.filter(
                    Q(
                        sender_id__in=student_ids,
                        receiver_id__in=accepted_friend_ids,
                        status='accepted'
                    ) | Q(
                        receiver_id__in=student_ids,
                        sender_id__in=accepted_friend_ids,
                        status='accepted'
                    )
                ).values('sender_id', 'receiver_id')
            )
            
            # Build map of suggested_student_id -> set of mutual friend IDs
            mutual_friend_ids_by_student = {}
            for friendship in mutual_friendships:
                sender_id = friendship['sender_id']
                receiver_id = friendship['receiver_id']
                
                # Determine which is the suggested student and which is the mutual friend
                if sender_id in student_ids:
                    suggested_id = sender_id
                    mutual_id = receiver_id
                else:
                    suggested_id = receiver_id
                    mutual_id = sender_id
                
                if suggested_id not in mutual_friend_ids_by_student:
                    mutual_friend_ids_by_student[suggested_id] = set()
                mutual_friend_ids_by_student[suggested_id].add(mutual_id)
            
            # Get all mutual friend details in one query
            all_mutual_friend_ids = set()
            for friend_ids in mutual_friend_ids_by_student.values():
                all_mutual_friend_ids.update(list(friend_ids)[:3])  # Limit to 3 per student
            
            if all_mutual_friend_ids:
                # Get actual Student objects instead of values() to access ImageField.url
                mutual_friends_objects = await sync_to_async(list)(
                    Student.objects.filter(id__in=all_mutual_friend_ids)
                )
                mutual_friends_details = [
                    {
                        'id': mf.id,
                        'kinde_user_id': mf.kinde_user_id,
                        'name': mf.name,
                        'username': mf.username,
                        'profile_image': mf.profile_image.url if mf.profile_image else None
                    }
                    for mf in mutual_friends_objects
                ]
                
                # Create lookup dict
                mutual_friends_lookup = {mf['id']: mf for mf in mutual_friends_details}
                
                # Build final map
                for suggested_id, friend_ids in mutual_friend_ids_by_student.items():
                    mutual_friends_map[suggested_id] = [
                        {
                            'id': mf_id,
                            'kinde_user_id': mutual_friends_lookup[mf_id]['kinde_user_id'],
                            'name': mutual_friends_lookup[mf_id]['name'],
                            'username': mutual_friends_lookup[mf_id]['username'],
                            'profile_image': mutual_friends_lookup[mf_id]['profile_image']
                        }
                        for mf_id in list(friend_ids)[:3]
                        if mf_id in mutual_friends_lookup
                    ]
    
        # Serialize student suggestions
        def _serialize_students(data):
            return StudentSerializer(data, many=True, context={
                'kinde_user_id': kinde_user_id,
                'mutual_friends': mutual_friends_map,
                'request': request
            }).data
    
        student_suggestions_data = await sync_to_async(_serialize_students)(
            student_suggestions_page
        )
    
        # Add suggestion metadata
        for i, student_obj in enumerate(student_suggestions_page):
            if i < len(student_suggestions_data):
                student_suggestions_data[i]['suggestion_score'] = float(
                    getattr(student_obj, 'suggestion_score', 0.0) or 0.0
                )
                student_suggestions_data[i]['shared_interests_count'] = int(
                    getattr(student_obj, 'shared_interests_count', 0) or 0
                )
                student_suggestions_data[i]['same_university'] = bool(
                    getattr(student_obj, 'same_university', 0) or 0
                )
                student_suggestions_data[i]['same_location'] = bool(
                    getattr(student_obj, 'same_location', 0) or 0
                )
                student_suggestions_data[i]['same_course'] = bool(
                    getattr(student_obj, 'same_course', 0) or 0
                )
                
                # Add mutual friends info and update suggestion score
                mutual_friends_list = mutual_friends_map.get(student_obj.id, [])
                actual_mutual_count = len(mutual_friends_list)
                student_suggestions_data[i]['mutual_friends_count'] = actual_mutual_count
                
                # Update suggestion score with actual mutual friends count
                # (mutual friends weight is 2.0, so add the difference)
                if actual_mutual_count > 0:
                    current_score = student_suggestions_data[i].get('suggestion_score', 0.0)
                    student_suggestions_data[i]['suggestion_score'] = current_score + (actual_mutual_count * 2.0)
                
                if mutual_friends_list:
                    student_suggestions_data[i]['mutual_friends_sample'] = (
                        mutual_friends_list[:2]
                    )
    
        # --- COMMUNITY SUGGESTIONS ---
    
        # Base query for community suggestions
        community_suggestions_qs = Communities.objects.exclude(
            id__in=excluded_community_ids
        ).select_related(
            'location__region'
        ).prefetch_related(
            'community_interest'
        ).annotate(
            member_count=Count('membership', distinct=True)
        )
    
        # Apply interest overlap scoring
        if user_interest_ids:
            community_suggestions_qs = community_suggestions_qs.annotate(
                **get_interest_overlap_annotations(user_interest_ids, 'community_interest')
            )
        else:
            community_suggestions_qs = community_suggestions_qs.annotate(
                interest_match_score=Value(0.0, output_field=FloatField())
            )
    
        # Apply location match scoring
        if user_region_id:
            community_suggestions_qs = community_suggestions_qs.annotate(
                **get_location_match_annotations(user_region_id, 'location')
            )
        else:
            community_suggestions_qs = community_suggestions_qs.annotate(
                location_score=Value(0.0, output_field=FloatField())
            )
    
        # Count friends in community
        if accepted_friend_ids:
            community_suggestions_qs = community_suggestions_qs.annotate(
                friends_in_community_count=Count(
                    'membership',
                    filter=Q(membership__user_id__in=accepted_friend_ids),
                    distinct=True
                )
            )
        else:
            community_suggestions_qs = community_suggestions_qs.annotate(
                friends_in_community_count=Value(0, output_field=IntegerField())
            )
        
        # Calculate suggestion score
        community_suggestions_qs = community_suggestions_qs.annotate(
            suggestion_score=ExpressionWrapper(
                (F('interest_match_score') * 3.0) +
                (F('location_score') * 2.0) +
                (F('friends_in_community_count') * 5.0) +
                (Coalesce(F('member_count'), Value(0), output_field=FloatField()) * 0.1),
                output_field=FloatField()
            )
        )
    
        # Order by suggestion score
        community_suggestions_qs = community_suggestions_qs.filter(
            suggestion_score__gt=0
        ).order_by('-suggestion_score', 'community_name')
    
        # Get community suggestions (fetch more than needed for mixing)
        total_community_suggestions = await sync_to_async(
            community_suggestions_qs.count
        )()
        community_suggestions_page = await sync_to_async(list)(
            community_suggestions_qs[:fetch_limit]
        )
    
        # Get friends in community details (OPTIMIZED)
        friends_in_community_map = {}
        if community_suggestions_page and accepted_friend_ids:
            community_ids = [c.id for c in community_suggestions_page]
            
            # Get friend memberships with actual user objects to access ImageField.url
            friend_memberships_objects = await sync_to_async(list)(
                Membership.objects.filter(
                    community_id__in=community_ids,
                    user_id__in=accepted_friend_ids
                ).select_related('user')
            )
            
            # Group by community
            for membership_obj in friend_memberships_objects:
                comm_id = membership_obj.community_id
                if comm_id not in friends_in_community_map:
                    friends_in_community_map[comm_id] = []
                
                user = membership_obj.user
                friends_in_community_map[comm_id].append({
                    'id': user.id,
                    'kinde_user_id': user.kinde_user_id,
                    'name': user.name,
                    'username': user.username,
                    'profile_image': user.profile_image.url if user.profile_image else None
                })
    
        # Prepare context for community serialization
        user_memberships = set(user_community_ids)
        user_muted_communities = muted_community_ids
        user_blocked_by_communities = community_that_blocked_me_ids
        user_community_roles = dict(
            await sync_to_async(list)(
                Membership.objects.filter(user=student).values_list(
                    'community_id', 'role'
                )
            )
        )
    
        # Serialize community suggestions
        def _serialize_communities(data):
            return CommunitySerializer(data, many=True, context={
                'kinde_user_id': kinde_user_id,
                'user_memberships': user_memberships,
                'user_muted_communities': user_muted_communities,
                'user_blocked_by_communities': user_blocked_by_communities,
                'user_community_roles': user_community_roles,
                'friends_in_community': friends_in_community_map,
                'request': request
            }).data
    
        community_suggestions_data = await sync_to_async(_serialize_communities)(
            community_suggestions_page
        )
    
        # Add suggestion metadata
        for i, community_obj in enumerate(community_suggestions_page):
            if i < len(community_suggestions_data):
                community_suggestions_data[i]['suggestion_score'] = float(
                    getattr(community_obj, 'suggestion_score', 0.0) or 0.0
                )
                community_suggestions_data[i]['friends_in_community_count'] = int(
                    getattr(community_obj, 'friends_in_community_count', 0) or 0
                )
                community_suggestions_data[i]['member_count'] = int(
                    getattr(community_obj, 'member_count', 0) or 0
                )
                
                # Add friends sample
                friends_list = friends_in_community_map.get(community_obj.id, [])
                if friends_list:
                    community_suggestions_data[i]['friends_in_community_sample'] = (
                        friends_list[:2]
                    )
        
        # Combine students and communities into a mixed list
        # Add type field to each item
        for student_item in student_suggestions_data:
            student_item['type'] = 'student'
        
        for community_item in community_suggestions_data:
            community_item['type'] = 'community'
        
        # Combine and sort by suggestion_score (highest first)
        mixed_results = student_suggestions_data + community_suggestions_data
        
        # Additional safety filter: Remove any items that shouldn't be suggested
        # (friends, communities user is a member of, blocked, muted, etc.)
        filtered_results = []
        
        for item in mixed_results:
            if item['type'] == 'student':
                student_id = item.get('id')
                # Exclude if: already a friend, blocked, muted, self, or has pending request
                if (student_id and 
                    student_id not in excluded_student_ids and
                    not item.get('is_blocked', False) and
                    not item.get('is_muted', False)):
                    filtered_results.append(item)
            elif item['type'] == 'community':
                community_id = item.get('id')
                # Exclude if: already a member, muted, or blocked by community
                if (community_id and 
                    community_id not in excluded_community_ids and
                    not item.get('is_member', False) and
                    not item.get('is_muted', False) and
                    not item.get('is_blocked_by_community', False)):
                    filtered_results.append(item)
        
        # Sort by suggestion_score (highest first)
        filtered_results.sort(key=lambda x: x.get('suggestion_score', 0.0), reverse=True)
        
        # Apply pagination to the filtered mixed list
        total_mixed_count = len(filtered_results)
        paginated_mixed = filtered_results[offset:offset + limit]
        next_offset = offset + limit if (offset + limit) < total_mixed_count else None
        
        return JsonResponse({
            'status': 'success',
            'results': paginated_mixed,
            'count': len(paginated_mixed),
            'total_count': total_mixed_count,
            'limit': limit,
            'offset': offset,
            'next_offset': next_offset,
            'has_next': next_offset is not None
        }, status=status.HTTP_200_OK)
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            'status': 'error',
            'message': f'Failed to get suggestions: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST', 'DELETE'])
@kinde_auth_required
def request_data_deletion(request, kinde_user_id=None):
    """
    Endpoint for a user to request their data to be removed from the app.
    Data will be retained for 30 days before permanent deletion.
    The Kinde authentication account is deleted immediately.
    This action cannot be cancelled once submitted.
    
    Accepts both POST and DELETE methods for flexibility.
    """
    try:
        # Get the student
        try:
            student = Student.objects.get(kinde_user_id=kinde_user_id)
        except Student.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'message': 'User not found.'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Check if there's already a pending deletion request
        existing_request = DataDeletionRequest.objects.filter(
            student=student,
            is_cancelled=False
        ).first()
        
        if existing_request:
            return JsonResponse({
                'status': 'error',
                'message': 'You already have a pending deletion request. Your data will be deleted on {}.'.format(
                    existing_request.scheduled_deletion_date.strftime('%Y-%m-%d')
                ),
                'scheduled_deletion_date': existing_request.scheduled_deletion_date.isoformat()
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Create deletion request - data will be deleted after 30 days
        with transaction.atomic():
            scheduled_deletion_date = timezone.now() + timedelta(days=30)
            deletion_request = DataDeletionRequest.objects.create(
                student=student,
                scheduled_deletion_date=scheduled_deletion_date
            )
            
            logger.info(f"Data deletion requested for user {student.id} ({student.name}). Scheduled for {scheduled_deletion_date}")
            
            # Delete user from Kinde immediately (if kinde_user_id exists)
            if student.kinde_user_id:
                try:
                    from .kinde_functions import delete_kinde_user
                    delete_kinde_user(student.kinde_user_id)
                    logger.info(f"Successfully deleted Kinde user {student.kinde_user_id} for student {student.id}")
                except Exception as kinde_error:
                    # Log error but don't fail the request
                    # Database deletion request already created
                    logger.error(f"Failed to delete Kinde user {student.kinde_user_id} for student {student.id}: {str(kinde_error)}")
                    # Continue - deletion request is still created
            
            # Send email notifications
            try:
                # Send email to user
                send_account_deletion_notification.delay(
                    student.email,
                    student.name,
                    scheduled_deletion_date
                )
                
                # Send email to admin
                send_admin_deletion_notification.delay(
                    student.email,
                    student.name,
                    student.username or 'N/A',
                    scheduled_deletion_date
                )
            except Exception as e:
                logger.error(f"Failed to send deletion notification emails: {str(e)}")
                # Don't fail the request if email fails
            
            return JsonResponse({
                'status': 'success',
                'message': 'Your data deletion request has been received. Your data will be permanently deleted after 30 days (on {}). This action cannot be cancelled.'.format(
                    scheduled_deletion_date.strftime('%Y-%m-%d')
                ),
                'scheduled_deletion_date': scheduled_deletion_date.isoformat(),
                'request_id': deletion_request.id
            }, status=status.HTTP_200_OK)
            
    except Exception as e:
        logger.error(f"Error creating data deletion request: {str(e)}")
        import traceback
        traceback.print_exc()
        return JsonResponse({
            'status': 'error',
            'message': f'Failed to create deletion request: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# Cancel deletion endpoint removed - deletion requests cannot be cancelled


def _perform_data_deletion(student):
    """
    Internal function to actually delete all user data.
    This is called by the Celery task after the 30-day retention period.
    """
    with transaction.atomic():
        student_id = student.id
        student_name = student.name  # For logging
        
        # 1. Anonymize comments that use SET_NULL (for GDPR compliance)
        # These will be set to null when student is deleted, but we anonymize them first
        PostComment.objects.filter(student=student).update(
            comment="[Deleted]"
        )
        Community_Posts_Comment.objects.filter(student=student).update(
            comment_text="[Deleted]"
        )
        
        # Delete event discussions (these use CASCADE but we delete them explicitly for clarity)
        Student_Events_Discussion.objects.filter(student=student).delete()
        Community_Events_Discussion.objects.filter(student=student).delete()
        
        # 2. Delete all user-created content (CASCADE will handle related objects)
        # Posts and related images/videos
        Posts.objects.filter(student=student).delete()
        
        # Student Events and related images/videos
        Student_Events.objects.filter(student=student).delete()
        
        # Community Posts (where user is poster)
        Community_Posts.objects.filter(poster=student).delete()
        
        # Community Events (where user is poster)
        Community_Events.objects.filter(poster=student).delete()
        
        # 3. Delete user interactions
        # Likes
        PostLike.objects.filter(student=student).delete()
        LikeCommunityPost.objects.filter(student=student).delete()
        LikeEvent.objects.filter(student=student).delete()
        LikeCommunityEvent.objects.filter(student=student).delete()
        
        # Bookmarks
        BookmarkedPosts.objects.filter(student=student).delete()
        BookmarkedCommunityPosts.objects.filter(student=student).delete()
        BookmarkedStudentEvents.objects.filter(student=student).delete()
        BookmarkedCommunityEvents.objects.filter(student=student).delete()
        
        # RSVPs
        EventRSVP.objects.filter(student=student).delete()
        CommunityEventRSVP.objects.filter(student=student).delete()
        
        # 4. Delete social connections
        # Friendships (both sent and received)
        Friendship.objects.filter(
            Q(sender=student) | Q(receiver=student)
        ).delete()
        
        # Memberships
        Membership.objects.filter(user=student).delete()
        
        # Blocked/Muted relationships
        Block.objects.filter(
            Q(blocker=student) | Q(blocked=student)
        ).delete()
        MutedStudents.objects.filter(
            Q(student=student) | Q(muted_student=student)
        ).delete()
        MutedCommunities.objects.filter(student=student).delete()
        BlockedByCommunities.objects.filter(blocked_student=student).delete()
        
        # 5. Delete messages and notifications
        DirectMessage.objects.filter(
            Q(sender=student) | Q(receiver=student)
        ).delete()
        Notification.objects.filter(
            Q(recipient=student) | Q(sender=student)
        ).delete()
        CommunityChatMessage.objects.filter(student=student).delete()
        
        # 6. Delete device tokens
        DeviceToken.objects.filter(user=student).delete()
        
        # 7. Delete email verification records
        EmailVerification.objects.filter(student=student).delete()
        
        # 8. Delete saved posts/events
        SavedPost.objects.filter(student=student).delete()
        SavedCommunityPost.objects.filter(student=student).delete()
        SavedStudentEvents.objects.filter(student=student).delete()
        
        # 9. Delete reports made by user
        Report.objects.filter(user=student).delete()
        
        # 10. Clear ManyToMany relationships (mentions, interests, etc.)
        student.student_interest.clear()
        student.student_mentions.clear()
        student.community_mentions.clear()
        
        # 11. Finally, delete the Student record itself
        student.delete()
        
        logger.info(f"Successfully deleted all data for user {student_id} ({student_name})")



def poster_a5_flyer(request):
    return render(request, 'poster.html')


def student_ads_page(request):
    """Static-style page for student ads: what is Studico. Downloadable for campaigns."""
    return render(request, 'student_ads.html')


def studico_posters(request):
    """Single-page Instagram-style poster carousel: 5 bold panels (save as PDF = one page)."""
    return render(request, 'studico_posters.html')


def studico_posters_light(request):
    """Light-themed version of the Studico posters page."""
    return render(request, 'studico_posters_light.html')


# ==========================
# Group Chat REST Endpoints
# ==========================

@csrf_exempt
@kinde_auth_required
async def create_group_chat(request, kinde_user_id=None):
    """
    Create a new standalone group chat.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    # Parse body (JSON or form-encoded)
    data = {}
    if request.body and request.content_type and 'application/json' in request.content_type:
        import json as _json
        try:
            data = _json.loads(request.body.decode('utf-8'))
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON body.'}, status=400)
    else:
        data = request.POST

    name = data.get('name')
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Group name is required.'}, status=400)

    description = data.get('description') or ''
    raw_member_ids = data.get('member_ids', [])

    # member_ids can be list (from JSON) or comma-separated string
    if isinstance(raw_member_ids, str):
        member_ids = [mid for mid in raw_member_ids.split(',') if mid.strip()]
    else:
        member_ids = raw_member_ids or []

    image_file = request.FILES.get('image')

    @sync_to_async
    def _create_group_and_members():
        group = GroupChat.objects.create(
            name=name,
            description=description,
            image=image_file,
            created_by=me,
        )
        # Creator is owner
        GroupChatMembership.objects.create(
            group=group,
            member=me,
            role='owner',
        )

        # Add initial members (as regular members)
        if member_ids:
            valid_students = Student.objects.filter(id__in=member_ids).exclude(id=me.id)
            existing_member_ids = set(
                GroupChatMembership.objects.filter(group=group).values_list('member_id', flat=True)
            )
            to_create = [
                GroupChatMembership(group=group, member=student, role='member')
                for student in valid_students
                if student.id not in existing_member_ids
            ]
            if to_create:
                GroupChatMembership.objects.bulk_create(to_create)

        return group

    try:
        group = await _create_group_and_members()
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to create group chat: {e}")
        return JsonResponse({'status': 'error', 'message': 'Failed to create group chat.'}, status=500)

    @sync_to_async
    def _serialize_group(g):
        serializer = GroupChatSerializer(g, context={'request': request, 'kinde_user_id': kinde_user_id})
        return serializer.data

    group_data = await _serialize_group(group)
    return JsonResponse({'status': 'success', 'group': group_data}, status=201)


@csrf_exempt
@kinde_auth_required
async def get_group_chat(request, group_id=None, kinde_user_id=None):
    """
    Get group chat info: name, members (with roles), and all media set in the group.
    Caller must be a member.
    """
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)
    if not group_id:
        return JsonResponse({'status': 'error', 'message': 'group_id is required in the URL.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        group = await GroupChat.objects.select_related('created_by').aget(id=group_id, is_active=True)
    except GroupChat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Group chat not found.'}, status=404)

    try:
        await GroupChatMembership.objects.aget(group=group, member=me)
    except GroupChatMembership.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'You are not a member of this group.'}, status=403)

    @sync_to_async
    def _build_group_info(g):
        # Members: membership + member (Student) + role
        memberships = GroupChatMembership.objects.filter(group=g).select_related('member')
        members = []
        for m in memberships:
            member_data = StudentChatSerializer(m.member, context={'request': request}).data
            members.append({'member': member_data, 'role': m.role, 'joined_at': m.joined_at.isoformat() if m.joined_at else None})

        # Media: group image + all message images in this group
        media = []
        if g.image:
            url = request.build_absolute_uri(g.image.url) if request else g.image.url
            media.append({'type': 'group_image', 'url': url, 'message_id': None, 'sent_at': None})

        message_images = (
            GroupChatMessage.objects.filter(group=g)
            .exclude(image='')
            .exclude(image__isnull=True)
            .order_by('sent_at', 'id')
        )
        for msg in message_images:
            url = request.build_absolute_uri(msg.image.url) if request and msg.image else (msg.image.url if msg.image else None)
            if url:
                media.append({
                    'type': 'message_image',
                    'url': url,
                    'message_id': msg.id,
                    'sent_at': msg.sent_at.isoformat() if msg.sent_at else None,
                })

        return {
            'name': g.name,
            'description': g.description,
            'id': g.id,
            'image_url': request.build_absolute_uri(g.image.url) if request and g.image else (g.image.url if g.image else None),
            'created_at': g.created_at.isoformat() if g.created_at else None,
            'created_by': StudentChatSerializer(g.created_by, context={'request': request}).data,
            'members': members,
            'media': media,
        }

    payload = await _build_group_info(group)
    return JsonResponse({'status': 'success', 'group': payload}, status=200)




@csrf_exempt
@kinde_auth_required
async def update_group_chat(request, group_id=None, kinde_user_id=None):
    """
    Update basic group chat details (name, description, image).
    Only owner/admins can update.
    """
    if request.method not in ['POST', 'PATCH']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)

    if not group_id:
        return JsonResponse({'status': 'error', 'message': 'group_id is required in URL.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        group = await GroupChat.objects.aget(id=group_id, is_active=True)
    except GroupChat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Group chat not found.'}, status=404)

    try:
        membership = await GroupChatMembership.objects.aget(group=group, member=me)
    except GroupChatMembership.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'You are not a member of this group.'}, status=403)

    if membership.role not in ['owner', 'admin']:
        return JsonResponse({'status': 'error', 'message': 'Only group owner/admins can update group details.'}, status=403)

    # Parse body
    data = {}
    if request.body and request.content_type and 'application/json' in request.content_type:
        import json as _json
        try:
            data = _json.loads(request.body.decode('utf-8'))
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON body.'}, status=400)
    else:
        data = request.POST

    new_name = data.get('name')
    new_description = data.get('description')
    image_file = request.FILES.get('image')

    @sync_to_async
    def _update_group():
        changed_fields = []
        if new_name:
            group.name = new_name
            changed_fields.append('name')
        if new_description is not None:
            group.description = new_description
            changed_fields.append('description')
        if image_file is not None:
            group.image = image_file
            changed_fields.append('image')
        if changed_fields:
            group.save(update_fields=changed_fields)
        return group

    group = await _update_group()

    @sync_to_async
    def _serialize_group(g):
        serializer = GroupChatSerializer(g, context={'request': request, 'kinde_user_id': kinde_user_id})
        return serializer.data

    group_data = await _serialize_group(group)
    return JsonResponse({'status': 'success', 'group': group_data}, status=200)


@csrf_exempt
@kinde_auth_required
async def add_group_members(request, group_id=None, kinde_user_id=None):
    """
    Add members to an existing group chat. Only owner/admins can add.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)
    if not group_id:
        return JsonResponse({'status': 'error', 'message': 'group_id is required in URL.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        group = await GroupChat.objects.aget(id=group_id, is_active=True)
    except GroupChat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Group chat not found.'}, status=404)

    try:
        membership = await GroupChatMembership.objects.aget(group=group, member=me)
    except GroupChatMembership.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'You are not a member of this group.'}, status=403)

    if membership.role not in ['owner', 'admin']:
        return JsonResponse({'status': 'error', 'message': 'Only group owner/admins can add members.'}, status=403)

    # Parse body
    import json as _json
    data = {}
    if request.body:
        try:
            data = _json.loads(request.body.decode('utf-8'))
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON body.'}, status=400)

    raw_member_ids = data.get('member_ids') or []
    if isinstance(raw_member_ids, str):
        member_ids = [mid for mid in raw_member_ids.split(',') if mid.strip()]
    else:
        member_ids = raw_member_ids

    if not member_ids:
        return JsonResponse({'status': 'error', 'message': 'member_ids is required.'}, status=400)

    @sync_to_async
    def _add_members():
        valid_students = Student.objects.filter(id__in=member_ids).exclude(id=me.id)
        existing_member_ids = set(
            GroupChatMembership.objects.filter(group=group).values_list('member_id', flat=True)
        )
        to_create = [
            GroupChatMembership(group=group, member=student, role='member')
            for student in valid_students
            if student.id not in existing_member_ids
        ]
        if to_create:
            GroupChatMembership.objects.bulk_create(to_create)
        return len(to_create)

    created_count = await _add_members()
    return JsonResponse({'status': 'success', 'added_count': created_count}, status=200)


@csrf_exempt
@kinde_auth_required
async def remove_group_members(request, group_id=None, kinde_user_id=None):
    """
    Remove members from a group chat. Only owner/admins can remove.
    Owner cannot be removed by this endpoint.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)
    if not group_id:
        return JsonResponse({'status': 'error', 'message': 'group_id is required in URL.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        group = await GroupChat.objects.aget(id=group_id, is_active=True)
    except GroupChat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Group chat not found.'}, status=404)

    try:
        membership = await GroupChatMembership.objects.aget(group=group, member=me)
    except GroupChatMembership.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'You are not a member of this group.'}, status=403)

    if membership.role not in ['owner', 'admin']:
        return JsonResponse({'status': 'error', 'message': 'Only group owner/admins can remove members.'}, status=403)

    import json as _json
    data = {}
    if request.body:
        try:
            data = _json.loads(request.body.decode('utf-8'))
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON body.'}, status=400)

    raw_member_ids = data.get('member_ids') or []
    if isinstance(raw_member_ids, str):
        member_ids = [mid for mid in raw_member_ids.split(',') if mid.strip()]
    else:
        member_ids = raw_member_ids

    if not member_ids:
        return JsonResponse({'status': 'error', 'message': 'member_ids is required.'}, status=400)

    @sync_to_async
    def _remove_members():
        # Do not allow removing the owner
        owner_ids = list(
            GroupChatMembership.objects.filter(group=group, role='owner').values_list('member_id', flat=True)
        )
        qs = GroupChatMembership.objects.filter(group=group, member_id__in=member_ids).exclude(member_id__in=owner_ids)
        removed_count = qs.count()
        qs.delete()
        return removed_count

    removed_count = await _remove_members()
    return JsonResponse({'status': 'success', 'removed_count': removed_count}, status=200)


@csrf_exempt
@kinde_auth_required
async def leave_group_chat(request, group_id=None, kinde_user_id=None):
    """
    Current user leaves the group chat.
    If the owner is the last member, the group is deleted.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)
    if not group_id:
        return JsonResponse({'status': 'error', 'message': 'group_id is required in URL.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        group = await GroupChat.objects.aget(id=group_id, is_active=True)
    except GroupChat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Group chat not found.'}, status=404)

    try:
        membership = await GroupChatMembership.objects.aget(group=group, member=me)
    except GroupChatMembership.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'You are not a member of this group.'}, status=403)

    @sync_to_async
    def _leave():
        """
        Remove the current user from the group.
        If they were the last member, delete the group entirely.
        After this, list_group_chats and get_all_unified_chats will no longer return this group.
        """
        # Count BEFORE deleting this membership
        total_members = GroupChatMembership.objects.filter(group=group).count()
        membership.delete()
        if total_members <= 1:
            # After removing this user, there are no members left
            group.delete()
            return 'deleted'
        return 'left'

    result = await _leave()
    if result == 'deleted':
        return JsonResponse({'status': 'success', 'message': 'You left and the group was deleted (no members left).'}, status=200)
    return JsonResponse({'status': 'success', 'message': 'Left group successfully.'}, status=200)


@csrf_exempt
@kinde_auth_required
async def delete_group_chat(request, group_id=None, kinde_user_id=None):
    """
    Remove the current user from the group and, if they are the last member,
    delete the group. This behaves like \"delete from my groups\".
    """
    if request.method not in ['POST', 'DELETE']:
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)
    if not group_id:
        return JsonResponse({'status': 'error', 'message': 'group_id is required in URL.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        group = await GroupChat.objects.aget(id=group_id, is_active=True)
    except GroupChat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Group chat not found.'}, status=404)

    try:
        membership = await GroupChatMembership.objects.aget(group=group, member=me)
    except GroupChatMembership.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'You are not a member of this group.'}, status=403)

    @sync_to_async
    def _leave_and_maybe_delete():
        """
        Same semantics as leave_group_chat:
        - Always remove this user from the group.
        - If there are no members left afterward, delete the group.
        """
        total_members = GroupChatMembership.objects.filter(group=group).count()
        membership.delete()
        if total_members <= 1:
            group.delete()
            return 'deleted'
        return 'left'

    result = await _leave_and_maybe_delete()
    if result == 'deleted':
        return JsonResponse({'status': 'success', 'message': 'You left and the group was deleted (no members left).'}, status=200)
    return JsonResponse({'status': 'success', 'message': 'Group removed from your groups (you are no longer a member).'}, status=200)


@csrf_exempt
@kinde_auth_required
async def list_group_chats(request, kinde_user_id=None):
    """
    List all active group chats for the authenticated user,
    including last_message and unread_count for each group.
    """
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    @sync_to_async
    def _fetch_groups_and_meta():
        # All active groups the user is a member of
        memberships = GroupChatMembership.objects.select_related('group').filter(
            member=me,
            group__is_active=True,
        )
        groups = [m.group for m in memberships]

        if not groups:
            return []

        group_ids = [g.id for g in groups]

        # Latest message per group
        all_messages = (
            GroupChatMessage.objects.filter(group_id__in=group_ids)
            .order_by('-sent_at', '-id')
            .select_related('student', 'group')
        )
        latest_by_group = {}
        for msg in all_messages:
            if msg.group_id not in latest_by_group:
                latest_by_group[msg.group_id] = msg

        # Unread count per group for this user
        unread_counts = {}
        for gid in group_ids:
            unread_counts[gid] = (
                GroupChatMessage.objects.filter(group_id=gid)
                .exclude(student=me)
                .exclude(read_by=me)
                .count()
            )

        # Attach metadata to group instances
        for g in groups:
            setattr(g, 'last_message', latest_by_group.get(g.id))
            setattr(g, 'unread_count', unread_counts.get(g.id, 0))

        # Sort groups by last_message timestamp (fallback to created_at)
        def _sort_key(group_obj):
            lm = getattr(group_obj, 'last_message', None)
            if lm:
                return lm.sent_at
            return group_obj.created_at

        groups.sort(key=_sort_key, reverse=True)
        return groups

    groups = await _fetch_groups_and_meta()

    @sync_to_async
    def _serialize_groups(objs):
        serializer = GroupChatSerializer(objs, many=True, context={'request': request, 'kinde_user_id': kinde_user_id})
        return serializer.data

    groups_data = await _serialize_groups(groups)
    return JsonResponse({'status': 'success', 'groups': groups_data}, status=200)


@csrf_exempt
@kinde_auth_required
async def group_chat_messages(request, group_id=None, kinde_user_id=None):
    """
    Retrieve message history for a group chat with cursor-based pagination.
    """
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed.'}, status=405)

    if not kinde_user_id:
        return JsonResponse({'status': 'error', 'message': 'Authentication required.'}, status=401)
    if not group_id:
        return JsonResponse({'status': 'error', 'message': 'group_id is required in the URL.'}, status=400)

    try:
        me = await Student.objects.aget(kinde_user_id=kinde_user_id)
    except Student.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Authenticated user not found.'}, status=404)

    try:
        group = await GroupChat.objects.aget(pk=group_id, is_active=True)
    except GroupChat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Group chat not found.'}, status=404)

    # Access control: only current members can access group history.
    try:
        is_member = await GroupChatMembership.objects.filter(group=group, member=me).aexists()
        if not is_member:
            return JsonResponse({'status': 'error', 'message': 'You are not a member of this group.'}, status=403)
    except Exception:
        return JsonResponse({'status': 'error', 'message': 'Failed to verify membership.'}, status=500)

    # Pagination params
    limit, offset = _parse_pagination_params(request)
    before_id = request.GET.get('before')
    after_id = request.GET.get('after')
    before_timestamp = request.GET.get('before_timestamp')
    after_timestamp = request.GET.get('after_timestamp')

    # Base queryset
    messages_queryset = GroupChatMessage.objects.filter(
        group=group
    ).select_related(
        'student',
        'group',
        'student_profile',
        'community_profile',
        'reply',
        'reply__student',
        'post',
        'community_post',
        'student_event',
        'community_event',
    ).prefetch_related('read_by').order_by('-sent_at', '-id')

    # Apply cursor filters
    try:
        if before_id:
            try:
                before_message = await GroupChatMessage.objects.aget(id=before_id)
                messages_queryset = messages_queryset.filter(
                    Q(sent_at__lt=before_message.sent_at) |
                    Q(sent_at=before_message.sent_at, id__lt=before_message.id)
                )
            except GroupChatMessage.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'Before message not found.'}, status=400)

        elif after_id:
            try:
                after_message = await GroupChatMessage.objects.aget(id=after_id)
                messages_queryset = messages_queryset.filter(
                    Q(sent_at__gt=after_message.sent_at) |
                    Q(sent_at=after_message.sent_at, id__gt=after_message.id)
                ).order_by('sent_at', 'id')
            except GroupChatMessage.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'After message not found.'}, status=400)

        elif before_timestamp:
            before_dt = datetime.fromisoformat(before_timestamp.replace('Z', '+00:00'))
            if timezone.is_naive(before_dt):
                before_dt = timezone.make_aware(before_dt)
            messages_queryset = messages_queryset.filter(sent_at__lt=before_dt)

        elif after_timestamp:
            after_dt = datetime.fromisoformat(after_timestamp.replace('Z', '+00:00'))
            if timezone.is_naive(after_dt):
                after_dt = timezone.make_aware(after_dt)
            messages_queryset = messages_queryset.filter(sent_at__gt=after_dt).order_by('sent_at', 'id')

    except ValueError as e:
        return JsonResponse({'status': 'error', 'message': f'Invalid timestamp format: {str(e)}'}, status=400)

    # Fetch messages
    try:
        messages_list = await sync_to_async(list)(messages_queryset[offset:offset + limit + 1])
    except Exception:
        return JsonResponse({'status': 'error', 'message': 'Failed to fetch messages.'}, status=500)

    has_more = len(messages_list) > limit
    if has_more:
        messages_list = messages_list[:limit]

    if not after_id and not after_timestamp:
        messages_list.reverse()

    pagination_info = {
        'has_more': has_more,
        'limit': limit,
        'count': len(messages_list),
    }

    if messages_list:
        first_message = messages_list[0]
        last_message = messages_list[-1]
        pagination_info.update({
            'first_message_id': first_message.id,
            'last_message_id': last_message.id,
            'first_timestamp': first_message.sent_at.isoformat(),
            'last_timestamp': last_message.sent_at.isoformat(),
        })
        base_url = request.build_absolute_uri().split('?')[0]
        if has_more and not (after_id or after_timestamp):
            pagination_info['next_url'] = f"{base_url}?before={first_message.id}&limit={limit}"
        pagination_info['newer_url'] = f"{base_url}?after={last_message.id}&limit={limit}"

    serializer_context = {'request': request, 'kinde_user_id': kinde_user_id}
    serializer = GroupChatMessageSerializer(messages_list, many=True, context=serializer_context)
    serializer_data = await sync_to_async(lambda: serializer.data)()

    return JsonResponse({
        'status': 'success',
        'messages': serializer_data,
        'pagination': pagination_info,
    }, status=200)
