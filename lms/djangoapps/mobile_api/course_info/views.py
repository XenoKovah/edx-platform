"""
Views for course info API
"""

import logging

from django.contrib.auth import get_user_model
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.djangoapps.static_replace import make_static_urls_absolute
from lms.djangoapps.certificates.api import certificate_downloadable_status
from lms.djangoapps.courseware.courses import get_course_info_section_block
from lms.djangoapps.course_goals.models import UserActivity
from lms.djangoapps.course_api.blocks.views import BlocksInCourseView
from lms.djangoapps.mobile_api.course_info.serializers import CourseInfoOverviewSerializer
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.lib.api.view_utils import view_auth_classes
from openedx.core.lib.xblock_utils import get_course_update_items
from openedx.features.course_experience import ENABLE_COURSE_GOALS
from ..decorators import mobile_course_access, mobile_view

User = get_user_model()
log = logging.getLogger(__name__)


@mobile_view()
class CourseUpdatesList(generics.ListAPIView):
    """
    **Use Case**

        Get the content for course updates.

    **Example Request**

        GET /api/mobile/v0.5/course_info/{course_id}/updates

    **Response Values**

        If the request is successful, the request returns an HTTP 200 "OK"
        response along with an array of course updates. Each course update
        contains the following values.

            * content: The content, as an HTML string, of the course update.
            * date: The date of the course update.
            * id: The unique identifier of the update.
            * status: Whether the update is visible or not.
    """

    @mobile_course_access()
    def list(self, request, course, *args, **kwargs):  # lint-amnesty, pylint: disable=arguments-differ
        course_updates_block = get_course_info_section_block(request, request.user, course, 'updates')
        update_items = get_course_update_items(course_updates_block)

        updates_to_show = [
            update for update in update_items
            if update.get("status") != "deleted"
        ]

        for item in updates_to_show:
            item['content'] = apply_wrappers_to_content(item['content'], course_updates_block, request)

        return Response(updates_to_show)


@mobile_view()
class CourseHandoutsList(generics.ListAPIView):
    """
    **Use Case**

        Get the HTML for course handouts.

    **Example Request**

        GET /api/mobile/v0.5/course_info/{course_id}/handouts

    **Response Values**

        If the request is successful, the request returns an HTTP 200 "OK"
        response along with the following value.

        * handouts_html: The HTML for course handouts.
    """

    @mobile_course_access()
    def list(self, request, course, *args, **kwargs):  # lint-amnesty, pylint: disable=arguments-differ
        course_handouts_block = get_course_info_section_block(request, request.user, course, 'handouts')
        if course_handouts_block:
            if course_handouts_block.data == "<ol></ol>":
                handouts_html = None
            else:
                handouts_html = apply_wrappers_to_content(course_handouts_block.data, course_handouts_block, request)
            return Response({'handouts_html': handouts_html})
        else:
            # course_handouts_block could be None if there are no handouts
            return Response({'handouts_html': None})


def apply_wrappers_to_content(content, block, request):
    """
    Updates a piece of html content with the filter functions stored in its module system, then replaces any
    static urls with absolute urls.

    Args:
        content: The html content to which to apply the content wrappers generated for this module system.
        block: The block containing a reference to the module system which contains functions to apply to the
        content. These functions include:
            * Replacing static url's
            * Replacing course url's
            * Replacing jump to id url's
        request: The request, used to replace static URLs with absolute URLs.

    Returns: A piece of html content containing the original content updated by each wrapper.

    """
    content = block.runtime.service(block, "replace_urls").replace_urls(content)

    return make_static_urls_absolute(request, content)


@mobile_view()
class CourseGoalsRecordUserActivity(APIView):
    """
    API that allows the mobile_apps to record activity for course goals to the user activity table
    """

    def post(self, request, *args, **kwargs):
        """
        Handle the POST request

        Populate the user activity table.
        """
        user_id = request.data.get('user_id')
        course_key = request.data.get('course_key')

        if not user_id or not course_key:
            log.error('User id and course key are required. %s %s', user_id, course_key)
            return Response(
                'User id and course key are required',
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user_id = int(user_id)
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            log.error('Provided user id does not correspond to an existing user %s', user_id)
            return Response(
                'Provided user id does not correspond to an existing user',
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            course_key = CourseKey.from_string(course_key)
        except InvalidKeyError:
            log.error('Provided course key is not valid %s', course_key)
            return Response(
                'Provided course key is not valid',
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not ENABLE_COURSE_GOALS.is_enabled(course_key):
            log.warning('For this mobile request, user activity is not enabled for this user {} and course {}'.format(
                str(user_id), str(course_key))
            )
            return Response(status=(200))

        # Populate user activity for tracking progress towards a user's course goals
        UserActivity.record_user_activity(user, course_key)
        return Response(status=(200))


@view_auth_classes(is_authenticated=False)
class BlocksInfoInCourseView(BlocksInCourseView):
    """
    **Use Case**

        This API endpoint is specifically optimized for the course homepage on Mobile Apps.
        The endpoint returns the blocks in the course according to the requesting user's access level.
        Additionally, response encompasses info fields with information about the course,
        including certificate URL, media dictionary with course image URLs, start and end dates for the course.

    **Example requests**:

        This api works with all versions {api_version}, you can use: v0.5, v1, v2 or v3

        GET /api/mobile/{api_version}/course_info/blocks/?course_id=<course_id>
        GET /api/mobile/{api_version}/course_info/blocks/?course_id=<course_id>
            &username=anjali
            &depth=all
            &requested_fields=graded,format,student_view_multi_device,lti_url
            &block_counts=video
            &student_view_data=video
            &block_types_filter=problem,html

    **Parameters:**

            username (str): The username of the specified user for whom the course data
                is being accessed.
            depth (integer, str, None): Optional number of blocks you receive in response
                course nesting depth, you can get only sections, sections and subsections,
                or provide string 'all' to receive all blocks of the course.
            requested_field (list): Optional list of names of additional fields to return for each block.
                Supported fields can be found in transformers.SUPPORTED_FIELDS.
            block_counts (list): Optional list of names of block types for which an aggregated count
                of blocks is returned.
            student_view_data (list): Optional list of names of block types for
                which student_view_data is returned.
            block_types_filter (list): Filter by block types:
                'video', 'discussion', 'html', 'chapter', 'sequential', 'vertical'.
            return_type (list, dict): Optional list or dictionary of block's fields based on 'return_type'.

    **Response example**

        Body consists of the following fields, you received this response if you use
        'return_type=dict' in query params:

        root: (str) The ID of the root node of the requested course block structure.\
        blocks: (dict) A dictionary or list, based on the value of the
            "return_type" parameter. Maps block usage IDs to a collection of
            information about each block. Each block contains the following
            fields.

        id: (str) The Course's id (Course Run key)
        name: (str) The course's name
        number: (str) The course's number
        org: (str) The course's organisation
        start: (str) Date the course begins, in ISO 8601 notation
        start_display: (str) Readably formatted start of the course
        start_type: (str) Hint describing how `start_display` is set. One of:
            * `"string"`: manually set by the course author
            * `"timestamp"`: generated from the `start` timestamp
            * `"empty"`: no start date is specified
        end: (str) Date the course ends, in ISO 8601 notation
        media: (dict) An object that contains named media items. Included here:
            * course_image: An image to show for the course.  Represented
              as an object with the following fields:
                * uri: The location of the image
        certificate: (dict) Information about the user's earned certificate in the course.
            Included here:
                * uri: The location of the user's certificate
        is_self_paced: (bool) Indicates if the course is self paced

        Body consists of the following fields, you received this response if you use
        'return_type=list' in query params:

        id: (str) The Course's id (Course Run key)
        block_id: (str) The unique identifier for the block_id
        lms_web_url: (str) The URL to the navigational container of the xBlock on the web.
        legacy_web_url: (str) Like `lms_web_url`, but always directs to
            the "Legacy" frontend experience.
        student_view_url: (str) The URL to retrieve the HTML rendering
            of this block's student view
        type: (str): The type of block. Possible values the names of any
            XBlock type in the system, including custom blocks. Examples are
            course, chapter, sequential, vertical, html, problem, video, and
            discussion.
        display_name: (str) The display name of the block.

    **Returns**

        * 200 on success with above fields.
        * 400 if an invalid parameter was sent or the username was not provided
        * 401 unauthorized, the provided access token has expired and is no longer valid
          for an authenticated request.
        * 403 if a user who does not have permission to masquerade as
          another user specifies a username other than their own.
        * 404 if the course is not available or cannot be seen.
    """

    def get_certificate(self, request, course_id):
        """
        Returns the information about the user's certificate in the course.

        Arguments:
            request (Request): The request object.
            course_id (str): The identifier of the course.
        Returns:
            (dict): A dict containing information about location of the user's certificate
            or an empty dictionary, if there is no certificate.
        """
        if request.user.is_authenticated:
            certificate_info = certificate_downloadable_status(request.user, course_id)
            if certificate_info['is_downloadable']:
                return {
                    'url': request.build_absolute_uri(
                        certificate_info['download_url']
                    ),
                }
        return {}

    def list(self, request, **kwargs):  # pylint: disable=W0221
        """
        REST API endpoint for listing all the blocks information in the course and
        information about the course while regarding user access and roles.

        Arguments:
            request - Django request object
        """

        response = super().list(request, kwargs)

        if request.GET.get('return_type', 'dict') == 'dict':
            course_id = request.query_params.get('course_id', None)
            course_key = CourseKey.from_string(course_id)
            course_overview = CourseOverview.get_from_id(course_key)
            course_data = {
                'id': course_id,
                'certificate': self.get_certificate(request, course_key),
            }
            course_data.update(CourseInfoOverviewSerializer(course_overview).data)
            response.data.update(course_data)
        return response
