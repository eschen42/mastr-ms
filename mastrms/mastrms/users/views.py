# Create your views here.
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect
from mastrms.app.utils.data_utils import jsonResponse, makeJsonFriendly
from mastrms.app.utils.mail_functions import sendAccountModificationEmail
from .models import *
from .user_manager import GroupManager
from .forms import getDetailsFromRequest

##The user info view, which sends the state of the logged in
##user to the frontend.
def userinfo(request):
    m = getCurrentUser(request, force_refresh = True)
    return HttpResponse(m.toJson())


def listAllNodes(request, *args):
    '''
    This view lists all nodes in the system
    These are the groups left over when the
    status and administrative groups are removed

    The format for the return is a list of dicts,
    each entry having a 'name' and a 'submitvalue'

    Note: this is for use in a dropdown which expects
    an additional option "Don't Know" which has the value ''.
    If request.REQUEST has 'ignoreNone', we do not do this.
    ""
    '''
    ldapgroups = GroupManager.list_groups()
    groups = []
    if not request.REQUEST.has_key('ignoreNone'):
        groups.append({'name':'Don\'t Know', 'submitValue':''})

    for groupname in ldapgroups:
        #Cull out the admin groups and the status groups
        if groupname not in MADAS_STATUS_GROUPS and groupname not in MADAS_ADMIN_GROUPS:
            groups.append({'name':groupname, 'submitValue':groupname})
    return jsonResponse(items=groups)



#Use view decorator here
@login_required
def user_load_profile(request, *args):
    '''This is called when loading user details - when the user
       clicks on the User button in the dashboard and selects 'My Account'
       Accessible by any logged in user
    '''
    logger.debug('***userload : enter ***')
    d = makeJsonFriendly([request.user.get_client_dict()])
    logger.debug('***userload : exit ***')
    return jsonResponse(data=d)

def userSave(request, *args):
    '''This is called when saving user details - when the user
       clicks on the User button in the dashboard and selects 'My Account',
       changes some details, and hits 'save'
       Accessible by any logged in user
    '''
    logger.debug('***users/userSave : enter ***' )
    success = False
    currentuser = getCurrentUser(request)
    parsedform = getDetailsFromRequest(request)

    #With a usersave, you are always editing your own user
    parsedform['email'] = currentuser.email
    success = saveMadasUser(currentuser, parsedform['email'], parsedform['details'], parsedform['status'], parsedform['password'])
    #refresh the user in case their details were just changed
    currentuser = getCurrentUser(request, force_refresh=True)

    if success:
        sendAccountModificationEmail(request, parsedform['email'])
    else:
        logger.error('Error saving user: %s' % (parsedform['email']))
        raise Exception('Error saving user.')

    logger.debug('***users/userSave : exit ***')
    return jsonResponse(mainContentFunction='user:myaccount')
