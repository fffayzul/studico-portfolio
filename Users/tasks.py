from celery import shared_task  # type: ignore
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import escape
from smtplib import SMTPRecipientsRefused
import logging

logger = logging.getLogger(__name__)


def _render_email_html(template_name, context):
    """Render an email template to HTML (Studico-styled)."""
    return render_to_string(f"emails/{template_name}", context)


@shared_task
def delete_storage_files_task(file_paths):
    """
    Delete files from default storage (GCS). Intended to run on the Celery worker (helper server),
    not on the main web server.
    """
    if not file_paths:
        return
    from django.core.files.storage import default_storage
    for path in file_paths:
        if path:
            try:
                default_storage.delete(path)
            except Exception as e:
                logger.warning("Failed to delete file %s from storage: %s", path, e)


def _broadcast_notification(notification, recipient_kinde_id):
    """
    Serialize and push a notification update over the websocket layer.
    Re-fetches the notification with select_related/prefetch_related to avoid N+1
    when NotificationSerializer accesses sender, notificationtype, and content relations.
    """
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from .serializers import NotificationSerializer
        from .models import Notification

        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.warning("Channel layer unavailable when broadcasting notification.")
            return

        # Re-fetch with all relations needed by NotificationSerializer (avoids N+1)
        notification = (
            Notification.objects.filter(pk=notification.pk)
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
        ).first()
        if not notification:
            return

        serialized = NotificationSerializer(notification).data
        async_to_sync(channel_layer.group_send)(
            f'notifications_{recipient_kinde_id}',
            {
                'type': 'send_notification',
                'notification_data': serialized
            }
        )
    except Exception as exc:
        logger.error(
            "Failed to broadcast websocket notification %s: %s",
            getattr(notification, "id", None),
            exc,
        )

import logging
from django.core.mail import send_mail
from django.conf import settings
from celery import shared_task # Assuming Celery for @shared_task

logger = logging.getLogger(__name__)

@shared_task
def send_welcome_email(email, student_name="there"):
    """
    Send a welcoming email to a new Studico user with clear next steps.
    """
    subject = f'Welcome to Studico, {student_name}!'

    # --- Email Body Option 1: Concise & Action-Oriented ---
    message_body = f"""
Hi {student_name},

Welcome to Studico! Your account has been successfully verified, and we're thrilled to have you join our growing community of learners.

Studico is designed to help you connect, collaborate, and excel in your academic journey.

**Here’s what you can do next:**

1.  **Complete Your Profile:** By doing this, it'll make it people on Studico know a bit more about you! 
2.  **Explore Communities:** Discover study groups, clubs, and discussions relevant to your interests. 
3.  **Create Your First Post/Event/Community:** Share an idea, Host an event, or start a community! 

We're excited to have you on board and hope you have a great time using Studico.

Have fun!,

The Studico Team

"""

    try:
        html_message = _render_email_html("welcome.html", {"student_name": student_name})
        send_mail(
            subject,
            message_body.strip(),
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
            html_message=html_message,
        )
        logger.info(f"Concise welcome email sent successfully to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send concise welcome email to {email}: {str(e)}")
        raise # Re-raise to let Celery retry if configured


@shared_task
def send_email_to_verified_users_task(subject, message):
    """
    Send one email (subject + plain text message) to all verified users.
    Runs on Celery worker to avoid blocking the admin request.
    """
    from .models import Student

    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None) or getattr(settings, 'EMAIL_HOST_USER', '')
    recipient_emails = []
    for s in Student.objects.filter(is_verified=True).only('email', 'student_email'):
        email = (s.student_email or s.email or '').strip()
        if email and email not in recipient_emails:
            recipient_emails.append(email)

    if not recipient_emails:
        logger.warning("send_email_to_verified_users_task: no verified users with valid emails")
        return 0

    message_html = escape(message).replace("\n", "<br>")
    html_message = _render_email_html("broadcast.html", {"message_html": message_html})
    sent = 0
    for email in recipient_emails:
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=[email],
                fail_silently=False,
                html_message=html_message,
            )
            sent += 1
        except Exception as e:
            logger.error("Failed to send admin email to %s: %s", email, e)
            # Continue to other recipients; don't re-raise so task completes

    logger.info("send_email_to_verified_users_task: sent to %s of %s recipients", sent, len(recipient_emails))
    return sent


@shared_task
def send_verification_email(email, otp, student_name="Valued User"):
    """
    Send email verification code to reduce spam flags, using a livelier, more informative tone.
    """
    # Option 1: More urgent & direct
    # subject = f"Action Required: Verify Your Studico Account! {student_name}"
    
    # Option 2: Friendly & clear
    subject = f"Your Studico Verification Code is Here, {student_name}!"

    # --- Email Body ---
    message_body = f"""
Hi {student_name},

Thanks for starting your journey with Studico! We just need to quickly verify that you are a student.

Your one-time verification code is:

                           {otp}

Please enter this code on the Studico verification page to complete your registration.



**Important:** This code is valid for 10 minutes. For your security, please do not share this code with anyone.

If you didn't request this code, you can safely ignore this email.

We're excited to have you join the Studico community!

Best regards,

The Studico Team

"""
    try:
        html_message = _render_email_html(
            "verification.html",
            {"student_name": student_name, "otp": otp},
        )
        send_mail(
            subject,
            message_body.strip(),
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
            html_message=html_message,
        )
        logger.info(f"Verification email sent successfully to {email}")
        return True
    except SMTPRecipientsRefused as e:
        # Invalid / non-existent mailbox etc. — treat as expected user error, not a server error.
        logger.warning(
            "Verification email not sent; invalid recipient %s: %s",
            email,
            e,
        )
        # Do NOT re-raise so Sentry/Celery doesn't treat this as a failure worth retrying.
        return False
    except Exception as e:
        logger.error(f"Failed to send verification email to {email}: {str(e)}")
        raise


@shared_task
def send_account_deletion_notification(email, student_name, scheduled_deletion_date):
    """
    Send email to user notifying them that their account deletion request has been received
    and their account will be deleted in 30 days.
    """
    subject = f'Account Deletion Request Received - Studico'
    
    deletion_date_str = scheduled_deletion_date.strftime('%B %d, %Y')
    
    message_body = f"""
Hi {student_name},

We've received your request to permanently delete your Studico account.

**Your account will be permanently deleted on {deletion_date_str} (30 days from now).**

During this 30-day period:
- Your account and all your information will be scheduled for deletion
- Your content will not be accessible to other users on Studico
- Your Kinde authentication account will be deleted immediately, and you will not be able to log in

**Important:** Once you submit a deletion request, it cannot be cancelled. Your account deletion is permanent and irreversible.

**What will be deleted:**
- Your profile, posts, photos, and videos
- All your comments, likes, and bookmarks
- Your friends list and friend requests
- Your community memberships
- All your messages and notifications
- All other data associated with your account

If you have any questions or need assistance, please contact us at support@teamstudico.com.

Best regards,
The Studico Team
"""
    try:
        html_message = _render_email_html(
            "deletion_notification.html",
            {"student_name": student_name, "deletion_date_str": deletion_date_str},
        )
        send_mail(
            subject,
            message_body.strip(),
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
            html_message=html_message,
        )
        logger.info(f"Account deletion notification email sent successfully to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send account deletion notification email to {email}: {str(e)}")
        raise


@shared_task
def send_admin_deletion_notification(student_email, student_name, student_username, scheduled_deletion_date):
    """
    Send email to admin (fayzul@teamstudico.com) notifying that a user has requested account deletion.
    """
    subject = f'Account Deletion Request - {student_name} ({student_email})'
    
    deletion_date_str = scheduled_deletion_date.strftime('%B %d, %Y at %I:%M %p UTC')
    
    message_body = f"""
A user has requested to delete their Studico account.

**User Information:**
- Name: {student_name}
- Email: {student_email}
- Username: {student_username if student_username else 'N/A'}
- Scheduled Deletion Date: {deletion_date_str}

**Next Steps:**
The account will be automatically deleted after 30 days (on {deletion_date_str}). The Kinde authentication account has been deleted immediately, and the user can no longer log in. This deletion request cannot be cancelled.

Please monitor the deletion process to ensure the account is fully deleted.

Best regards,
Studico System
"""
    deletion_date_str = scheduled_deletion_date.strftime("%B %d, %Y at %I:%M %p UTC")
    try:
        html_message = _render_email_html(
            "admin_deletion_notification.html",
            {
                "student_name": student_name,
                "student_email": student_email,
                "student_username": student_username or "N/A",
                "deletion_date_str": deletion_date_str,
            },
        )
        send_mail(
            subject,
            message_body.strip(),
            settings.DEFAULT_FROM_EMAIL,
            ['fayzul@teamstudico.com'],
            fail_silently=False,
            html_message=html_message,
        )
        logger.info(f"Admin deletion notification email sent successfully for user {student_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send admin deletion notification email: {str(e)}")
        raise


@shared_task
def send_deletion_confirmation_email(email, student_name):
    """
    Send email to user confirming that their account has been permanently deleted.
    Note: This is sent after deletion, so we need to pass the email as a parameter
    since the student object will no longer exist.
    """
    subject = f'Your Studico Account Has Been Deleted'
    
    message_body = f"""
Hi {student_name},

This email confirms that your Studico account and all associated data have been permanently deleted as requested.

**What was deleted:**
- Your profile and all personal information
- All your posts, photos, and videos
- All your comments, likes, and bookmarks
- Your friends list and connections
- Your community memberships
- All your messages and notifications
- All other data associated with your account

Your account deletion is now complete. If you'd like to use Studico again in the future, you can create a new account.

Thank you for being part of the Studico community.

Best regards,
The Studico Team
"""
    try:
        html_message = _render_email_html(
            "deletion_confirmation.html",
            {"student_name": student_name},
        )
        send_mail(
            subject,
            message_body.strip(),
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
            html_message=html_message,
        )
        logger.info(f"Account deletion confirmation email sent successfully to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send account deletion confirmation email to {email}: {str(e)}")
        # Don't raise here since the account is already deleted
        logger.warning(f"Email notification failed but account deletion was successful")


@shared_task
def send_admin_deletion_confirmation(student_email, student_name, student_username):
    """
    Send email to admin (fayzul@teamstudico.com) confirming that a user's account has been deleted.
    """
    subject = f'Account Deletion Completed - {student_name} ({student_email})'
    
    message_body = f"""
A user's account has been successfully deleted from Studico.

**Deleted User Information:**
- Name: {student_name}
- Email: {student_email}
- Username: {student_username if student_username else 'N/A'}

**Deletion Status:**
The account and all associated data have been permanently deleted from the system.

**What was deleted:**
- User profile and personal information
- All posts, photos, and videos
- All comments, likes, and bookmarks
- Friends list and connections
- Community memberships
- All messages and notifications
- All other associated data

The deletion process is complete.

Best regards,
Studico System
"""
    try:
        html_message = _render_email_html(
            "admin_deletion_confirmation.html",
            {
                "student_name": student_name,
                "student_email": student_email,
                "student_username": student_username or "N/A",
            },
        )
        send_mail(
            subject,
            message_body.strip(),
            settings.DEFAULT_FROM_EMAIL,
            ['fayzul@teamstudico.com'],
            fail_silently=False,
            html_message=html_message,
        )
        logger.info(f"Admin deletion confirmation email sent successfully for user {student_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send admin deletion confirmation email: {str(e)}")
        # Don't raise here since the account is already deleted
        logger.warning(f"Admin email notification failed but account deletion was successful")


@shared_task
def send_banned_notification_email(email, student_name, reason=None, banned_until_iso=None):
    """
    Send email to a banned user notifying them of the ban. Not appealable.
    If banned_until_iso is None or the date is more than 1 year away, the ban is described as permanent.
    """
    if not (email and (email := email.strip())):
        logger.warning("send_banned_notification_email skipped: no email address")
        return False
    from datetime import datetime, timezone
    subject = "Your Studico Account Has Been Banned"
    is_permanent = True
    expiry_date_str = None
    if banned_until_iso:
        try:
            expiry = datetime.fromisoformat(banned_until_iso.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= now:
                is_permanent = True
            else:
                # Ban expires within a reasonable time: show as temporary
                one_year_later = now.replace(year=now.year + 1)
                if expiry <= one_year_later:
                    is_permanent = False
                    expiry_date_str = expiry.strftime("%d %B %Y")
                # else: expires in more than a year, still say permanent
        except Exception:
            pass

    context = {
        "student_name": student_name or "there",
        "reason": (reason or "").strip() or "Violation of our community guidelines.",
        "is_permanent": is_permanent,
        "expiry_date_str": expiry_date_str,
    }

    message_body = f"""
Hi {context['student_name']},

Your Studico account has been banned.
"""
    if is_permanent:
        message_body += """
This ban is permanent. You will not be able to access your account or use Studico services.
"""
    else:
        message_body += f"""
Your ban will expire on {expiry_date_str}. Until then, you will not be able to access your account or use Studico services.
"""
    message_body += f"""
Reason: {context['reason']}

This decision is final and cannot be appealed.

Best regards,
The Studico Team
"""
    try:
        html_message = _render_email_html("banned.html", context)
        send_mail(
            subject,
            message_body.strip(),
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
            html_message=html_message,
        )
        logger.info("Banned notification email sent to %s", email)
        return True
    except Exception as e:
        logger.error("Failed to send banned notification email to %s: %s", email, e)
        return False


@shared_task
def send_direct_message_notification_task(message_id):
    """
    Background task to fan out direct message notifications (push + DB + websocket).
    """
    # Check Firebase initialization in Celery process
    from firebase_admin import get_app
    from django.conf import settings
    import os
    try:
        app = get_app()
        logger.info(f"Firebase app initialised with project: {app.project_id}")
    except ValueError:
        logger.error("Firebase app not initialised in Celery process.")
        # Diagnose why Firebase isn't initialized
        fsa_available = bool(os.getenv("FSA"))
        firebase_creds_available = bool(getattr(settings, "FIREBASE_CREDENTIALS_INFO", None))
        logger.error(f"Diagnostics - FSA env var available: {fsa_available}, FIREBASE_CREDENTIALS_INFO available: {firebase_creds_available}")
        if not fsa_available:
            logger.error("CRITICAL: FSA environment variable is NOT set in Celery worker. Set it in Railway for the Celery service.")
        elif not firebase_creds_available:
            logger.error("CRITICAL: FIREBASE_CREDENTIALS_INFO is None in settings. FSA may be invalid JSON or not loaded properly.")



    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    from .models import DirectMessage, Notification, MutedStudents
    from .firebase_utils import send_push_notifications_to_user

    try:
        message = DirectMessage.objects.select_related('sender', 'receiver').get(pk=message_id)
    except DirectMessage.DoesNotExist:
        logger.warning(f"DirectMessage {message_id} not found; skipping notification.")
        return

    sender_user = message.sender
    receiver_user = message.receiver

    if sender_user == receiver_user:
        return

    is_muted = MutedStudents.objects.filter(
        student=receiver_user,
        muted_student=sender_user
    ).exists()

    if is_muted:
        logger.info(f"Receiver {receiver_user.id} muted sender {sender_user.id}; skipping notification.")
        return

    message_preview = message.message[:100] + "..." if len(message.message) > 100 else message.message

    Notification.objects.create(
        recipient=receiver_user,
        content=f"{sender_user.name} sent you a message: '{message_preview}'",
        post=None,
        community_post=None
    )

    title = f"New message from {sender_user.name}"
    body = message_preview
    notification_data = {
        "type": "direct_message",
        "message_id": str(message.id),
        "sender_id": str(sender_user.id),
        "sender_name": str(sender_user.name),
        "sender_kinde_id": str(sender_user.kinde_user_id),
        "receiver_id": str(receiver_user.id),
        "receiver_kinde_id": str(receiver_user.kinde_user_id),
        "message_preview": str(message_preview),
        "timestamp": message.timestamp.isoformat(),
        "has_image": str(bool(message.image)).lower()
    }

    send_push_notifications_to_user(receiver_user, title, body, notification_data)

    channel_layer = get_channel_layer()
    if channel_layer:
        update_data = {
            'conversation_type': 'direct_chat',
            'conversation_target_id': sender_user.kinde_user_id,
            'last_message_text': message_preview,
            'last_message_sender_name': sender_user.name,
            'last_message_sender_username': sender_user.username,
            'last_message_sender_profile_picture': sender_user.profile_image.url if sender_user.profile_image else None,
            'last_message_sender_kinde_id': sender_user.kinde_user_id,
            'last_message_timestamp': message.timestamp.isoformat(),
            'message_id': message.id,
            'message_has_image': bool(message.image),
            'message_image_url': message.image.url if message.image else None
        }

        receiver_group = f'unified_chats_{receiver_user.kinde_user_id}'
        sender_group = f'unified_chats_{sender_user.kinde_user_id}'

        async_to_sync(channel_layer.group_send)(
            receiver_group,
            {
                'type': 'unified_chats_update',
                'update_data': update_data
            }
        )
        async_to_sync(channel_layer.group_send)(
            sender_group,
            {
                'type': 'unified_chats_update',
                'update_data': update_data
            }
        )


@shared_task
def send_community_message_notification_task(message_id):
    """
    Background task to dispatch community chat notifications.
    """
    # Check Firebase initialization in Celery process
    from firebase_admin import get_app
    from django.conf import settings
    import os
    try:
        app = get_app()
        logger.info(f"Firebase app initialised with project: {app.project_id}")
    except ValueError:
        logger.error("Firebase app not initialised in Celery process.")
        # Diagnose why Firebase isn't initialized
        fsa_available = bool(os.getenv("FSA"))
        firebase_creds_available = bool(getattr(settings, "FIREBASE_CREDENTIALS_INFO", None))
        logger.error(f"Diagnostics - FSA env var available: {fsa_available}, FIREBASE_CREDENTIALS_INFO available: {firebase_creds_available}")
        if not fsa_available:
            logger.error("CRITICAL: FSA environment variable is NOT set in Celery worker. Set it in Railway for the Celery service.")
        elif not firebase_creds_available:
            logger.error("CRITICAL: FIREBASE_CREDENTIALS_INFO is None in settings. FSA may be invalid JSON or not loaded properly.")

    from django.utils import timezone
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    from .models import CommunityChatMessage, Membership, MutedCommunities, MutedStudents, Notification
    from .firebase_utils import send_push_notifications_to_user

    try:
        message = CommunityChatMessage.objects.select_related(
            'student',
            'community',
        ).get(pk=message_id)
    except CommunityChatMessage.DoesNotExist:
        logger.warning(f"CommunityChatMessage {message_id} not found; skipping notification.")
        return

    sender_user = message.student
    community = message.community

    memberships = Membership.objects.filter(
        community=community,
        role__in=['admin', 'secondary_admin']
    ).exclude(user=sender_user).select_related('user')

    member_users = [membership.user for membership in memberships]
    if not member_users:
        return

    muted_community_users = set(
        MutedCommunities.objects.filter(
            community=community
        ).values_list('student__id', flat=True)
    )

    muted_sender_users = set(
        MutedStudents.objects.filter(muted_student=sender_user).values_list('student__id', flat=True)
    )

    message_preview = message.message[:100] + "..." if len(message.message) > 100 else message.message
    title = f"New message in {community.community_name}"
    body = f"{sender_user.name}: {message_preview}"

    notification_data = {
        "type": "community_chat_message",
        "message_id": str(message.id),
        "community_id": str(community.id),
        "community_name": str(community.community_name),
        "sender_id": str(sender_user.id),
        "sender_name": str(sender_user.name),
        "sender_username": str(getattr(sender_user, 'username', '')),
        "sender_profile_picture": str(sender_user.profile_image.url if sender_user.profile_image else ""),
        "message_preview": str(message_preview),
        "timestamp": timezone.now().isoformat(),
    }

    for user in member_users:
        if user.id in muted_community_users or user.id in muted_sender_users:
            continue

        Notification.objects.create(
            recipient=user,
            content=f"{sender_user.name} posted in {community.community_name}: '{message_preview}'",
            sender=sender_user
        )

        send_push_notifications_to_user(
            user=user,
            title=title,
            body=body,
            data=notification_data
        )

    channel_layer = get_channel_layer()
    if channel_layer:
        update_data = {
            'conversation_type': 'community_chat',
            'conversation_target_id': str(community.id),
            'conversation_name': community.community_name,
            'conversation_profile_picture': community.community_image.url if community.community_image else None,
            'conversation_tag': community.community_tag,
            'conversation_bio': community.community_bio,
            'last_message_text': message_preview,
            'last_message_sender_name': sender_user.name,
            'last_message_sender_username': sender_user.username,
            'last_message_sender_profile_picture': sender_user.profile_image.url if sender_user.profile_image else None,
            'last_message_sender_kinde_id': sender_user.kinde_user_id,
            'last_message_timestamp': message.sent_at.isoformat(),
            'message_id': message.id,
            'message_has_image': bool(message.image),
            'message_image_url': message.image.url if message.image else None,
            'community_id': community.id
        }

        receiver_group = f'unified_chats_{community.id}'
        sender_group = f'unified_chats_{sender_user.kinde_user_id}'

        async_to_sync(channel_layer.group_send)(
            receiver_group,
            {
                'type': 'unified_chats_update',
                'update_data': update_data
            }
        )
        async_to_sync(channel_layer.group_send)(
            sender_group,
            {
                'type': 'unified_chats_update',
                'update_data': update_data
            }
        )


@shared_task
def send_group_message_notification_task(message_id):
    """
    Background task to dispatch group chat notifications.
    Similar to community chat notifications but for standalone GroupChat.
    """
    # Check Firebase initialization in Celery process
    from firebase_admin import get_app
    from django.conf import settings
    import os

    try:
        app = get_app()
        logger.info(f"Firebase app initialised with project: {app.project_id}")
    except ValueError:
        logger.error("Firebase app not initialised in Celery process.")
        fsa_available = bool(os.getenv("FSA"))
        firebase_creds_available = bool(getattr(settings, "FIREBASE_CREDENTIALS_INFO", None))
        logger.error(f"Diagnostics - FSA env var available: {fsa_available}, FIREBASE_CREDENTIALS_INFO available: {firebase_creds_available}")
        if not fsa_available:
            logger.error("CRITICAL: FSA environment variable is NOT set in Celery worker. Set it in Railway for the Celery service.")
        elif not firebase_creds_available:
            logger.error("CRITICAL: FIREBASE_CREDENTIALS_INFO is None in settings. FSA may be invalid JSON or not loaded properly.")

    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    from .models import GroupChatMessage, GroupChatMembership, MutedStudents, Notification
    from .firebase_utils import send_push_notifications_to_user

    try:
        message = GroupChatMessage.objects.select_related(
            'student',
            'group',
        ).get(pk=message_id)
    except GroupChatMessage.DoesNotExist:
        logger.warning(f"GroupChatMessage {message_id} not found; skipping notification.")
        return

    sender_user = message.student
    group = message.group

    # All members except sender
    memberships = GroupChatMembership.objects.filter(
        group=group,
    ).exclude(member=sender_user).select_related('member')
    member_users = [m.member for m in memberships]
    if not member_users:
        return

    # Users who muted this sender
    muted_sender_users = set(
        MutedStudents.objects.filter(muted_student=sender_user).values_list('student__id', flat=True)
    )

    message_preview = message.message[:100] + "..." if len(message.message) > 100 else message.message
    title = f"New message in {group.name}"
    body = f"{sender_user.name}: {message_preview}"

    notification_data_base = {
        "type": "group_chat_message",
        "message_id": str(message.id),
        "group_id": str(group.id),
        "group_name": str(group.name),
        "sender_id": str(sender_user.id),
        "sender_name": str(sender_user.name),
        "sender_username": str(getattr(sender_user, 'username', '')),
        "sender_profile_picture": str(sender_user.profile_image.url if sender_user.profile_image else ""),
        "message_preview": str(message_preview),
        "has_image": str(bool(message.image)).lower(),
    }

    channel_layer = get_channel_layer()

    for user in member_users:
        if user.id in muted_sender_users:
            continue

        # Create Notification row
        Notification.objects.create(
            recipient=user,
            sender=sender_user,
            content=f"{sender_user.name} in {group.name}: '{message_preview}'",
            post=None,
            community_post=None,
        )

        data = {
            **notification_data_base,
            "receiver_id": str(user.id),
            "receiver_kinde_id": str(user.kinde_user_id),
        }

        send_push_notifications_to_user(user, title, body, data)

        # Send unified chat update if needed (optional, reuse unified_chats group)
        if channel_layer:
            update_data = {
                'conversation_type': 'group_chat',
                'conversation_target_id': str(group.id),
                'display_name': group.name,
                'display_avatar_url': group.image.url if group.image else None,
                'last_message_text': message_preview,
                'last_message_image_url': message.image.url if message.image else None,
                'last_message_timestamp': message.sent_at.isoformat(),
                'last_message_sender_name': sender_user.name,
                'last_message_sender_kinde_id': sender_user.kinde_user_id,
            }
            user_group = f'unified_chats_{user.kinde_user_id}'
            async_to_sync(channel_layer.group_send)(
                user_group,
                {
                    'type': 'unified_chats_update',
                    'update_data': update_data,
                },
            )


@shared_task
def send_student_event_notification_task(student_event_id):
    """
    Fan out notifications when a student posts a new event.
    """
    # Check Firebase initialization in Celery process
    from firebase_admin import get_app
    from django.conf import settings
    import os
    try:
        app = get_app()
        logger.info(f"Firebase app initialised with project: {app.project_id}")
    except ValueError:
        logger.error("Firebase app not initialised in Celery process.")
        # Diagnose why Firebase isn't initialized
        fsa_available = bool(os.getenv("FSA"))
        firebase_creds_available = bool(getattr(settings, "FIREBASE_CREDENTIALS_INFO", None))
        logger.error(f"Diagnostics - FSA env var available: {fsa_available}, FIREBASE_CREDENTIALS_INFO available: {firebase_creds_available}")
        if not fsa_available:
            logger.error("CRITICAL: FSA environment variable is NOT set in Celery worker. Set it in Railway for the Celery service.")
        elif not firebase_creds_available:
            logger.error("CRITICAL: FIREBASE_CREDENTIALS_INFO is None in settings. FSA may be invalid JSON or not loaded properly.")

    from django.utils import timezone
    from django.db.models import Q

    from .models import Student_Events, Student, Notification, MutedStudents
    from .firebase_utils import send_push_notifications_to_user

    try:
        student_event = Student_Events.objects.select_related('student').get(pk=student_event_id)
    except Student_Events.DoesNotExist:
        logger.warning(f"Student_Events {student_event_id} not found; skipping notification.")
        return

    event_creator = student_event.student
    if not event_creator:
        logger.info(f"Student event {student_event_id} has no associated creator; skipping notification.")
        return

    friends = Student.objects.filter(
        Q(sent_requests__receiver=event_creator, sent_requests__status='accepted') |
        Q(received_requests__sender=event_creator, received_requests__status='accepted')
    ).distinct()

    if not friends.exists():
        return

    muted_creator_users = set(
        MutedStudents.objects.filter(muted_student=event_creator).values_list('student__id', flat=True)
    )

    event_date = getattr(student_event, 'date', None)
    if event_date:
        try:
            display_date = event_date.strftime('%B %d')
        except Exception:
            display_date = "Check it out!"
        event_date_iso = event_date.isoformat()
    else:
        display_date = "Check it out!"
        event_date_iso = ""

    title = f"{event_creator.name} posted a new event"
    body = f"'{student_event.event_name}' - {display_date}"

    notification_data = {
        "type": "student_event",
        "student_event_id": str(student_event.id),
        "creator_id": str(event_creator.id),
        "creator_name": event_creator.name,
        "event_name": student_event.event_name,
        "event_date": event_date_iso,
        "timestamp": timezone.now().isoformat(),
    }

    for friend in friends:
        if friend.id in muted_creator_users:
            continue

        notification = Notification.objects.create(
            recipient=friend,
            content=f"New event '{student_event.event_name}' posted by your friend {event_creator.name}",
            student_event=student_event,
            sender=event_creator
        )

        send_push_notifications_to_user(
            user=friend,
            title=title,
            body=body,
            data=notification_data
        )

        _broadcast_notification(notification, friend.kinde_user_id)


@shared_task
def send_post_like_notification_task(post_like_id):
    from .models import PostLike, Notification
    from .firebase_utils import send_push_notifications_to_user

    try:
        like = PostLike.objects.select_related('post', 'student', 'post__student').get(pk=post_like_id)
    except PostLike.DoesNotExist:
        logger.warning("PostLike %s not found; skipping notification.", post_like_id)
        return

    post = like.post
    recipient = post.student
    if recipient == like.student:
        return

    notification = Notification.objects.create(
        recipient=recipient,
        content=f"{like.student.name} liked your post: '{post.context_text}'",
        post=post,
        sender=like.student
    )

    send_push_notifications_to_user(
        user=recipient,
        title=f"{like.student.name} liked your post",
        body="",
        data={"post_id": str(post.id)}
    )

    _broadcast_notification(notification, recipient.kinde_user_id)


@shared_task
def send_post_comment_notification_task(post_comment_id):
    from django.utils import timezone
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    from .models import PostComment, Notification
    from .firebase_utils import send_push_notifications_to_user
    from .serializers import PostCommentSerializer

    try:
        comment = PostComment.objects.select_related('post', 'student', 'post__student').get(pk=post_comment_id)
    except PostComment.DoesNotExist:
        logger.warning("PostComment %s not found; skipping notification.", post_comment_id)
        return

    post = comment.post
    recipient = post.student
    if recipient == comment.student:
        return

    notification = Notification.objects.create(
        recipient=recipient,
        content=f"{comment.student.name} commented on your post: '{post.context_text}'",
        post=post,
        post_comment=comment,
        sender=comment.student
    )

    send_push_notifications_to_user(
        user=recipient,
        title=f"{comment.student.name} commented on your post",
        body=" ",
        data={
            "post_id": str(post.id),
            "comment_id": str(comment.id),
        }
    )

    _broadcast_notification(notification, recipient.kinde_user_id)

    channel_layer = get_channel_layer()
    if channel_layer:
        comment_data = PostCommentSerializer(comment, context={'kinde_user_id': None}).data
        async_to_sync(channel_layer.group_send)(
            f'post_comments_{post.id}',
            {
                'type': 'comment_added',
                'comment_data': comment_data
            }
        )


@shared_task
def send_friend_request_notification_task(friendship_id, event_type):
    from django.utils import timezone

    from .models import Friendship, Notification
    from .firebase_utils import send_push_notifications_to_user

    try:
        friendship = Friendship.objects.select_related('sender', 'receiver').get(pk=friendship_id)
    except Friendship.DoesNotExist:
        logger.warning("Friendship %s not found; skipping notification.", friendship_id)
        return

    sender_user = friendship.sender
    receiver_user = friendship.receiver

    if event_type == 'pending':
        if sender_user == receiver_user:
            return

        Notification.objects.create(
            recipient=receiver_user,
            content=f"You have a new friend request from {sender_user.name}"
        )

        send_push_notifications_to_user(
            user=receiver_user,
            title="New Friend Request",
            body=f"{sender_user.name} wants to be your friend",
            data={
                "type": "friend_request",
                "friendship_id": str(friendship.id),
                "sender_id": str(sender_user.id),
                "sender_name": str(sender_user.name),
                "sender_username": str(getattr(sender_user, 'username', '')),
                "sender_profile_picture": str(sender_user.profile_image.url if sender_user.profile_image else ""),
                "timestamp": timezone.now().isoformat(),
            }
        )
    elif event_type == 'accepted':
        Notification.objects.create(
            recipient=sender_user,
            content=f"{receiver_user.name} accepted your friend request"
        )

        send_push_notifications_to_user(
            user=sender_user,
            title="Friend Request Accepted",
            body=f"{receiver_user.name} accepted your friend request",
            data={
                "type": "friend_request_accepted",
                "student_id": str(receiver_user.id),
                "friend_id": str(receiver_user.id),
                "friend_name": str(receiver_user.name),
                "friend_username": str(getattr(receiver_user, 'username', '')),
                "friend_profile_picture": str(receiver_user.profile_image.url if receiver_user.profile_image else ""),
                "friendship_id": str(friendship.id),
                "timestamp": timezone.now().isoformat(),
            }
        )


@shared_task
def send_community_event_notification_task(community_event_id):
    from django.utils import timezone

    from .models import Community_Events, Membership, Notification, MutedCommunities
    from .firebase_utils import send_push_notifications_to_user

    try:
        community_event = Community_Events.objects.select_related('community', 'poster').get(pk=community_event_id)
    except Community_Events.DoesNotExist:
        logger.warning("Community_Events %s not found; skipping notification.", community_event_id)
        return

    community = community_event.community
    event_creator = community_event.poster

    memberships = Membership.objects.filter(community=community).select_related('user')
    if not memberships.exists():
        return

    muted_community_users = set(
        MutedCommunities.objects.filter(community=community).values_list('student__id', flat=True)
    )

    event_date = getattr(community_event, 'date', None)
    if event_date:
        try:
            display_date = event_date.strftime('%B %d')
        except Exception:
            display_date = "Check it out!"
        event_date_iso = event_date.isoformat()
    else:
        display_date = "Check it out!"
        event_date_iso = ""

    notification_data = {
        "type": "community_event",
        "community_event_id": str(community_event.id),
        "community_id": str(community.id),
        "community_name": community.community_name,
        "event_name": community_event.event_name,
        "event_date": event_date_iso,
        "creator_id": str(event_creator.id) if event_creator else "",
        "creator_name": event_creator.name if event_creator else "",
        "timestamp": timezone.now().isoformat(),
    }

    for membership in memberships:
        recipient = membership.user

        if event_creator and recipient == event_creator:
            continue

        if recipient.id in muted_community_users:
            continue

        notification = Notification.objects.create(
            recipient=recipient,
            content=f"New event '{community_event.event_name}' posted in your community '{community.community_name}'",
            community_event=community_event,
            sender=event_creator if event_creator else None
        )

        send_push_notifications_to_user(
            user=recipient,
            title=f"New Event by {community.community_name}",
            body=f"'{community_event.event_name}' - {display_date}",
            data=notification_data
        )

        _broadcast_notification(notification, recipient.kinde_user_id)


@shared_task
def send_community_post_like_notification_task(like_id):
    from django.utils import timezone

    from .models import LikeCommunityPost, Membership, Notification
    from .firebase_utils import send_push_notifications_to_user

    try:
        like = LikeCommunityPost.objects.select_related(
            'event',
            'student',
            'event__community',
            'event__poster',
        ).get(pk=like_id)
    except LikeCommunityPost.DoesNotExist:
        logger.warning("LikeCommunityPost %s not found; skipping notification.", like_id)
        return

    community_post = like.event
    community = community_post.community
    liker = like.student
    post_author = getattr(community_post, 'poster', None)

    admin_memberships = Membership.objects.filter(
        community=community,
        role__in=['admin', 'secondary_admin']
    ).select_related('user')

    recipients = set()
    for membership in admin_memberships:
        if membership.user != liker:
            recipients.add(membership.user)

    if post_author and post_author != liker:
        recipients.add(post_author)

    if not recipients:
        return

    post_preview = community_post.post_text[:100] + "..." if len(community_post.post_text) > 100 else community_post.post_text

    notification_data = {
        "type": "community_post_like",
        "community_post_id": str(community_post.id),
        "community_id": str(community.id),
        "community_name": community.community_name,
        "liker_id": str(liker.id),
        "liker_name": liker.name,
        "post_preview": post_preview,
        "post_author_id": str(post_author.id) if post_author else "",
        "timestamp": timezone.now().isoformat(),
    }

    for recipient in recipients:
        if recipient == liker:
            continue

        if recipient == post_author:
            content = f"{liker.name} liked your post in {community.community_name}: '{post_preview}'"
        else:
            content = f"{liker.name} liked a post in your community: '{post_preview}'"

        notification = Notification.objects.create(
            recipient=recipient,
            content=content,
            community_post=community_post,
            sender=liker
        )

        send_push_notifications_to_user(
            user=recipient,
            title=f"{liker.name} liked a post",
            body=f"{liker.name} liked a post in {community.community_name}",
            data=notification_data
        )

        _broadcast_notification(notification, recipient.kinde_user_id)


@shared_task
def send_community_post_comment_notification_task(comment_id):
    from django.utils import timezone
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    from .models import Community_Posts_Comment, Membership, Notification
    from .firebase_utils import send_push_notifications_to_user
    from .serializers import CommunityPostCommentSerializer

    try:
        comment = Community_Posts_Comment.objects.select_related(
            'community_post',
            'community_post__community',
            'student',
            'community_post__poster',
        ).get(pk=comment_id)
    except Community_Posts_Comment.DoesNotExist:
        logger.warning("Community_Posts_Comment %s not found; skipping notification.", comment_id)
        return

    community_post = comment.community_post
    community = community_post.community
    commenter = comment.student
    post_author = getattr(community_post, 'poster', None)

    admin_memberships = Membership.objects.filter(
        community=community,
        role__in=['admin', 'secondary_admin']
    ).select_related('user')

    recipients = set()
    for membership in admin_memberships:
        if membership.user != commenter:
            recipients.add(membership.user)

    if post_author and post_author != commenter:
        recipients.add(post_author)

    if not recipients:
        return

    comment_preview = comment.comment_text[:100] + "..." if len(comment.comment_text) > 100 else comment.comment_text
    post_preview = community_post.post_text[:100] + "..." if len(community_post.post_text) > 100 else community_post.post_text

    notification_data = {
        "type": "community_post_comment",
        "community_post_id": str(community_post.id),
        "community_post_comment_id": str(comment.id),
        "community_id": str(community.id),
        "community_name": community.community_name,
        "commenter_id": str(commenter.id),
        "commenter_name": commenter.name,
        "comment_preview": comment_preview,
        "post_preview": post_preview,
        "post_author_id": str(post_author.id) if post_author else "",
        "timestamp": timezone.now().isoformat(),
    }

    for recipient in recipients:
        if recipient == commenter:
            continue

        if recipient == post_author:
            content = f"{commenter.name} commented on your post in {community.community_name}: '{post_preview}'"
        else:
            content = f"{commenter.name} commented on a post in your community: '{post_preview}'"

        notification = Notification.objects.create(
            recipient=recipient,
            content=content,
            community_post_comment=comment,
            community_post=community_post,
            sender=commenter
        )

        send_push_notifications_to_user(
            user=recipient,
            title=f"{commenter.name} commented on a post",
            body=comment_preview,
            data=notification_data
        )

        _broadcast_notification(notification, recipient.kinde_user_id)

    channel_layer = get_channel_layer()
    if channel_layer:
        comment_data = CommunityPostCommentSerializer(comment, context={'kinde_user_id': None}).data
        async_to_sync(channel_layer.group_send)(
            f'community_post_comments_{community_post.id}',
            {
                'type': 'comment_added',
                'comment_data': comment_data
            }
        )


@shared_task
def send_community_new_member_notification_task(membership_id):
    from django.utils import timezone

    from .models import Membership, Notification
    from .firebase_utils import send_push_notifications_to_user

    try:
        membership = Membership.objects.select_related('community', 'user').get(pk=membership_id)
    except Membership.DoesNotExist:
        logger.warning("Membership %s not found; skipping notification.", membership_id)
        return

    community = membership.community
    new_member = membership.user

    admin_memberships = Membership.objects.filter(
        community=community,
        role__in=['admin', 'secondary_admin']
    ).select_related('user')

    if not admin_memberships.exists():
        return

    notification_data = {
        "type": "community_new_member",
        "member_id": str(new_member.id),
        "community_id": str(community.id),
        "community_name": community.community_name,
        "new_member_id": str(new_member.id),
        "new_member_name": new_member.name,
        "membership_id": str(membership.id),
        "timestamp": timezone.now().isoformat(),
    }

    for admin_membership in admin_memberships:
        admin_user = admin_membership.user
        if admin_user == new_member:
            continue

        notification = Notification.objects.create(
            recipient=admin_user,
            content=f"{new_member.name} has joined your community '{community.community_name}'"
        )

        send_push_notifications_to_user(
            user=admin_user,
            title=f"New member joined {community.community_name}",
            body=f"{new_member.name} is now a member",
            data=notification_data
        )

        _broadcast_notification(notification, admin_user.kinde_user_id)


def _resolve_instance(model_label, instance_id):
    from django.apps import apps

    try:
        model = apps.get_model(model_label)
    except Exception:
        logger.error("Unable to resolve model label %s", model_label)
        return None

    try:
        return model.objects.get(pk=instance_id)
    except model.DoesNotExist:  # type: ignore[attr-defined]
        logger.warning("Instance %s for model %s not found.", instance_id, model_label)
        return None


def _get_parent_info(instance, content_type):
    if content_type == 'post':
        return instance.id, 'post'
    if content_type == 'comment':
        return instance.post.id if getattr(instance, 'post', None) else None, 'post'
    if content_type == 'community_post':
        return instance.id, 'community_post'
    if content_type == 'community_post_comment':
        parent = getattr(instance, 'community_post', None)
        return parent.id if parent else None, 'community_post'
    if content_type == 'student_event':
        return instance.id, 'student_event'
    if content_type == 'community_event':
        return instance.id, 'community_event'
    if content_type == 'student_event_discussion':
        parent = getattr(instance, 'student_event', None)
        return parent.id if parent else None, 'student_event'
    if content_type == 'community_event_discussion':
        parent = getattr(instance, 'community_event', None)
        return parent.id if parent else None, 'community_event'
    return None, None


def _get_content_preview(instance, content_type):
    text = ""
    if content_type == 'post':
        text = getattr(instance, 'context_text', "")[:100]
    elif content_type == 'comment':
        text = getattr(instance, 'comment', "")[:100]
    elif content_type == 'community_post':
        text = getattr(instance, 'post_text', "")[:100]
    elif content_type == 'community_post_comment':
        text = getattr(instance, 'comment_text', "")[:100]
    elif content_type in ['community_event_discussion', 'student_event_discussion']:
        text = getattr(instance, 'discussion_text', "")[:100]
    elif content_type in ['student_event', 'community_event']:
        if hasattr(instance, 'event_name'):
            description = getattr(instance, 'description', "") or ""
            text = f"{instance.event_name}: {description[:80]}"
        else:
            text = str(instance)[:100]
    return text


def _get_student_mention_content(instance, creator, mentioned_student, content_type):
    preview = _get_content_preview(instance, content_type)
    base = f"You were mentioned by {creator.name}"
    if content_type in ['post', 'community_post', 'student_event', 'community_event']:
        body = f"in their {content_type.replace('_', ' ')}: \"{preview[:50]}{'...' if len(preview) > 50 else ''}\""
    else:
        body = f"in a {content_type.replace('_', ' ')}: \"{preview[:50]}{'...' if len(preview) > 50 else ''}\""
    return (
        base,
        body,
        f"You were mentioned in a {content_type.replace('_', ' ')} by {creator.name}"
    )


def _get_community_mention_content(instance, creator, community, content_type):
    preview = _get_content_preview(instance, content_type)
    body = f"{creator.name} mentioned {community.community_name}: \"{preview[:50]}{'...' if len(preview) > 50 else ''}\""
    return (
        f"{creator.name} mentioned {community.community_name}",
        body,
        f"{creator.name} mentioned {community.community_name}"
    )


def _get_notification_foreign_key(instance, content_type):
    mapping = {
        'post': 'post',
        'comment': 'post_comment',
        'community_post': 'community_post',
        'community_post_comment': 'community_post_comment',
        'community_event_discussion': 'community_event_discussion',
        'student_event_discussion': 'student_event_discussion',
        'student_event': 'student_event',
        'community_event': 'community_event',
    }
    field = mapping.get(content_type)
    return {field: instance} if field else {}


@shared_task
def process_mentions_task(model_label, instance_id, mention_type, content_type, pk_list):
    from django.utils import timezone
    from django.db.models import Q

    from .models import Student, Communities, MutedStudents, MutedCommunities, Notification
    from .firebase_utils import send_push_notifications_to_user

    instance = _resolve_instance(model_label, instance_id)
    if not instance:
        return

    creator = getattr(instance, 'student', None) or getattr(instance, 'poster', None)
    if not creator:
        return

    muted_creator_users = set(
        MutedStudents.objects.filter(muted_student=creator).values_list('student__id', flat=True)
    )

    if mention_type == 'student':
        mentioned_entities = Student.objects.filter(id__in=pk_list)
        for mentioned_student in mentioned_entities:
            if mentioned_student.id == creator.id or mentioned_student.id in muted_creator_users:
                continue

            title, body, notification_content = _get_student_mention_content(
                instance, creator, mentioned_student, content_type
            )

            parent_id, parent_type = _get_parent_info(instance, content_type)

            notification = Notification.objects.create(
                recipient=mentioned_student,
                content=notification_content,
                **_get_notification_foreign_key(instance, content_type)
            )

            send_push_notifications_to_user(
                user=mentioned_student,
                title=title,
                body=body,
                data={
                    "type": f"{content_type}_student_mention",
                    f"{content_type}_id": str(instance.id),
                    "parent_id": str(parent_id) if parent_id else "",
                    "parent_type": str(parent_type) if parent_type else "",
                    "creator_id": str(creator.id),
                    "creator_name": str(creator.name),
                    "content_preview": str(_get_content_preview(instance, content_type)),
                    "timestamp": timezone.now().isoformat(),
                }
            )

            _broadcast_notification(notification, mentioned_student.kinde_user_id)
    elif mention_type == 'community':
        mentioned_entities = Communities.objects.filter(id__in=pk_list)
        for community in mentioned_entities:
            community_admins = Student.objects.filter(
                membership__community=community,
                membership__role__in=['admin', 'secondary_admin']
            )

            muted_community_users = set(
                MutedCommunities.objects.filter(community=community).values_list('student__id', flat=True)
            )

            for admin in community_admins:
                if admin.id == creator.id or admin.id in muted_community_users:
                    continue

                title, body, notification_content = _get_community_mention_content(
                    instance, creator, community, content_type
                )

                parent_id, parent_type = _get_parent_info(instance, content_type)

                notification = Notification.objects.create(
                    recipient=admin,
                    content=notification_content,
                    **_get_notification_foreign_key(instance, content_type)
                )

                send_push_notifications_to_user(
                    user=admin,
                    title=title,
                    body=body,
                    data={
                        "type": f"{content_type}_community_mention",
                        f"{content_type}_id": str(instance.id),
                        "parent_id": str(parent_id) if parent_id else "",
                        "parent_type": str(parent_type) if parent_type else "",
                        "creator_id": str(creator.id),
                        "creator_name": str(creator.name),
                        "community_id": str(community.id),
                        "community_name": str(community.community_name),
                        "content_preview": str(_get_content_preview(instance, content_type)),
                        "timestamp": timezone.now().isoformat(),
                    }
                )

                _broadcast_notification(notification, admin.kinde_user_id)


@shared_task
def send_student_event_rsvp_notification_task(rsvp_id):
    from django.utils import timezone

    from .models import EventRSVP, Notification, MutedStudents
    from .firebase_utils import send_push_notifications_to_user

    try:
        rsvp = EventRSVP.objects.select_related(
            'event',
            'event__student',
            'student',
        ).get(pk=rsvp_id)
    except EventRSVP.DoesNotExist:
        logger.warning("EventRSVP %s not found; skipping notification.", rsvp_id)
        return

    event = rsvp.event
    event_creator = event.student
    rsvp_student = rsvp.student

    if not event_creator or event_creator == rsvp_student:
        return

    is_muted = MutedStudents.objects.filter(
        student=event_creator,
        muted_student=rsvp_student
    ).exists()

    if is_muted:
        return

    status_text = rsvp.status.replace('_', ' ').title()

    notification = Notification.objects.create(
        recipient=event_creator,
        content=f"{rsvp_student.name} is {status_text.lower()} for your event '{event.event_name}'",
        student_event=event,
        sender=rsvp_student
    )

    send_push_notifications_to_user(
        user=event_creator,
        title="New RSVP for your event",
        body=f"{rsvp_student.name} is {status_text.lower()}",
        data={
            "type": "student_event_rsvp",
            "student_event_id": str(event.id),
            "rsvp_id": str(rsvp.id),
            "rsvp_student_id": str(rsvp_student.id),
            "rsvp_student_name": str(rsvp_student.name),
            "rsvp_student_username": str(getattr(rsvp_student, 'username', '')),
            "rsvp_student_profile_picture": str(rsvp_student.profile_image.url if rsvp_student.profile_image else ""),
            "rsvp_status": str(rsvp.status),
            "event_name": str(event.event_name),
            "timestamp": timezone.now().isoformat(),
        }
    )

    _broadcast_notification(notification, event_creator.kinde_user_id)


@shared_task
def send_community_event_rsvp_notification_task(rsvp_id):
    from django.utils import timezone

    from .models import CommunityEventRSVP, Membership, Notification, MutedStudents
    from .firebase_utils import send_push_notifications_to_user

    try:
        rsvp = CommunityEventRSVP.objects.select_related(
            'event',
            'event__community',
            'event__poster',
            'student',
        ).get(pk=rsvp_id)
    except CommunityEventRSVP.DoesNotExist:
        logger.warning("CommunityEventRSVP %s not found; skipping notification.", rsvp_id)
        return

    event = rsvp.event
    community = event.community
    event_creator = event.poster
    rsvp_student = rsvp.student

    admin_memberships = Membership.objects.filter(
        community=community,
        role__in=['admin', 'secondary_admin']
    ).select_related('user')

    recipients = set()
    if event_creator and event_creator != rsvp_student:
        recipients.add(event_creator)

    for membership in admin_memberships:
        if membership.user != rsvp_student:
            recipients.add(membership.user)

    if not recipients:
        return

    status_text = rsvp.status.replace('_', ' ').title()
    notification_data = {
        "type": "community_event_rsvp",
        "community_event_id": str(event.id),
        "community_id": str(community.id),
        "community_name": str(community.community_name),
        "rsvp_id": str(rsvp.id),
        "rsvp_student_id": str(rsvp_student.id),
        "rsvp_student_name": str(rsvp_student.name),
        "rsvp_student_username": str(getattr(rsvp_student, 'username', '')),
        "rsvp_student_profile_picture": str(rsvp_student.profile_image.url if rsvp_student.profile_image else ""),
        "rsvp_status": str(rsvp.status),
        "event_name": str(event.event_name),
        "timestamp": timezone.now().isoformat(),
    }

    for recipient in recipients:
        is_muted = MutedStudents.objects.filter(
            student=recipient,
            muted_student=rsvp_student
        ).exists()

        if is_muted:
            continue

        if recipient == event_creator:
            content = f"{rsvp_student.name} is {status_text.lower()} for your event '{event.event_name}' in {community.community_name}"
        else:
            content = f"{rsvp_student.name} is {status_text.lower()} for the event '{event.event_name}' in your community {community.community_name}"

        notification = Notification.objects.create(
            recipient=recipient,
            content=content,
            community_event=event,
            sender=rsvp_student
        )

        send_push_notifications_to_user(
            user=recipient,
            title=f"New RSVP for {community.community_name} event",
            body=f"{rsvp_student.name} is {status_text.lower()}",
            data=notification_data
        )

        _broadcast_notification(notification, recipient.kinde_user_id)


@shared_task
def send_student_event_discussion_notification_task(discussion_id):
    from django.utils import timezone
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    from .models import Student_Events_Discussion, Notification, MutedStudents
    from .firebase_utils import send_push_notifications_to_user
    from .serializers import StudentEventDiscussionSerializer

    try:
        discussion = Student_Events_Discussion.objects.select_related(
            'student_event',
            'student_event__student',
            'student',
        ).get(pk=discussion_id)
    except Student_Events_Discussion.DoesNotExist:
        logger.warning("Student_Events_Discussion %s not found; skipping notification.", discussion_id)
        return

    event = discussion.student_event
    event_creator = event.student
    discussion_student = discussion.student

    if event_creator == discussion_student:
        return

    is_muted = MutedStudents.objects.filter(
        student=event_creator,
        muted_student=discussion_student
    ).exists()

    if is_muted:
        return

    discussion_preview = discussion.discussion_text[:100] + "..." if len(discussion.discussion_text) > 100 else discussion.discussion_text

    notification = Notification.objects.create(
        recipient=event_creator,
        content=f"{discussion_student.name} commented on your event '{event.event_name}': '{discussion_preview}'",
        student_event_discussion=discussion,
        student_event=event,
        sender=discussion_student
    )

    send_push_notifications_to_user(
        user=event_creator,
        title="New discussion on your event",
        body=f"{discussion_student.name}: {discussion_preview}",
        data={
            "type": "student_event_discussion",
            "student_event_id": str(event.id),
            "discussion_id": str(discussion.id),
            "discussion_student_id": str(discussion_student.id),
            "discussion_student_name": str(discussion_student.name),
            "discussion_student_username": str(getattr(discussion_student, 'username', '')),
            "discussion_student_profile_picture": str(discussion_student.profile_image.url if discussion_student.profile_image else ""),
            "discussion_preview": str(discussion_preview),
            "event_name": str(event.event_name),
            "timestamp": timezone.now().isoformat(),
        }
    )

    _broadcast_notification(notification, event_creator.kinde_user_id)

    channel_layer = get_channel_layer()
    if channel_layer:
        discussion_data = StudentEventDiscussionSerializer(discussion, context={'kinde_user_id': None}).data
        async_to_sync(channel_layer.group_send)(
            f'student_event_discussions_{event.id}',
            {
                'type': 'discussion_added',
                'discussion_data': discussion_data
            }
        )


@shared_task
def send_community_event_discussion_notification_task(discussion_id):
    from django.utils import timezone
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync

    from .models import Community_Events_Discussion, Membership, Notification, MutedStudents
    from .firebase_utils import send_push_notifications_to_user
    from .serializers import CommunityEventDiscussionSerializer

    try:
        discussion = Community_Events_Discussion.objects.select_related(
            'community_event',
            'community_event__community',
            'community_event__poster',
            'student',
        ).get(pk=discussion_id)
    except Community_Events_Discussion.DoesNotExist:
        logger.warning("Community_Events_Discussion %s not found; skipping notification.", discussion_id)
        return

    event = discussion.community_event
    community = event.community
    event_creator = event.poster
    discussion_student = discussion.student

    admin_memberships = Membership.objects.filter(
        community=community,
        role__in=['admin', 'secondary_admin']
    ).select_related('user')

    recipients = set()
    if event_creator and event_creator != discussion_student:
        recipients.add(event_creator)

    for membership in admin_memberships:
        if membership.user != discussion_student:
            recipients.add(membership.user)

    if not recipients:
        return

    discussion_preview = discussion.discussion_text[:100] + "..." if len(discussion.discussion_text) > 100 else discussion.discussion_text
    notification_data = {
        "type": "community_event_discussion",
        "community_event_id": str(event.id),
        "community_id": str(community.id),
        "community_name": str(community.community_name),
        "discussion_id": str(discussion.id),
        "discussion_student_id": str(discussion_student.id),
        "discussion_student_name": str(discussion_student.name),
        "discussion_student_username": str(getattr(discussion_student, 'username', '')),
        "discussion_student_profile_picture": str(discussion_student.profile_image.url if discussion_student.profile_image else ""),
        "discussion_preview": str(discussion_preview),
        "event_name": str(event.event_name),
        "timestamp": timezone.now().isoformat(),
    }

    for recipient in recipients:
        is_muted = MutedStudents.objects.filter(
            student=recipient,
            muted_student=discussion_student
        ).exists()

        if is_muted:
            continue

        if recipient == event_creator:
            content = f"{discussion_student.name} commented on your event '{event.event_name}' in {community.community_name}: '{discussion_preview}'"
        else:
            content = f"{discussion_student.name} commented on the event '{event.event_name}' in your community {community.community_name}: '{discussion_preview}'"

        notification = Notification.objects.create(
            recipient=recipient,
            content=content,
            community_event_discussion=discussion,
            community_event=event,
            sender=discussion_student
        )

        send_push_notifications_to_user(
            user=recipient,
            title=f"New discussion on {community.community_name} event",
            body=f"{discussion_student.name}: {discussion_preview}",
            data=notification_data
        )

        _broadcast_notification(notification, recipient.kinde_user_id)

    channel_layer = get_channel_layer()
    if channel_layer:
        discussion_data = CommunityEventDiscussionSerializer(discussion, context={'kinde_user_id': None}).data
        async_to_sync(channel_layer.group_send)(
            f'community_event_discussions_{event.id}',
            {
                'type': 'discussion_added',
                'discussion_data': discussion_data
            }
        )


@shared_task
def process_data_deletion_requests():
    """
    Celery task to process data deletion requests that are older than 30 days.
    This should be scheduled to run daily (e.g., via Celery Beat).
    """
    from django.utils import timezone
    from django.db import transaction
    from .models import (
        DataDeletionRequest, Student, PostComment, Community_Posts_Comment,
        Student_Events_Discussion, Community_Events_Discussion,
        Posts, Student_Events, Community_Posts, Community_Events,
        PostLike, LikeCommunityPost, LikeEvent, LikeCommunityEvent,
        BookmarkedPosts, BookmarkedCommunityPosts, BookmarkedStudentEvents,
        BookmarkedCommunityEvents, EventRSVP, CommunityEventRSVP,
        Friendship, Membership, Block, MutedStudents, MutedCommunities,
        BlockedByCommunities, DirectMessage, Notification, CommunityChatMessage,
        DeviceToken, EmailVerification, SavedPost, SavedCommunityPost,
        SavedStudentEvents, Report
    )
    from django.db.models import Q
    
    now = timezone.now()
    
    # Find all deletion requests that are past their scheduled deletion date and not cancelled
    deletion_requests = DataDeletionRequest.objects.filter(
        scheduled_deletion_date__lte=now,
        is_cancelled=False,
        deleted_at__isnull=True  # Not already processed
    ).select_related('student')
    
    processed_count = 0
    error_count = 0
    
    for deletion_request in deletion_requests:
        try:
            student = deletion_request.student
            student_id = student.id
            student_name = student.name
            student_email = student.email  # Capture email before deletion
            student_username = student.username  # Capture username before deletion
            kinde_user_id = student.kinde_user_id  # Capture Kinde user ID before deletion
            
            # Perform the actual deletion
            with transaction.atomic():
                # 1. Anonymize comments that use SET_NULL
                PostComment.objects.filter(student=student).update(comment="[Deleted]")
                Community_Posts_Comment.objects.filter(student=student).update(comment_text="[Deleted]")
                
                # 2. Delete event discussions
                Student_Events_Discussion.objects.filter(student=student).delete()
                Community_Events_Discussion.objects.filter(student=student).delete()
                
                # 3. Delete all user-created content
                Posts.objects.filter(student=student).delete()
                Student_Events.objects.filter(student=student).delete()
                Community_Posts.objects.filter(poster=student).delete()
                Community_Events.objects.filter(poster=student).delete()
                
                # 4. Delete user interactions
                PostLike.objects.filter(student=student).delete()
                LikeCommunityPost.objects.filter(student=student).delete()
                LikeEvent.objects.filter(student=student).delete()
                LikeCommunityEvent.objects.filter(student=student).delete()
                BookmarkedPosts.objects.filter(student=student).delete()
                BookmarkedCommunityPosts.objects.filter(student=student).delete()
                BookmarkedStudentEvents.objects.filter(student=student).delete()
                BookmarkedCommunityEvents.objects.filter(student=student).delete()
                EventRSVP.objects.filter(student=student).delete()
                CommunityEventRSVP.objects.filter(student=student).delete()
                
                # 5. Delete social connections
                Friendship.objects.filter(Q(sender=student) | Q(receiver=student)).delete()
                Membership.objects.filter(user=student).delete()
                Block.objects.filter(Q(blocker=student) | Q(blocked=student)).delete()
                MutedStudents.objects.filter(Q(student=student) | Q(muted_student=student)).delete()
                MutedCommunities.objects.filter(student=student).delete()
                BlockedByCommunities.objects.filter(blocked_student=student).delete()
                
                # 6. Delete messages and notifications
                DirectMessage.objects.filter(Q(sender=student) | Q(receiver=student)).delete()
                Notification.objects.filter(Q(recipient=student) | Q(sender=student)).delete()
                CommunityChatMessage.objects.filter(student=student).delete()
                
                # 7. Delete device tokens and verification records
                DeviceToken.objects.filter(user=student).delete()
                EmailVerification.objects.filter(student=student).delete()
                
                # 8. Delete saved posts/events
                SavedPost.objects.filter(student=student).delete()
                SavedCommunityPost.objects.filter(student=student).delete()
                SavedStudentEvents.objects.filter(student=student).delete()
                
                # 9. Delete reports
                Report.objects.filter(user=student).delete()
                
                # 10. Clear ManyToMany relationships
                student.student_interest.clear()
                student.student_mentions.clear()
                student.community_mentions.clear()
                
                # 11. Finally, delete the Student record itself
                student.delete()
            
            # Verify that the account is fully deleted
            try:
                # Check if student still exists (should not)
                student_exists = Student.objects.filter(id=student_id).exists()
                if student_exists:
                    logger.error(f"CRITICAL: Student {student_id} still exists after deletion attempt!")
                    raise Exception(f"Student {student_id} was not fully deleted")
                
                # Verify related data is cleaned up
                remaining_posts = Posts.objects.filter(student_id=student_id).count()
                remaining_events = Student_Events.objects.filter(student_id=student_id).count()
                remaining_friendships = Friendship.objects.filter(Q(sender_id=student_id) | Q(receiver_id=student_id)).count()
                remaining_memberships = Membership.objects.filter(user_id=student_id).count()
                remaining_messages = DirectMessage.objects.filter(Q(sender_id=student_id) | Q(receiver_id=student_id)).count()
                
                if remaining_posts > 0 or remaining_events > 0 or remaining_friendships > 0 or remaining_memberships > 0 or remaining_messages > 0:
                    logger.warning(f"Some data may remain for user {student_id}: posts={remaining_posts}, events={remaining_events}, friendships={remaining_friendships}, memberships={remaining_memberships}, messages={remaining_messages}")
                else:
                    logger.info(f"Verified complete deletion of user {student_id} - all related data removed")
                    
            except Exception as verify_error:
                logger.error(f"Error during deletion verification for user {student_id}: {str(verify_error)}")
                # Continue with email notifications even if verification has issues
            
            # Mark the deletion request as processed
            deletion_request.deleted_at = now
            deletion_request.save()
            
            # Note: Kinde user deletion happens immediately when deletion request is created
            # No need to delete from Kinde here as it's already done
            
            # Send confirmation email to admin after successful deletion
            try:
                # Send confirmation email to admin
                send_admin_deletion_confirmation.delay(
                    student_email,
                    student_name,
                    student_username or 'N/A'
                )
            except Exception as email_error:
                logger.error(f"Failed to send admin deletion confirmation email for user {student_email}: {str(email_error)}")
                # Don't fail the deletion if email fails
            
            processed_count += 1
            logger.info(f"Processed data deletion for user {student_id} ({student_name}) - request ID: {deletion_request.id}")
            
        except Exception as e:
            error_count += 1
            logger.error(f"Error processing deletion request {deletion_request.id}: {str(e)}")
            import traceback
            traceback.print_exc()
    
    logger.info(f"Data deletion task completed: {processed_count} processed, {error_count} errors")
    return {
        'processed': processed_count,
        'errors': error_count
    }