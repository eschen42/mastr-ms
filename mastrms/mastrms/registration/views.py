from mastrms.app.utils.data_utils import jsonResponse, jsonErrorResponse
from mastrms.users.models import *
from mastrms.users.forms import getDetailsFromRequest
from mastrms.app.utils.mail_functions import sendRegistrationToAdminEmail
import logging
from django.conf import settings

logger = logging.getLogger('mastrms.general')

def submit(request, *args):
    '''This adds a new user into ldap with no groups
    '''
    detailsDict = getDetailsFromRequest(request)

    username = detailsDict['username']
    user_exists = User.objects.filter(username=username).exists()

    if not user_exists:
        #saveMadasUser will add the user if they do not exist already.
        user_exists = saveMadasUser(request.user, username, detailsDict['details'], detailsDict['status'], detailsDict['password'])

        if not user_exists:
            logger.warning("Could not add new user %s" % (username))
    else:
        logger.warning("User %s already existed, skipping registration" % (username))

    if user_exists:
        sendRegistrationToAdminEmail(request, username,
                                     settings.REGISTRATION_TO_EMAIL)

    return jsonResponse()