from django.db.models import F, ExpressionWrapper, fields, Count, Value, Q, Case, When
from django.db.models.functions import Power, Now, Extract
from datetime import timedelta
from django.db.models import OuterRef, Subquery # For complex subqueries if needed

# --- SCORING WEIGHTS (Adjust these values to fine-tune your feed) ---
W_POPULARITY = 1.0        # Weight for overall trending score
W_INTEREST = 5.0          # Weight for matching user's interests
W_FRIEND_ACTIVITY = 10.0  # Weight for activity from accepted friends (likes/RSVPs)
W_LOCATION = 2.0          # Weight for items in user's region/location
W_AUTHOR_FRIEND = 15.0    # Weight for content authored by a direct friend
W_MEMBER_COMMUNITY = 8.0  # Weight for content from a community the user belongs to

# --- Trending/Popularity Score (Adapted from your previous calculate_trending_score) ---
def get_popularity_score_annotations(time_field_name, like_related_name=None, comment_related_name=None, rsvp_related_name=None):
    annotations = {}

    # Step 1: Calculate raw interaction counts
    if rsvp_related_name:
        annotations['_raw_interactions'] = Count(rsvp_related_name, distinct=True)
    else:
        likes_count_expr = Count(like_related_name, distinct=True) if like_related_name else Value(0)
        comments_count_expr = Count(comment_related_name, distinct=True) if comment_related_name else Value(0)
        annotations['_raw_interactions'] = ExpressionWrapper(
            likes_count_expr + comments_count_expr,
            output_field=fields.IntegerField()
        )
    
    # Step 2: Calculate hours since creation and a safe (non-zero) version
# Step 2: Calculate hours since creation and a safe (non-zero) version
    annotations['_time_diff_seconds'] = ExpressionWrapper(
        Now() - F(time_field_name),
        output_field=fields.DurationField()
    )
    
    # --- FIX HERE ---
    # Extract total seconds from the DurationField (interval)
    total_seconds_since_creation = Extract('_time_diff_seconds', 'epoch')  
    
    # Convert total seconds to hours and ensure float division
    annotations['_hours_since_creation'] = ExpressionWrapper(
        total_seconds_since_creation / 3600.0, # Divide by 3600.0 (float) to get hours as a float
        output_field=fields.FloatField()
    )
    # --- END FIX ---
    
    annotations['popularity_safe_hours_since_creation'] = Case(
        When(Q(**{F('_hours_since_creation').name + '__lt': 1}), then=Value(1.0)), 
        default=F('_hours_since_creation'),
        output_field=fields.FloatField()
    )

    # Step 3: Calculate score numerator (referencing _raw_interactions alias)
    # --- FIX 2 ---
    annotations['__score_numerator'] = Case(
        When(Q(**{F('_raw_interactions').name + '__gt': 0}), then=F('_raw_interactions')), # Corrected syntax
        default=Value(0.0),
        output_field=fields.FloatField()
    )
    
    # Step 4: Calculate final denominator for decay
    annotations['__score_denominator_raw'] = Power(F('popularity_safe_hours_since_creation'), Value(1.5))
    # --- FIX 3 ---
    annotations['popularity_score_denominator'] = Case(
        When(Q(**{F('__score_denominator_raw').name + '__exact': 0}), then=Value(1.0)), 
        default=F('__score_denominator_raw'),
        output_field=fields.FloatField()
    )

    # Step 5: Final popularity score
    annotations['popularity_score'] = ExpressionWrapper(
        F('__score_numerator') / F('popularity_score_denominator'),
        output_field=fields.FloatField()
    )
    
    return annotations

# --- Other scoring functions will follow a similar pattern, returning dictionaries ---
# Make sure they use unique aliases and return 'location_score', 'interest_match_score', 'friend_activity_score'

# --- REVISED: get_location_match_annotations ---
def get_location_match_annotations(user_region_id, item_location_field_name):
    annotations = {}
    if not user_region_id:
        annotations['location_score'] = Value(0.0, output_field=fields.FloatField())
        return annotations
    annotations['location_score'] = Case(
        When(**{f'{item_location_field_name}__region_id': user_region_id, 'then': Value(1.0)}),
        default=Value(0.0),
        output_field=fields.FloatField()
    )
    return annotations

# --- REVISED: get_interest_overlap_annotations ---
def get_interest_overlap_annotations(user_interest_ids, item_interest_related_name):
    annotations = {}
    if not user_interest_ids:
        annotations['interest_match_score'] = Value(0.0)
        return annotations

    annotations['interest_match_score'] = Count(
        item_interest_related_name,
        filter=Q(**{f'{item_interest_related_name}__id__in': user_interest_ids}),
        distinct=True,
        output_field=fields.FloatField()
    )
    return annotations

# --- REVISED: get_friend_activity_annotations ---
def get_friend_activity_annotations(accepted_friend_ids, like_related_name=None, rsvp_related_name=None):
    annotations = {}
    if not accepted_friend_ids:
        annotations['friend_activity_score'] = Value(0.0)
        return annotations

    annotations['_friend_likes_count'] = Value(0.0, output_field=fields.FloatField())
    annotations['_friend_rsvps_count'] = Value(0.0, output_field=fields.FloatField())

    if like_related_name:
        annotations['_friend_likes_count'] = Count(
            like_related_name,
            filter=Q(**{f'{like_related_name}__student__id__in': accepted_friend_ids}),
            distinct=True,
            output_field=fields.FloatField()
        )
    
    if rsvp_related_name:
        annotations['_friend_rsvps_count'] = Count(
            rsvp_related_name,
            filter=Q(**{f'{rsvp_related_name}__student__id__in': accepted_friend_ids}),
            distinct=True,
            output_field=fields.FloatField()
        )
    
    annotations['friend_activity_score'] = ExpressionWrapper(
        F('_friend_likes_count') + F('_friend_rsvps_count'),
        output_field=fields.FloatField()
    )
    return annotations

# --- Location Match Score ---
def get_location_match_annotations(user_region_id, item_location_field_name):
    """
    Returns a dictionary of ORM annotations for calculating location match score.
    The score is under the 'location_score' key.
    """
    annotations = {}
    if not user_region_id:
        annotations['location_score'] = Value(0.0, output_field=fields.FloatField())
        return annotations
    annotations['location_score'] = Case(
        When(**{f'{item_location_field_name}__region_id': user_region_id, 'then': Value(1.0)}),
        default=Value(0.0),
        output_field=fields.FloatField()
    )
    return annotations

# --- REVISED: get_interest_overlap_annotations (now returns a dict) ---
def get_interest_overlap_annotations(user_interest_ids, item_interest_related_name):
    """
    Returns a dictionary of ORM annotations for calculating interest overlap score.
    The score is under the 'interest_match_score' key.
    """
    annotations = {}
    if not user_interest_ids:
        annotations['interest_match_score'] = Value(0.0)
        return annotations

    annotations['interest_match_score'] = Count(
        item_interest_related_name,
        filter=Q(**{f'{item_interest_related_name}__id__in': user_interest_ids}),
        distinct=True, # Count distinct overlapping interests
        output_field=fields.FloatField() # Ensure float for division
    )
    return annotations

# --- REVISED: get_friend_activity_annotations (now returns a dict) ---
def get_friend_activity_annotations(accepted_friend_ids, like_related_name=None, rsvp_related_name=None):
    """
    Returns a dictionary of ORM annotations for calculating friend activity score.
    The score is under the 'friend_activity_score' key.
    """
    annotations = {}
    if not accepted_friend_ids:
        annotations['friend_activity_score'] = Value(0.0)
        return annotations

    # Initialize a base score expression
    annotations['_friend_likes_count'] = Value(0.0, output_field=fields.FloatField()) # Intermediate alias
    annotations['_friend_rsvps_count'] = Value(0.0, output_field=fields.FloatField()) # Intermediate alias

    if like_related_name:
        annotations['_friend_likes_count'] = Count(
            like_related_name,
            filter=Q(**{f'{like_related_name}__student__id__in': accepted_friend_ids}),
            distinct=True,
            output_field=fields.FloatField()
        )
    
    if rsvp_related_name:
        annotations['_friend_rsvps_count'] = Count(
            rsvp_related_name,
            filter=Q(**{f'{rsvp_related_name}__student__id__in': accepted_friend_ids}),
            distinct=True,
            output_field=fields.FloatField()
        )
    
    annotations['friend_activity_score'] = ExpressionWrapper(
        F('_friend_likes_count') + F('_friend_rsvps_count'), # Sum intermediate aliases
        output_field=fields.FloatField()
    )
    return annotations


def get_author_friend_annotations(accepted_friend_ids, author_field):
    """
    Returns 1.0 if the post/event author is a direct friend, 0.0 otherwise.
    author_field: ORM field path to the author Student FK (e.g. 'student', 'poster')
    """
    annotations = {}
    if not accepted_friend_ids:
        annotations['author_friend_score'] = Value(0.0, output_field=fields.FloatField())
        return annotations
    annotations['author_friend_score'] = Case(
        When(**{f'{author_field}__id__in': accepted_friend_ids}, then=Value(1.0)),
        default=Value(0.0),
        output_field=fields.FloatField()
    )
    return annotations


def get_community_membership_annotations(user_community_ids, community_field):
    """
    Returns 1.0 if the post/event belongs to a community the user is a member of, 0.0 otherwise.
    community_field: ORM field path to the community FK (e.g. 'community')
    """
    annotations = {}
    if not user_community_ids:
        annotations['community_member_score'] = Value(0.0, output_field=fields.FloatField())
        return annotations
    annotations['community_member_score'] = Case(
        When(**{f'{community_field}__id__in': user_community_ids}, then=Value(1.0)),
        default=Value(0.0),
        output_field=fields.FloatField()
    )
    return annotations