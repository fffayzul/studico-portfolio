from rest_framework import serializers
from .models import *
from asgiref.sync import sync_to_async

MODEL_MAP = {
    'student': Student,
    'post': Posts,
    'community_post': Community_Posts,
    'direct_message': DirectMessage,
    'community_event': Community_Events,
    'student_event': Student_Events,
    # Add other reportable models here
}

class RecursiveCommentSerializer(serializers.Serializer):
    """
    Serializer for recursive field display.
    This acts as a proxy to resolve circular dependencies.
    """
    def to_representation(self, instance):
        # Dynamically instantiate the main comment serializer
        # This calls the PostCommentDisplaySerializer, passing the context
        serializer = self.parent.parent.__class__(instance, context=self.context)
        return serializer.data





class StudentNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Student
        fields = '__all__'

class StudentMentionSerializer(serializers.ModelSerializer):
    """Simple serializer for student mentions - only returns id and username"""
    class Meta:
        model = Student
        fields = ['id', 'username']


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = '__all__'


class InterestSerializer(serializers.ModelSerializer):
    class Meta:
        model = Interests
        fields = ['interest']

class StudentSerializer(serializers.ModelSerializer):
    interests = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    is_muted = serializers.SerializerMethodField()
    is_blocked = serializers.SerializerMethodField()
    mutual_friends = serializers.SerializerMethodField()
    country_code = serializers.SerializerMethodField()


    class Meta:
        model = Student
        fields = '__all__'

    def get_country_code(self, obj):
        return obj.country.code if obj.country else None

    def get_interests(self, obj):
        interests = obj.student_interest.all()
        return InterestSerializer(interests, many=True).data

    def get_location(self, obj):
        return LocationSerializer(obj.student_location).data

    def get_is_muted(self, obj):
        """Check if the requesting user has muted this student. Use context user_muted_student_ids (set) to avoid N+1."""
        user_muted_ids = self.context.get('user_muted_student_ids')
        if user_muted_ids is not None:
            return obj.id in user_muted_ids
        kinde_id = self.context.get('kinde_user_id')
        if kinde_id:
            try:
                return MutedStudents.objects.filter(
                    student__kinde_user_id=kinde_id,
                    muted_student=obj
                ).exists()
            except Student.DoesNotExist:
                return False
        return False

    def get_is_blocked(self, obj):
        """Check if the requesting user has blocked this student. Use context user_blocked_student_ids (set) to avoid N+1."""
        user_blocked_ids = self.context.get('user_blocked_student_ids')
        if user_blocked_ids is not None:
            return obj.id in user_blocked_ids
        kinde_id = self.context.get('kinde_user_id')
        if kinde_id:
            try:
                return Block.objects.filter(blocker__kinde_user_id=kinde_id, blocked=obj).exists()
            except Student.DoesNotExist:
                return False
        return False
    
    def get_mutual_friends(self, obj):
        """Get mutual friends between requesting user and this student"""
        mutual_friends_data = self.context.get('mutual_friends', {})
        mutual_data = mutual_friends_data.get(obj.id, [])
        
        if not mutual_data:
            return {
                'count': 0,
                'sample_friend': None,
                'has_more': False
            }
        
        # Return first mutual friend as sample and count
        return {
            'count': len(mutual_data),
            'sample_friend': mutual_data[0] if mutual_data else None,
            'has_more': len(mutual_data) > 1
        }
    
    

class StudentProfileUpdateSerializer(serializers.ModelSerializer):
    # These are custom fields for FKs and M2M, which are also part of the form data
    university_id = serializers.PrimaryKeyRelatedField(queryset=University.objects.all(), source='university')
    student_location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), source='student_location', allow_null=True, required=False)
    student_interest_ids = serializers.PrimaryKeyRelatedField(queryset=Interests.objects.all(), many=True, source='student_interest')
    course_id = serializers.PrimaryKeyRelatedField(queryset=Courses.objects.all(), source='course', allow_null=True, required=False)

    class Meta:
        model = Student
        # The fields list is your contract
        fields = [
            'name',
            'bio',
            'profile_image',
            'university_id',
            'student_location_id',
            'student_interest_ids',
            'course_id',
        ]



class FriendshipSerializer(serializers.ModelSerializer):
    friend = serializers.SerializerMethodField()

    class Meta:
        model = Friendship
        fields = ['friend']

    
    def get_friend(self, obj):
        student_id = self.context.get('student_id')
        kinde_user_id = self.context.get('kinde_user_id')
        mutual_friends = self.context.get('mutual_friends', {})
        request = self.context.get('request')
        user_muted_student_ids = self.context.get('user_muted_student_ids')
        user_blocked_student_ids = self.context.get('user_blocked_student_ids')

        # Determine which student to show (the "other" person in the friendship)
        if obj.sender.id == student_id:
            friend = obj.receiver
        else:
            friend = obj.sender

        # Pass along the full context to StudentSerializer (including N+1-avoidance keys)
        context = {
            'student_id': student_id,
            'kinde_user_id': kinde_user_id,
            'mutual_friends': mutual_friends,
            'request': request,
        }
        if user_muted_student_ids is not None:
            context['user_muted_student_ids'] = user_muted_student_ids
        if user_blocked_student_ids is not None:
            context['user_blocked_student_ids'] = user_blocked_student_ids
        return StudentSerializer(friend, context=context).data



class CommunityNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Communities
        fields = '__all__'

class CommunityMentionSerializer(serializers.ModelSerializer):
    """Simple serializer for community mentions - only returns id and community_tag"""
    class Meta:
        model = Communities
        fields = ['id', 'community_tag']


class CommunitySerializer(serializers.ModelSerializer):
    interests = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    is_member = serializers.SerializerMethodField()
    is_muted = serializers.SerializerMethodField()
    is_blocked_by_community = serializers.SerializerMethodField()
    friends_in_community = serializers.SerializerMethodField()
    user_role = serializers.SerializerMethodField()
    
    class Meta:
        model = Communities
        fields = '__all__'

    def get_interests(self, obj):
        # Use prefetched data - no additional query
        interests = obj.community_interest.all()
        return InterestSerializer(interests, many=True).data
    
    def get_location(self, obj):
        # Use select_related data - no additional query
        return LocationSerializer(obj.location).data if obj.location else None
    
    def get_is_member(self, obj):
        # Use bulk-fetched data from context - no additional query
        user_memberships = self.context.get('user_memberships', set())
        return obj.id in user_memberships
    
    def get_is_muted(self, obj):
        # Use bulk-fetched data from context - no additional query
        user_muted_communities = self.context.get('user_muted_communities', set())
        return obj.id in user_muted_communities
    
    def get_is_blocked_by_community(self, obj):
        # Use bulk-fetched data from context - no additional query
        user_blocked_by_communities = self.context.get('user_blocked_by_communities', set())
        return obj.id in user_blocked_by_communities
    
    def get_user_role(self, obj):
        """
        Returns the user's role in the community: 'admin', 'secondary_admin', 'member', or None
        Uses bulk-fetched data from context to avoid additional queries
        """
        user_community_roles = self.context.get('user_community_roles', {})
        return user_community_roles.get(obj.id, None)
    
    def get_friends_in_community(self, obj):
        # Get friends in community from context
        friends_in_community = self.context.get('friends_in_community', {})
        friends_data = friends_in_community.get(obj.id, [])
        
        if not friends_data:
            return {
                'count': 0,
                'sample_friend': None,
                'has_more': False
            }
        
        # Return first friend as sample and count
        return {
            'count': len(friends_data),
            'sample_friend': friends_data[0] if friends_data else None,
            'has_more': len(friends_data) > 1
        }



class PostCommentSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    student_id = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_picture = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    # The 'replies' field uses the RecursiveCommentSerializer
    # 'replies' is the related_name defined on the `parent` ForeignKey in your PostComment model
    replies = RecursiveCommentSerializer(many=True, read_only=True)
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)

    class Meta:
        model = PostComment
        fields = ['id', 'comment', 'student_username', 'student_id', 'student_name', 'student_picture', 'commented_at', 'parent', 'replies', 'student_mentions', 'community_mentions', 'is_mine']

    def get_student_id(self, obj):
        """Return the student's primary key"""
        if obj.student:
            return obj.student.id
        return None
    
    def get_student_name(self, obj):
        """Return the student's username"""
        if obj.student:
            return obj.student.username
        return None
    
    def get_student_picture(self, obj):
        """Return the student's profile picture URL"""
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None

    def get_is_mine(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        return bool(kinde_user_id and obj.student and obj.student.kinde_user_id == kinde_user_id)

class PostImagesSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    class Meta:
        model = PostImages
        fields = ['id','image_url']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

class PostVideosSerializer(serializers.ModelSerializer):
    video_url = serializers.SerializerMethodField()
    class Meta:
        model = PostVideos
        fields = ['id','video_url']

    def get_video_url(self, obj):
        request = self.context.get('request')
        if obj.video:
            if request:
                return request.build_absolute_uri(obj.video.url)
            return obj.video.url
        return None


#n+1 potential problem here if not careful
class PostSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    # These now expose the annotated values
    comment_count = serializers.SerializerMethodField() 
    like_count = serializers.SerializerMethodField()
    student_profile_picture = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    
    is_liked = serializers.SerializerMethodField()
    isBookmarked = serializers.SerializerMethodField()
    
    # New: The calculated final score
    final_score = serializers.FloatField(read_only=True) 
    # New: The individual score components (useful for debugging/analysis)
    popularity_score = serializers.FloatField(read_only=True)
    interest_match_score = serializers.FloatField(read_only=True)
    friend_activity_score = serializers.FloatField(read_only=True)
    location_score = serializers.FloatField(read_only=True)
    media = serializers.SerializerMethodField()  # Combined media in upload order
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    
    def get_media(self, obj):
        """Combine images and videos in chronological order - optimized for performance"""
        from django.utils import timezone
        request = self.context.get('request')
        
        # Django automatically uses prefetched data when available
        # Accessing .all() on a prefetched relationship uses cached data
        images = obj.images.all()
        videos = obj.videos.all()
        
        # Early return if no media
        if not images and not videos:
            return []
        
        # Build media items with optimized timestamp conversion
        media_items = []
        OLD_TIMESTAMP = 0.0
        
        # Helper function to get sort key from datetime
        def get_sort_key(dt):
            if dt is None:
                return OLD_TIMESTAMP
            try:
                if timezone.is_aware(dt):
                    return dt.timestamp()
                else:
                    return timezone.make_aware(dt).timestamp()
            except (OSError, OverflowError, ValueError):
                return OLD_TIMESTAMP
        
        # Helper function to build URL
        def build_url(file_field):
            if not file_field:
                return None
            if request:
                return request.build_absolute_uri(file_field.url)
            return file_field.url
        
        # Process images
        for img in images:
            media_items.append((
                get_sort_key(img.created_at),
                'image',
                {
                    'id': img.id,
                    'type': 'image',
                    'url': build_url(img.image)
                }
            ))
        
        # Process videos
        for vid in videos:
            media_items.append((
                get_sort_key(vid.created_at),
                'video',
                {
                    'id': vid.id,
                    'type': 'video',
                    'url': build_url(vid.video)
                }
            ))
        
        # Sort by timestamp and return
        media_items.sort(key=lambda x: x[0])
        return [item[2] for item in media_items]


    # For nested comments

    class Meta:
        model = Posts
        fields = '__all__' 
        # Or explicitly list all fields: ['id', 'context_text', 'post_date', 'image', 'student', 'student_username',
        # 'comment_count', 'like_count', 'is_liked', 'isBookmarked', 'final_score', 'popularity_score',
        # 'interest_match_score', 'friend_activity_score', 'location_score', 'comments']


    def get_is_liked(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id:
            # Check if 'likes' was prefetched, if so, use prefetched data
            if hasattr(obj, '_prefetched_objects_cache') and 'likes' in obj._prefetched_objects_cache:
                # OPTIMIZATION: Use set lookup instead of iteration - O(1) vs O(n)
                liked_kinde_ids = {like.student.kinde_user_id for like in obj.likes.all() if hasattr(like, 'student') and like.student}
                return kinde_user_id in liked_kinde_ids
            # Fallback for direct query if not prefetched (less efficient, but won't crash sync context)
            try:
                return obj.likes.filter(student__kinde_user_id=kinde_user_id).exists()
            except Student.DoesNotExist:
                return False
        return False
    
    def get_isBookmarked(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id:
            if hasattr(obj, '_prefetched_objects_cache') and 'bookmarkedposts_set' in obj._prefetched_objects_cache:
                # OPTIMIZATION: Use set lookup instead of iteration - O(1) vs O(n)
                bookmarked_kinde_ids = {bookmark.student.kinde_user_id for bookmark in obj.bookmarkedposts_set.all() if hasattr(bookmark, 'student') and bookmark.student}
                return kinde_user_id in bookmarked_kinde_ids
            try:
                return obj.bookmarkedposts_set.filter(student__kinde_user_id=kinde_user_id).exists()
            except Student.DoesNotExist: 
                pass
        return False
    
    def get_student_profile_picture(self, obj):
        """
        Return the profile picture URL or None if no picture exists
        """
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None

    def get_is_mine(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        return bool(kinde_user_id and obj.student and obj.student.kinde_user_id == kinde_user_id)
    

    def get_comment_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'comments' in obj._prefetched_objects_cache:
            return len(obj.comments.all())
        return PostComment.objects.filter(post=obj).count()

    def get_like_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'likes' in obj._prefetched_objects_cache:
            return len(obj.likes.all())
        return PostLike.objects.filter(post=obj).count()



    
class PostNameSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    student_profile_picture = serializers.SerializerMethodField()
    # These now expose the annotated values
    comment_count = serializers.IntegerField(read_only=True) 
    like_count = serializers.IntegerField(read_only=True)
    # New: The calculated final score
    final_score = serializers.FloatField(read_only=True) 
    # New: The individual score components (useful for debugging/analysis)
    popularity_score = serializers.FloatField(read_only=True)
    interest_match_score = serializers.FloatField(read_only=True)
    friend_activity_score = serializers.FloatField(read_only=True)
    location_score = serializers.FloatField(read_only=True)
    media = serializers.SerializerMethodField()  # Combined media in upload order
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)

    def get_media(self, obj):
        """Combine images and videos in chronological order - optimized for performance"""
        from django.utils import timezone
        request = self.context.get('request')
        
        # Django automatically uses prefetched data when available
        # Accessing .all() on a prefetched relationship uses cached data
        images = obj.images.all()
        videos = obj.videos.all()
        
        # Early return if no media
        if not images and not videos:
            return []
        
        # Build media items with optimized timestamp conversion
        media_items = []
        OLD_TIMESTAMP = 0.0
        
        # Helper function to get sort key from datetime
        def get_sort_key(dt):
            if dt is None:
                return OLD_TIMESTAMP
            try:
                if timezone.is_aware(dt):
                    return dt.timestamp()
                else:
                    return timezone.make_aware(dt).timestamp()
            except (OSError, OverflowError, ValueError):
                return OLD_TIMESTAMP
        
        # Helper function to build URL
        def build_url(file_field):
            if not file_field:
                return None
            if request:
                return request.build_absolute_uri(file_field.url)
            return file_field.url
        
        # Process images
        for img in images:
            media_items.append((
                get_sort_key(img.created_at),
                'image',
                {
                    'id': img.id,
                    'type': 'image',
                    'url': build_url(img.image)
                }
            ))
        
        # Process videos
        for vid in videos:
            media_items.append((
                get_sort_key(vid.created_at),
                'video',
                {
                    'id': vid.id,
                    'type': 'video',
                    'url': build_url(vid.video)
                }
            ))
        
        # Sort by timestamp and return
        media_items.sort(key=lambda x: x[0])
        return [item[2] for item in media_items]

    class Meta:
        model = Posts
        fields = '__all__'
    
    def get_student_profile_picture(self, obj):
        """
        Return the profile picture URL or None if no picture exists
        """
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None
    



    


class BlockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Block
        fields = '__all__'



class CommunityPostCommentSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    student_id = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_picture = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    is_my_community = serializers.SerializerMethodField()
    replies = RecursiveCommentSerializer(many=True, read_only=True) # Recursive field
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)

    class Meta:
        model = Community_Posts_Comment
        fields = ['id', 'community_post', 'comment_text', 'student', 'student_username', 'student_id', 'student_name', 'student_picture', 'sent_at', 'parent', 'replies', 'student_mentions', 'community_mentions', 'is_mine', 'is_my_community']
        # You can add 'student' in read_only_fields in a create/update serializer if you prefer

    def get_student_id(self, obj):
        """Return the student's primary key"""
        if obj.student:
            return obj.student.id
        return None
    
    def get_student_name(self, obj):
        """Return the student's username"""
        if obj.student:
            return obj.student.username
        return None
    
    def get_student_picture(self, obj):
        """Return the student's profile picture URL"""
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None

    def get_is_mine(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        return bool(kinde_user_id and obj.student and obj.student.kinde_user_id == kinde_user_id)


    def get_is_my_community(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if not kinde_user_id:
            return False
        return Membership.objects.filter(
            user__kinde_user_id=kinde_user_id,
            community=obj.community_post.community,
            role__in=['admin','secondary_admin']
        ).exists()
  






class CommunityEventDiscussionSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    student_id = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_picture = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    replies = RecursiveCommentSerializer(many=True, read_only=True) # Recursive field
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    is_my_community = serializers.SerializerMethodField()

    class Meta:
        model = Community_Events_Discussion
        fields = ['id', 'community_event', 'discussion_text', 'student', 'student_username', 'student_id', 'student_name', 'student_picture', 'sent_at', 'parent', 'replies', 'student_mentions', 'community_mentions', 'is_mine', 'is_my_community']

    def get_student_id(self, obj):
        """Return the student's primary key"""
        if obj.student:
            return obj.student.id
        return None
    
    def get_student_name(self, obj):
        """Return the student's username"""
        if obj.student:
            return obj.student.username
        return None
    
    def get_student_picture(self, obj):
        """Return the student's profile picture URL"""
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None

    def get_is_mine(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        return bool(kinde_user_id and obj.student and obj.student.kinde_user_id == kinde_user_id)
    
    def get_is_my_community(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if not kinde_user_id:
            return False
        return Membership.objects.filter(
            user__kinde_user_id=kinde_user_id,
            community=obj.community_event.community,
            role__in=['admin','secondary_admin']
        ).exists()


class StudentEventDiscussionSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    student_id = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_picture = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    replies = RecursiveCommentSerializer(many=True, read_only=True) # Recursive field
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)

    class Meta:
        model = Student_Events_Discussion
        fields = ['id', 'student_event', 'discussion_text', 'student', 'student_username', 'student_id', 'student_name', 'student_picture', 'sent_at', 'parent', 'replies', 'student_mentions', 'community_mentions', 'is_mine']
    
    def get_student_id(self, obj):
        """Return the student's primary key"""
        if obj.student:
            return obj.student.id
        return None
    
    def get_student_name(self, obj):
        """Return the student's username"""
        if obj.student:
            return obj.student.username
        return None
    
    def get_student_picture(self, obj):
        """Return the student's profile picture URL"""
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None
    
    def get_is_mine(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        return bool(kinde_user_id and obj.student and obj.student.kinde_user_id == kinde_user_id)





class BookmarkedStudentEventSerializer(serializers.ModelSerializer):
    student_username = serializers.SerializerMethodField()

    class Meta:
        model = BookmarkedStudentEvents
        fields = '__all__'

    def get_student_username(self, obj):
        return obj.student.username
    

class BookmarkedCommunityEventSerializer(serializers.ModelSerializer):
    community_name = serializers.SerializerMethodField()

    class Meta:
        model = BookmarkedCommunityEvents
        fields = '__all__'

    def get_community_name(self, obj):
        return obj.community_event.community.community_name
    
class BookmarkedPostSerializer(serializers.ModelSerializer):
    student_username = serializers.SerializerMethodField()

    class Meta:
        model = BookmarkedPosts
        fields = '__all__'

    def get_student_username(self, obj):
        return obj.student.username
    
class BookmarkedCommunityPostSerializer(serializers.ModelSerializer):
    community_name = serializers.SerializerMethodField()

    class Meta:
        model = BookmarkedCommunityPosts
        fields = '__all__'

    def get_community_name(self, obj):
        return obj.community.community_name
    





class CommunityPostImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    class Meta:
        model = Community_Posts_Image
        fields = ['id', 'image_url']  # Include any other fields you need, like 'caption' if it exists


    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

class CommunityPostVideoSerializer(serializers.ModelSerializer):
    video_url = serializers.SerializerMethodField()
    class Meta:
        model = Community_Posts_Video
        fields = ['id', 'video_url']

    def get_video_url(self, obj):
        request = self.context.get('request')
        if obj.video:
            if request:
                return request.build_absolute_uri(obj.video.url)
            return obj.video.url
        return None

#n+1 potential problem here if not careful
class CommunityPostSerializer(serializers.ModelSerializer):
    # Standard fields
    community_name = serializers.CharField(source='community.community_name', read_only=True)
    poster_username = serializers.CharField(source='poster.username', read_only=True) # Assuming 'poster' is the student who posted
    community_image = serializers.SerializerMethodField()
    
    # Annotated counts
    comment_count = serializers.SerializerMethodField()
    like_count = serializers.SerializerMethodField()
    
    # User-specific interaction flags
    is_liked = serializers.SerializerMethodField()
    isBookmarked = serializers.SerializerMethodField() # Assuming BookmarkedCommunityPosts model exists
    is_my_community = serializers.SerializerMethodField()

    # New scoring fields
    final_score = serializers.FloatField(read_only=True)
    popularity_score = serializers.FloatField(read_only=True)
    interest_match_score = serializers.FloatField(read_only=True)
    friend_activity_score = serializers.FloatField(read_only=True)
    location_score = serializers.FloatField(read_only=True)
    media = serializers.SerializerMethodField()  # Combined media in upload order
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    
    def get_media(self, obj):
        """Combine images and videos in chronological order - optimized for performance"""
        from django.utils import timezone
        request = self.context.get('request')
        
        # Django automatically uses prefetched data when available
        # Accessing .all() on a prefetched relationship uses cached data
        images = obj.images.all()
        videos = obj.videos.all()
        
        # Early return if no media
        if not images and not videos:
            return []
        
        # Build media items with optimized timestamp conversion
        media_items = []
        OLD_TIMESTAMP = 0.0
        
        # Helper function to get sort key from datetime
        def get_sort_key(dt):
            if dt is None:
                return OLD_TIMESTAMP
            try:
                if timezone.is_aware(dt):
                    return dt.timestamp()
                else:
                    return timezone.make_aware(dt).timestamp()
            except (OSError, OverflowError, ValueError):
                return OLD_TIMESTAMP
        
        # Helper function to build URL
        def build_url(file_field):
            if not file_field:
                return None
            if request:
                return request.build_absolute_uri(file_field.url)
            return file_field.url
        
        # Process images
        for img in images:
            media_items.append((
                get_sort_key(img.created_at),
                'image',
                {
                    'id': img.id,
                    'type': 'image',
                    'url': build_url(img.image)
                }
            ))
        
        # Process videos
        for vid in videos:
            media_items.append((
                get_sort_key(vid.created_at),
                'video',
                {
                    'id': vid.id,
                    'type': 'video',
                    'url': build_url(vid.video)
                }
            ))
        
        # Sort by timestamp and return
        media_items.sort(key=lambda x: x[0])
        return [item[2] for item in media_items]


    # Nested comments


    class Meta:
        model = Community_Posts
        fields = '__all__' # Or explicitly list all fields: ['id', 'post_text', 'post_date', 'post_time', 'community', 'community_name', 'poster', 'poster_username', 'comment_count', 'like_count', 'is_liked', 'isBookmarked', 'final_score', 'popularity_score', 'interest_match_score', 'friend_activity_score', 'location_score', 'comments']

    def get_is_liked(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id:
            # OPTIMIZATION: Use set lookup instead of iteration - O(1) vs O(n)
            if hasattr(obj, '_prefetched_objects_cache') and 'likecommunitypost_set' in obj._prefetched_objects_cache:
                liked_kinde_ids = {like.student.kinde_user_id for like in obj.likecommunitypost_set.all() if hasattr(like, 'student') and like.student}
                return kinde_user_id in liked_kinde_ids
            # Fallback for direct query if not prefetched (less efficient)
            try:
                return obj.likecommunitypost_set.filter(student__kinde_user_id=kinde_user_id).exists()
            except Student.DoesNotExist:
                return False
        return False
    
    def get_isBookmarked(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id:
            if hasattr(obj, '_prefetched_objects_cache') and 'bookmarkedcommunityposts_set' in obj._prefetched_objects_cache:
                # OPTIMIZATION: Use set lookup instead of iteration - O(1) vs O(n)
                bookmarked_kinde_ids = {bookmark.student.kinde_user_id for bookmark in obj.bookmarkedcommunityposts_set.all() if hasattr(bookmark, 'student') and bookmark.student}
                return kinde_user_id in bookmarked_kinde_ids
            try:
                return BookmarkedCommunityPosts.objects.filter(
                    community_post=obj, 
                    student__kinde_user_id=kinde_user_id
                ).exists()
            except Student.DoesNotExist:
                return False
        return False
    
    def get_comment_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'community_posts_comment_set' in obj._prefetched_objects_cache:
            return len(obj.community_posts_comment_set.all())
        return Community_Posts_Comment.objects.filter(community_post=obj).count()

    def get_like_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'likecommunitypost_set' in obj._prefetched_objects_cache:
            return len(obj.likecommunitypost_set.all())
        return LikeCommunityPost.objects.filter(event=obj).count()



    def get_is_my_community(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if not kinde_user_id:
            return False
        return Membership.objects.filter(
            user__kinde_user_id=kinde_user_id,
            community=obj.community,
            role__in=['admin','secondary_admin']
        ).exists()
    
    def get_community_image(self, obj):
        """
        Return the community image URL or None if no image exists
        """
        if obj.community and obj.community.community_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.community.community_image.url)
            return obj.community.community_image.url
        return None


    
class CommunityPostNameSerializer(serializers.ModelSerializer):
    community_name = serializers.CharField(source='community.community_name', read_only=True)
    community_image = serializers.SerializerMethodField()
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    class Meta:
        model = Community_Posts
        fields = '__all__'
    
    def get_community_image(self, obj):
        """
        Return the community image URL or None if no image exists
        """
        if obj.community and obj.community.community_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.community.community_image.url)
            return obj.community.community_image.url
        return None




class StudentEventImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    class Meta:
        model = Student_Events_Image
        fields = ['id', 'image_url']  # Include any other fields you need, like 'caption' if it exists

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

class StudentEventVideoSerializer(serializers.ModelSerializer):
    video_url = serializers.SerializerMethodField()
    class Meta:
        model = Student_Events_Video
        fields = ['id', 'video_url']

    def get_video_url(self, obj):
        request = self.context.get('request')
        if obj.video:
            if request:
                return request.build_absolute_uri(obj.video.url)
            return obj.video.url
        return None

# --- NEW/UPDATED: StudentEventSerializer ---
#n+1 potential problem here if not careful
class StudentEventSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    student_profile_picture = serializers.SerializerMethodField()
    
    # Annotated counts for RSVPs and discussions
    rsvp_count = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()

    # User-specific interaction flags
    is_rsvpd = serializers.SerializerMethodField() # Check if current user has RSVP'd
    isBookmarked = serializers.SerializerMethodField() # Assuming BookmarkedStudentEvents model exists
    is_mine = serializers.SerializerMethodField()

    # New scoring fields
    final_score = serializers.FloatField(read_only=True)
    popularity_score = serializers.FloatField(read_only=True)
    interest_match_score = serializers.FloatField(read_only=True)
    friend_activity_score = serializers.FloatField(read_only=True)
    location_score = serializers.FloatField(read_only=True)
    media = serializers.SerializerMethodField()  # Combined media in upload order
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    
    def get_media(self, obj):
        """Combine images and videos in chronological order - optimized for performance"""
        from django.utils import timezone
        request = self.context.get('request')
        
        # Django automatically uses prefetched data when available
        # Accessing .all() on a prefetched relationship uses cached data
        images = obj.images.all()
        videos = obj.videos.all()
        
        # Early return if no media
        if not images and not videos:
            return []
        
        # Build media items with optimized timestamp conversion
        media_items = []
        OLD_TIMESTAMP = 0.0
        
        # Helper function to get sort key from datetime
        def get_sort_key(dt):
            if dt is None:
                return OLD_TIMESTAMP
            try:
                if timezone.is_aware(dt):
                    return dt.timestamp()
                else:
                    return timezone.make_aware(dt).timestamp()
            except (OSError, OverflowError, ValueError):
                return OLD_TIMESTAMP
        
        # Helper function to build URL
        def build_url(file_field):
            if not file_field:
                return None
            if request:
                return request.build_absolute_uri(file_field.url)
            return file_field.url
        
        # Process images
        for img in images:
            media_items.append((
                get_sort_key(img.created_at),
                'image',
                {
                    'id': img.id,
                    'type': 'image',
                    'url': build_url(img.image)
                }
            ))
        
        # Process videos
        for vid in videos:
            media_items.append((
                get_sort_key(vid.created_at),
                'video',
                {
                    'id': vid.id,
                    'type': 'video',
                    'url': build_url(vid.video)
                }
            ))
        
        # Sort by timestamp and return
        media_items.sort(key=lambda x: x[0])
        return [item[2] for item in media_items]


    # Nested discussions

    class Meta:
        model = Student_Events
        fields = '__all__' # Or explicitly list all fields

    def get_is_rsvpd(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id and hasattr(obj, '_prefetched_objects_cache') and 'eventrsvp' in obj._prefetched_objects_cache:
            return any(rsvp.student.kinde_user_id == kinde_user_id for rsvp in obj.eventrsvp.all())
        elif kinde_user_id:
            try:
                return EventRSVP.objects.filter(event=obj, student__kinde_user_id=kinde_user_id).exists()
            except Student.DoesNotExist:
                return False
        return False

    def get_isBookmarked(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id:
            if hasattr(obj, '_prefetched_objects_cache') and 'bookmarkedstudentevents_set' in obj._prefetched_objects_cache:
                # OPTIMIZATION: Use set lookup instead of iteration - O(1) vs O(n)
                bookmarked_kinde_ids = {bookmark.student.kinde_user_id for bookmark in obj.bookmarkedstudentevents_set.all() if hasattr(bookmark, 'student') and bookmark.student}
                return kinde_user_id in bookmarked_kinde_ids
            try:
                return BookmarkedStudentEvents.objects.filter(
                    student_event=obj, # Note: your model uses 'studentevent' as FK to Student_Events
                    student__kinde_user_id=kinde_user_id
                ).exists()
            except Student.DoesNotExist:
                return False
        return False
    
    def get_comment_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'student_events_discussion_set' in obj._prefetched_objects_cache:
            return len(obj.student_events_discussion_set.all())
        return Student_Events_Discussion.objects.filter(student_event=obj).count()

    def get_rsvp_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'eventrsvp' in obj._prefetched_objects_cache:
            return len(obj.eventrsvp.all())
        return EventRSVP.objects.filter(event=obj).count()

    def get_is_mine(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        return bool(kinde_user_id and obj.student and obj.student.kinde_user_id == kinde_user_id)
    
    def get_student_profile_picture(self, obj):
        """
        Return the profile picture URL or None if no picture exists
        """
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None

class StudentEventNameSerializer(serializers.ModelSerializer):
    student_username = serializers.CharField(source='student.username', read_only=True)
    student_profile_picture = serializers.SerializerMethodField()
    
    # Annotated counts for RSVPs and discussions
    rsvp_count = serializers.IntegerField(read_only=True)
    interested_count = serializers.IntegerField(read_only=True)
    discussion_count = serializers.IntegerField(read_only=True)
    # New scoring fields
    final_score = serializers.FloatField(read_only=True)
    popularity_score = serializers.FloatField(read_only=True)
    interest_match_score = serializers.FloatField(read_only=True)
    friend_activity_score = serializers.FloatField(read_only=True)
    location_score = serializers.FloatField(read_only=True)
    media = serializers.SerializerMethodField()  # Combined media in upload order
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    
    def get_media(self, obj):
        """Combine images and videos in chronological order - optimized for performance"""
        from django.utils import timezone
        request = self.context.get('request')
        
        # Django automatically uses prefetched data when available
        # Accessing .all() on a prefetched relationship uses cached data
        images = obj.images.all()
        videos = obj.videos.all()
        
        # Early return if no media
        if not images and not videos:
            return []
        
        # Build media items with optimized timestamp conversion
        media_items = []
        OLD_TIMESTAMP = 0.0
        
        # Helper function to get sort key from datetime
        def get_sort_key(dt):
            if dt is None:
                return OLD_TIMESTAMP
            try:
                if timezone.is_aware(dt):
                    return dt.timestamp()
                else:
                    return timezone.make_aware(dt).timestamp()
            except (OSError, OverflowError, ValueError):
                return OLD_TIMESTAMP
        
        # Helper function to build URL
        def build_url(file_field):
            if not file_field:
                return None
            if request:
                return request.build_absolute_uri(file_field.url)
            return file_field.url
        
        # Process images
        for img in images:
            media_items.append((
                get_sort_key(img.created_at),
                'image',
                {
                    'id': img.id,
                    'type': 'image',
                    'url': build_url(img.image)
                }
            ))
        
        # Process videos
        for vid in videos:
            media_items.append((
                get_sort_key(vid.created_at),
                'video',
                {
                    'id': vid.id,
                    'type': 'video',
                    'url': build_url(vid.video)
                }
            ))
        
        # Sort by timestamp and return
        media_items.sort(key=lambda x: x[0])
        return [item[2] for item in media_items]
    
    def get_student_profile_picture(self, obj):
        """
        Return the profile picture URL or None if no picture exists
        """
        if obj.student and obj.student.profile_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.student.profile_image.url)
            return obj.student.profile_image.url
        return None


    class Meta:
        model = Student_Events
        fields = '__all__'
    

class CommunityEventImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    class Meta:
        model = Community_Events_Image
        fields = ['id', 'image_url']  # Include any other fields you need, like 'caption' if it exists

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

class CommunityEventVideoSerializer(serializers.ModelSerializer):
    video_url = serializers.SerializerMethodField()
    class Meta:
        model = Community_Events_Video
        fields = ['id', 'video_url']

    def get_video_url(self, obj):
        request = self.context.get('request')
        if obj.video:
            if request:
                return request.build_absolute_uri(obj.video.url)
            return obj.video.url
        return None


# --- NEW/UPDATED: CommunityEventsSerializer ---
class CommunityEventsSerializer(serializers.ModelSerializer):
    community_name = serializers.CharField(source='community.community_name', read_only=True)
    poster_username = serializers.CharField(source='poster.username', read_only=True) # Assuming 'poster' is the student who posted
    community_image = serializers.SerializerMethodField()

    # Annotated counts for RSVPs and discussions
    rsvp_count = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()

    # User-specific interaction flags
    is_rsvpd = serializers.SerializerMethodField()
    isBookmarked = serializers.SerializerMethodField() # Assuming BookmarkedCommunityEvents model exists
    is_my_community = serializers.SerializerMethodField()

    # New scoring fields
    final_score = serializers.FloatField(read_only=True)
    popularity_score = serializers.FloatField(read_only=True)
    interest_match_score = serializers.FloatField(read_only=True)
    friend_activity_score = serializers.FloatField(read_only=True)
    location_score = serializers.FloatField(read_only=True)
    media = serializers.SerializerMethodField()  # Combined media in upload order
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    
    def get_media(self, obj):
        """Combine images and videos in chronological order - optimized for performance"""
        from django.utils import timezone
        request = self.context.get('request')
        
        # Django automatically uses prefetched data when available
        # Accessing .all() on a prefetched relationship uses cached data
        images = obj.images.all()
        videos = obj.videos.all()
        
        # Early return if no media
        if not images and not videos:
            return []
        
        # Build media items with optimized timestamp conversion
        media_items = []
        OLD_TIMESTAMP = 0.0
        
        # Helper function to get sort key from datetime
        def get_sort_key(dt):
            if dt is None:
                return OLD_TIMESTAMP
            try:
                if timezone.is_aware(dt):
                    return dt.timestamp()
                else:
                    return timezone.make_aware(dt).timestamp()
            except (OSError, OverflowError, ValueError):
                return OLD_TIMESTAMP
        
        # Helper function to build URL
        def build_url(file_field):
            if not file_field:
                return None
            if request:
                return request.build_absolute_uri(file_field.url)
            return file_field.url
        
        # Process images
        for img in images:
            media_items.append((
                get_sort_key(img.created_at),
                'image',
                {
                    'id': img.id,
                    'type': 'image',
                    'url': build_url(img.image)
                }
            ))
        
        # Process videos
        for vid in videos:
            media_items.append((
                get_sort_key(vid.created_at),
                'video',
                {
                    'id': vid.id,
                    'type': 'video',
                    'url': build_url(vid.video)
                }
            ))
        
        # Sort by timestamp and return
        media_items.sort(key=lambda x: x[0])
        return [item[2] for item in media_items]

    # Nested discussions

    class Meta:
        model = Community_Events
        fields = '__all__' # Or explicitly list all fields

    def get_is_rsvpd(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id and hasattr(obj, '_prefetched_objects_cache') and 'communityeventrsvp' in obj._prefetched_objects_cache:
            return any(rsvp.student.kinde_user_id == kinde_user_id for rsvp in obj.communityeventrsvp.all())
        elif kinde_user_id:
            try:
                return CommunityEventRSVP.objects.filter(event=obj, student__kinde_user_id=kinde_user_id).exists()
            except Student.DoesNotExist:
                return False
        return False

    def get_isBookmarked(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if kinde_user_id:
            if hasattr(obj, '_prefetched_objects_cache') and 'bookmarkedcommunityevents_set' in obj._prefetched_objects_cache:
                # OPTIMIZATION: Use set lookup instead of iteration - O(1) vs O(n)
                bookmarked_kinde_ids = {bookmark.student.kinde_user_id for bookmark in obj.bookmarkedcommunityevents_set.all() if hasattr(bookmark, 'student') and bookmark.student}
                return kinde_user_id in bookmarked_kinde_ids
            try:
                return BookmarkedCommunityEvents.objects.filter(
                    community_event=obj, # Note: your model uses 'community_event' as FK to Community_Events
                    student__kinde_user_id=kinde_user_id
                ).exists()
            except Student.DoesNotExist:
                return False
        return False
    
    def get_comment_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'community_events_discussion_set' in obj._prefetched_objects_cache:
            return len(obj.community_events_discussion_set.all())
        return Community_Events_Discussion.objects.filter(community_event=obj).count()

    def get_rsvp_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'communityeventrsvp' in obj._prefetched_objects_cache:
            return len(obj.communityeventrsvp.all())
        return CommunityEventRSVP.objects.filter(event=obj).count()


    def get_is_my_community(self, obj):
        kinde_user_id = self.context.get('kinde_user_id')
        if not kinde_user_id:
            return False
        return Membership.objects.filter(
            user__kinde_user_id=kinde_user_id,
            community=obj.community,
            role__in=['admin','secondary_admin']
        ).exists()
    
    def get_community_image(self, obj):
        """
        Return the community image URL or None if no image exists
        """
        if obj.community and obj.community.community_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.community.community_image.url)
            return obj.community.community_image.url
        return None
    
class CommunityEventsNameSerializer(serializers.ModelSerializer):
    community_name = serializers.CharField(source='community.community_name', read_only=True)
    poster_username = serializers.CharField(source='poster.username', read_only=True) # Assuming 'poster' is the student who posted
    community_image = serializers.SerializerMethodField()

    # Annotated counts for RSVPs and discussions
    rsvp_count = serializers.IntegerField(read_only=True)
    interested_count = serializers.IntegerField(read_only=True)
    not_going_count = serializers.IntegerField(read_only=True)
    discussion_count = serializers.IntegerField(read_only=True)

    # User-specific interaction flags

    # New scoring fields
    final_score = serializers.FloatField(read_only=True)
    popularity_score = serializers.FloatField(read_only=True)
    interest_match_score = serializers.FloatField(read_only=True)
    friend_activity_score = serializers.FloatField(read_only=True)
    location_score = serializers.FloatField(read_only=True)
    media = serializers.SerializerMethodField()  # Combined media in upload order
    student_mentions = StudentMentionSerializer(many=True, read_only=True)
    community_mentions = CommunityMentionSerializer(many=True, read_only=True)
    
    def get_media(self, obj):
        """Combine images and videos in chronological order - optimized for performance"""
        from django.utils import timezone
        request = self.context.get('request')
        
        # Django automatically uses prefetched data when available
        # Accessing .all() on a prefetched relationship uses cached data
        images = obj.images.all()
        videos = obj.videos.all()
        
        # Early return if no media
        if not images and not videos:
            return []
        
        # Build media items with optimized timestamp conversion
        media_items = []
        OLD_TIMESTAMP = 0.0
        
        # Helper function to get sort key from datetime
        def get_sort_key(dt):
            if dt is None:
                return OLD_TIMESTAMP
            try:
                if timezone.is_aware(dt):
                    return dt.timestamp()
                else:
                    return timezone.make_aware(dt).timestamp()
            except (OSError, OverflowError, ValueError):
                return OLD_TIMESTAMP
        
        # Helper function to build URL
        def build_url(file_field):
            if not file_field:
                return None
            if request:
                return request.build_absolute_uri(file_field.url)
            return file_field.url
        
        # Process images
        for img in images:
            media_items.append((
                get_sort_key(img.created_at),
                'image',
                {
                    'id': img.id,
                    'type': 'image',
                    'url': build_url(img.image)
                }
            ))
        
        # Process videos
        for vid in videos:
            media_items.append((
                get_sort_key(vid.created_at),
                'video',
                {
                    'id': vid.id,
                    'type': 'video',
                    'url': build_url(vid.video)
                }
            ))
        
        # Sort by timestamp and return
        media_items.sort(key=lambda x: x[0])
        return [item[2] for item in media_items]
    
    def get_community_image(self, obj):
        """
        Return the community image URL or None if no image exists
        """
        if obj.community and obj.community.community_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.community.community_image.url)
            return obj.community.community_image.url
        return None

    # Nested discussions

    class Meta:
        model = Community_Events
        fields = '__all__' # Or explicitly list all fields
    

class FriendshipSerializer(serializers.ModelSerializer):
    sender = StudentNameSerializer(read_only=True)
    class Meta:
        model = Friendship
        fields = '__all__'


class RegionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Region
        fields = ['id', 'region']

# --- NEW: Serializer for Interests ---
class InterestsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Interests
        fields = ['id', 'interest']
    

class LocationSerializer(serializers.ModelSerializer):
    region = RegionSerializer(read_only=True)  # Nested serializer for region

    class Meta:
        model = Location
        fields = '__all__'

class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ['id', 'name', 'code', 'allowed_email_domains']

class UniversitySerializer(serializers.ModelSerializer):

    class Meta:
        model = University
        fields = '__all__'

class CourseSerializer(serializers.ModelSerializer):

    class Meta:
        model = Courses
        fields = '__all__'

class NotificationSerializer(serializers.ModelSerializer):
    notificationtype = serializers.CharField(source='notificationtype.notification_type', read_only=True)
    student_event = serializers.SerializerMethodField()
    community_event = serializers.SerializerMethodField()
    post = serializers.SerializerMethodField()
    community_post = serializers.SerializerMethodField()
    sender = StudentNameSerializer(read_only=True)
    class Meta:
        model = Notification
        fields = '__all__'
    
    def get_student_event(self, obj):
        """Return student_event from notification or from student_event_discussion"""
        # If notification has student_event directly, use it
        if obj.student_event:
            return StudentEventNameSerializer(obj.student_event).data
        # If notification has student_event_discussion, get event from discussion
        elif obj.student_event_discussion and obj.student_event_discussion.student_event:
            return StudentEventNameSerializer(obj.student_event_discussion.student_event).data
        return None
    
    def get_community_event(self, obj):
        """Return community_event from notification or from community_event_discussion"""
        # If notification has community_event directly, use it
        if obj.community_event:
            return CommunityEventsNameSerializer(obj.community_event).data
        # If notification has community_event_discussion, get event from discussion
        elif obj.community_event_discussion and obj.community_event_discussion.community_event:
            return CommunityEventsNameSerializer(obj.community_event_discussion.community_event).data
        return None
    
    def get_post(self, obj):
        """Return post from notification or from post_comment"""
        # If notification has post directly, use it
        if obj.post:
            return PostNameSerializer(obj.post).data
        # If notification has post_comment, get post from comment
        elif obj.post_comment and obj.post_comment.post:
            return PostNameSerializer(obj.post_comment.post).data
        return None
    
    def get_community_post(self, obj):
        """Return community_post from notification or from community_post_comment"""
        # If notification has community_post directly, use it
        if obj.community_post:
            return CommunityPostNameSerializer(obj.community_post).data
        # If notification has community_post_comment, get post from comment
        elif obj.community_post_comment and obj.community_post_comment.community_post:
            return CommunityPostNameSerializer(obj.community_post_comment.community_post).data
        return None

class StudentChatSerializer(serializers.ModelSerializer): # A simple serializer for the sender/receiver
    class Meta:
        model = Student
        fields = ['id', 'name', 'username', 'kinde_user_id'] # Include kinde_user_id for Flutter checks


class DirectMessageParentSerializer(serializers.ModelSerializer):
    sender = StudentChatSerializer(read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = DirectMessage
        fields = ['id', 'sender', 'message', 'image_url', 'timestamp'] # Include only essential parent data
    
    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None

class DirectMessageSerializer(serializers.ModelSerializer):
    sender = StudentChatSerializer(read_only=True) # Nested serializer for sender details
    receiver = StudentChatSerializer(read_only=True) # Nested serializer for receiver details
    image_url = serializers.SerializerMethodField() # For the image URL
    reply = DirectMessageParentSerializer(read_only=True)
    post = PostNameSerializer(read_only=True)
    community_post = CommunityPostNameSerializer(read_only=True)
    student_event = StudentEventNameSerializer(read_only=True)
    community_event = CommunityEventsNameSerializer(read_only=True)
    student_profile = StudentNameSerializer(read_only=True)
    community_profile = CommunityNameSerializer(read_only=True)
    status = serializers.SerializerMethodField()
    delivered_at = serializers.SerializerMethodField()
    read_at = serializers.SerializerMethodField()

    class Meta:
        model = DirectMessage
        fields = ['id', 'sender', 'receiver', 'message', 'image_url', 'timestamp', 'is_read', 'status', 'delivered_at', 'read_at', 'reply', 'post', 'community_post', 'student_event', 'community_event', 'student_profile', 'community_profile']

    def get_status(self, obj):
        # Frontend: "sending"|"sent"|"delivered"|"read"|"failed"
        if obj.is_read and getattr(obj, 'read_at', None):
            return 'read'
        if getattr(obj, 'delivered_at', None):
            return 'delivered'
        return 'sent'

    def get_delivered_at(self, obj):
        at = getattr(obj, 'delivered_at', None)
        return at.isoformat() if at else None

    def get_read_at(self, obj):
        at = getattr(obj, 'read_at', None)
        return at.isoformat() if at else None

    def get_image_url(self, obj):
        request = self.context.get('request') # Get request context if available (for full URL)
        if obj.image:
            # Build full URL if request context is available
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url # Otherwise, just return relative URL
        return None
    


class CommunityChatReplySerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.name', read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = CommunityChatMessage
        fields = ['id', 'student_name', 'message', 'image_url', 'sent_at']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None
    
class CommunityChatMessageSerializer(serializers.ModelSerializer):
    student = StudentChatSerializer(read_only=True)
    community_name = serializers.CharField(source='community.community_name', read_only=True)
    image_url = serializers.SerializerMethodField()
    reply = CommunityChatReplySerializer(read_only=True)
    post = PostNameSerializer(read_only=True)
    community_post = CommunityPostNameSerializer(read_only=True)
    student_event = StudentEventNameSerializer(read_only=True)
    community_event = CommunityEventsNameSerializer(read_only=True)
    student_profile = StudentNameSerializer(read_only=True)
    community_profile = CommunityNameSerializer(read_only=True)
    is_read_by_me = serializers.SerializerMethodField()
    

    class Meta:
        model = CommunityChatMessage
        fields = ['id', 'community', 'community_name', 'student', 'message', 'image_url', 'reply', 'sent_at', 'post', 'community_post', 'student_event', 'community_event', 'student_profile', 'community_profile', 'is_read_by_me']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None
    
    def get_is_read_by_me(self, obj):
        """Check if the current user has read this message"""
        kinde_user_id = self.context.get('kinde_user_id')
        if not kinde_user_id:
            return False
        
        try:
            # Check if the current user is in the read_by many-to-many relationship
            return obj.read_by.filter(kinde_user_id=kinde_user_id).exists()
        except Exception:
            return False

 
class GroupChatReplySerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.name', read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = GroupChatMessage
        fields = ['id', 'student_name', 'message', 'image_url', 'sent_at']

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None


class GroupChatMessageSerializer(serializers.ModelSerializer):
    student = StudentChatSerializer(read_only=True)
    group_name = serializers.CharField(source='group.name', read_only=True)
    image_url = serializers.SerializerMethodField()
    reply = GroupChatReplySerializer(read_only=True)
    post = PostNameSerializer(read_only=True)
    community_post = CommunityPostNameSerializer(read_only=True)
    student_event = StudentEventNameSerializer(read_only=True)
    community_event = CommunityEventsNameSerializer(read_only=True)
    student_profile = StudentNameSerializer(read_only=True)
    community_profile = CommunityNameSerializer(read_only=True)
    is_read_by_me = serializers.SerializerMethodField()

    class Meta:
        model = GroupChatMessage
        fields = [
            'id',
            'group',
            'group_name',
            'student',
            'message',
            'image_url',
            'reply',
            'sent_at',
            'post',
            'community_post',
            'student_event',
            'community_event',
            'student_profile',
            'community_profile',
            'is_read_by_me',
        ]

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None

    def get_is_read_by_me(self, obj):
        """Check if the current user has read this group message"""
        kinde_user_id = self.context.get('kinde_user_id')
        if not kinde_user_id:
            return False
        try:
            return obj.read_by.filter(kinde_user_id=kinde_user_id).exists()
        except Exception:
            return False


class GroupChatSerializer(serializers.ModelSerializer):
    created_by = StudentChatSerializer(read_only=True)
    image_url = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = GroupChat
        fields = [
            'id',
            'name',
            'description',
            'image_url',
            'created_at',
            'created_by',
            'is_active',
            'unread_count',
            'last_message',
        ]

    def get_image_url(self, obj):
        request = self.context.get('request')
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None

    def get_last_message(self, obj):
        """
        Serialize the last message for this group.
        We rely on a pre-attached `last_message` attribute set by the view
        to avoid doing synchronous DB work from async call sites.
        """
        last = getattr(obj, 'last_message', None)
        if not last:
            return None
        context = self.context.copy()
        return GroupChatMessageSerializer(last, context=context).data

    def get_unread_count(self, obj):
        """
        Unread count for the current user in this group.
        Prefer a pre-computed `unread_count` attribute set by the view.
        """
        precomputed = getattr(obj, 'unread_count', None)
        if precomputed is not None:
            return precomputed

        # Fallback computation (best-effort; may be overridden by views for performance)
        request = self.context.get('request')
        kinde_user_id = getattr(getattr(request, 'user', None), 'kinde_user_id', None) if request else self.context.get('kinde_user_id')
        if not kinde_user_id:
            return 0

        from .models import Student, GroupChatMessage  # Local import to avoid circulars
        try:
            student = Student.objects.get(kinde_user_id=kinde_user_id)
        except Student.DoesNotExist:
            return 0

        return (
            GroupChatMessage.objects.filter(group=obj)
            .exclude(student=student)
            .exclude(read_by=student)
            .count()
        )


class CommunityListDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Communities
        fields = ['id', 'community_name', 'community_bio'] # Or whatever subset is needed for the list

# This serializer is for the individual items in the community chat list
class CommunityChatListItemSerializer(serializers.Serializer):
    # 'community' will be the Community object
    community = CommunityListDetailSerializer(read_only=True) # Or CommunitySerializer if you need more details
    # 'last_message' will be the CommunityChatMessage object (can be null if no messages yet)
    last_message = CommunityChatMessageSerializer(read_only=True, allow_null=True)


class ConversationItemSerializer(serializers.Serializer):
    # 'other_participant' will be a Student object
    other_participant = StudentChatSerializer(read_only=True)
    # 'last_message' will be a DirectMessage object
    last_message = DirectMessageSerializer(read_only=True)

class UnifiedConversationItemSerializer(serializers.Serializer):
    # Identifiers
    conversation_type = serializers.CharField()  # 'direct_chat', 'community_chat', or 'group_chat'
    
    # Key identifier of the "other party" for navigation on frontend
    # For direct chat, this would be the other_user_kinde_id.
    # For community chat, this would be the community_id (PK).
    conversation_target_id = serializers.CharField() 
    
    # Display information for the "other party" or community
    display_name = serializers.CharField()
    display_avatar_url = serializers.CharField(allow_null=True) # Optional: if you have profile pics/community logos
    display_bio = serializers.CharField(allow_null=True) # Optional: if useful

    # Last message details
    last_message_text = serializers.CharField(allow_null=True)
    last_message_image_url = serializers.CharField(allow_null=True)
    last_message_timestamp = serializers.DateTimeField(allow_null=True)
    last_message_sender_name = serializers.CharField(allow_null=True) # Who sent the last message in this convo
    last_message_type = serializers.CharField(allow_null=True) # Type of last message: 'post', 'community post', 'student event', 'community event', 'student profile', 'community profile', or 'message'

    # Add any other common fields you need.
    is_read = serializers.BooleanField(required=False)
