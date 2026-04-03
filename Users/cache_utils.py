from django.core.cache import cache
from django.db.models import Q

DEFAULT_TIMEOUT = 3600
RELATIONSHIP_CACHE_TIMEOUT = 300


def set_cache(key, value, timeout=DEFAULT_TIMEOUT):
    cache.set(key, value, timeout)


def get_cache(key):
    return cache.get(key)


def invalidate_cache(key):
    cache.delete(key)


def build_student_block_cache_key(student_id: int) -> str:
    return f"blocks:outgoing:{student_id}"


def build_student_blocked_by_cache_key(student_id: int) -> str:
    return f"blocks:incoming:{student_id}"


def build_student_blocked_by_comm_cache_key(student_id: int) -> str:
    return f"blocks:communities:{student_id}"


def build_muted_students_cache_key(student_id: int) -> str:
    return f"mutes:students:{student_id}"


def build_muted_communities_cache_key(student_id: int) -> str:
    return f"mutes:communities:{student_id}"


def build_blocking_snapshot_key(student_id: int) -> str:
    return f"blocking:snapshot:{student_id}"


def build_friends_snapshot_cache_key(student_id: int) -> str:
    return f"friends:snapshot:{student_id}"


def build_pair_block_cache_key(student_a_id: int, student_b_id: int) -> str:
    low, high = sorted((student_a_id, student_b_id))
    return f"block_status:{low}:{high}"


def get_outgoing_block_ids(student_id: int):
    """Get list of student IDs that this student has blocked. No longer cached individually."""
    from .models import Block
    return list(Block.objects.filter(blocker_id=student_id).values_list('blocked_id', flat=True))


def get_incoming_block_ids(student_id: int):
    """Get list of student IDs that have blocked this student. No longer cached individually."""
    from .models import Block
    return list(Block.objects.filter(blocked_id=student_id).values_list('blocker_id', flat=True))


def get_blocking_communities(student_id: int):
    """Get list of community IDs that have blocked this student. No longer cached individually."""
    from .models import BlockedByCommunities
    return list(BlockedByCommunities.objects.filter(blocked_student_id=student_id).values_list('community_id', flat=True))


def get_muted_student_ids(student_id: int):
    """Get list of student IDs that this student has muted. No longer cached individually."""
    from .models import MutedStudents
    return list(MutedStudents.objects.filter(student_id=student_id).values_list('muted_student_id', flat=True))


def get_muted_community_ids(student_id: int):
    """Get list of community IDs that this student has muted. No longer cached individually."""
    from .models import MutedCommunities
    return list(MutedCommunities.objects.filter(student_id=student_id).values_list('community_id', flat=True))


def get_friend_snapshot(student_id: int):
    key = build_friends_snapshot_cache_key(student_id)
    snapshot = get_cache(key)
    if snapshot is not None:
        return snapshot

    from .models import Student

    friends_qs = Student.objects.filter(
        Q(sent_requests__receiver_id=student_id, sent_requests__status='accepted') |
        Q(received_requests__sender_id=student_id, received_requests__status='accepted')
    ).distinct()

    friend_details = list(
        friends_qs.values(
            'id',
            'kinde_user_id',
            'name',
            'username',
            'bio',
            'profile_image',
        )
    )

    for entry in friend_details:
        profile_image = entry.get('profile_image')
        if profile_image:
            entry['profile_image'] = profile_image if isinstance(profile_image, str) else getattr(profile_image, 'url', None)
        entry['is_online'] = False

    snapshot = {
        'ids': [entry['id'] for entry in friend_details],
        'details': friend_details,
    }
    set_cache(key, snapshot, timeout=RELATIONSHIP_CACHE_TIMEOUT)
    return snapshot


def invalidate_friend_snapshot(student_id: int):
    invalidate_cache(build_friends_snapshot_cache_key(student_id))


def get_relationship_snapshot(student_id: int):
    key = build_blocking_snapshot_key(student_id)
    snapshot = get_cache(key)
    if snapshot is not None:
        return snapshot

    snapshot = {
        'blocking': get_outgoing_block_ids(student_id),
        'blocked_by': get_incoming_block_ids(student_id),
        'blocked_by_communities': get_blocking_communities(student_id),
        'muted_students': get_muted_student_ids(student_id),
        'muted_communities': get_muted_community_ids(student_id),
    }
    set_cache(key, snapshot, timeout=RELATIONSHIP_CACHE_TIMEOUT)
    return snapshot


def invalidate_relationship_snapshot(student_id: int):
    invalidate_cache(build_blocking_snapshot_key(student_id))


def invalidate_pair_block_cache(student_a_id: int, student_b_id: int):
    invalidate_cache(build_pair_block_cache_key(student_a_id, student_b_id))


def has_user_blocked(blocker_id: int, candidate_id: int) -> bool:
    """
    Check if blocker_id has blocked candidate_id.
    Uses relationship snapshot which is efficient when you need multiple checks
    for the same blocker_id. For single checks, use check_single_block instead.
    """
    snapshot = get_relationship_snapshot(blocker_id)
    blocking_ids = snapshot.get('blocking', []) if snapshot else []
    return candidate_id in blocking_ids


def check_single_block(blocker_id: int, candidate_id: int) -> bool:
    """
    Check if blocker_id has blocked candidate_id.
    Optimized for single checks - only queries/caches this specific relationship,
    not the full relationship snapshot. Use this when checking if a target student
    has blocked the requester (don't need full snapshot for target).
    """
    # Use a specific cache key for this unidirectional check
    key = f"block_check:{blocker_id}:{candidate_id}"
    cached = get_cache(key)
    if cached is not None:
        return cached
    
    # Direct database query - only check this specific relationship
    from .models import Block
    result = Block.objects.filter(blocker_id=blocker_id, blocked_id=candidate_id).exists()
    
    # Cache this specific check result
    set_cache(key, result, timeout=RELATIONSHIP_CACHE_TIMEOUT)
    return result


def have_block_relationship(student_a_id: int, student_b_id: int) -> bool:
    key = build_pair_block_cache_key(student_a_id, student_b_id)
    cached = get_cache(key)
    if cached is not None:
        return cached

    block_exists = has_user_blocked(student_a_id, student_b_id)
    if not block_exists:
        block_exists = has_user_blocked(student_b_id, student_a_id)

    set_cache(key, block_exists, timeout=RELATIONSHIP_CACHE_TIMEOUT)
    return block_exists


def is_blocked_by_community(student_id: int, community_id: int) -> bool:
    snapshot = get_relationship_snapshot(student_id)
    blocked = snapshot.get('blocked_by_communities', []) if snapshot else []
    return community_id in blocked