from django.conf import settings
from django.db import models
from django.db.models import Q
from django.http import HttpResponse, HttpResponseNotFound
from django.contrib.auth.decorators import login_required
import json
import logging

from mastrms.app.utils.data_utils import jsonResponse, jsonErrorResponse
from mastrms.quote.models import Quoterequest, Formalquote, Organisation, UserOrganisation
from mastrms.repository.json_util import makeJsonFriendly
from mastrms.decorators import admins_only, admins_or_nodereps, privileged_only, authentication_required
from mastrms.users.user_manager import GroupManager
from mastrms.users.models import * # All the MAUser functions, plus the groups information
from mastrms.users.forms import getDetailsFromRequest
from mastrms.app.utils.mail_functions import sendApprovedRejectedEmail, sendAccountModificationEmail

logger = logging.getLogger('mastrms.general')


def _filter_users(groups, requestinguser):
    '''This function produces a list of users, according to Madas rules,
    that is, if the requesting user is an Admin, they see all users in 'groups',
    but if they are only a noderep, they only see members in 'groups' who are
    also in their node'''

    retval = []
    if not requestinguser.IsPrivileged:
        return retval #early exit. Bad data.


    searchGroups = []
    if not (requestinguser.IsAdmin or requestinguser.IsMastrAdmin) and requestinguser.IsPrivileged:
        searchGroups += requestinguser.Nodes

    searchGroups += groups

    #The default 'method' is and
    userlist = getMadasUsersFromGroups(searchGroups)
    return userlist

@admins_or_nodereps
def admin_requests(request, *args):
    '''This corresponds to Madas Dashboard->Admin->Active Requests
       Accessible by Administrators, Node Reps
    '''
    currentuser = getCurrentUser(request)
    newlist = _filter_users([MADAS_PENDING_GROUP], currentuser)
    return jsonResponse(items=newlist)

@privileged_only
def user_search(request, *args):
    '''This corresponds to Madas Dashboard->Admin->Active User Search
       Accessible by Administrators, Node Reps
    '''
    currentuser = getCurrentUser(request)
    newlist = _filter_users([MADAS_USER_GROUP], currentuser)
    #for each user in newlist, set the client flag if applicable.
    #This is potentially pretty inefficient, because we are loading every user.
    for user_n in newlist:
        u = getMadasUser(user_n['email'])
        user_n['isClient'] = u.IsClient
    return jsonResponse(items=newlist)

@admins_or_nodereps
def rejected_user_search(request, *args):
    '''This corresponds to Madas Dashboard->Admin->Rejected User Search
       Accessible by Administrators, Node Reps
    '''
    currentuser = getCurrentUser(request)
    newlist = _filter_users([MADAS_REJECTED_GROUP], currentuser)
    return jsonResponse(items=newlist)

@admins_or_nodereps
def deleted_user_search(request, *args):
    '''This corresponds to Madas Dashboard->Admin->Deleted User Search
       Accessible by Administrators, Node Reps
    '''
    currentuser = getCurrentUser(request)
    newlist = _filter_users([MADAS_DELETED_GROUP], currentuser)
    return jsonResponse(items=newlist)

@privileged_only
def user_load(request, *args):
    '''This is called when an admin user opens up an individual user record
       from an admin view e.g. Active User Search
       Accessible by Administrators, Node Reps
    '''
    logger.debug('***admin/user_load : enter ***' )
    u = User.objects.get(email=request.REQUEST['email'])
    d = u.get_client_dict()
    logger.debug('***admin/user_load : exit ***' )
    return jsonResponse(data=[d])

@privileged_only
def user_save(request, *args):
    '''This is called when an admin user hits save on an individual user record
       from an admin view e.g. Active User Search
       Accessible by Administrators, Node Reps
    '''
    logger.debug('***admin/user_save : enter ***')
    currentuser = getCurrentUser(request)
    parsedform = getDetailsFromRequest(request)
    #look up the user they are editing:
    existingUser = getMadasUser(parsedform['email'])
    existingstatus = existingUser.StatusGroup
    success = saveMadasUser(currentuser, parsedform['email'], parsedform['details'], parsedform['status'], parsedform['password'])
    newstatus = existingUser.StatusGroup

    if newstatus != existingstatus:
        if newstatus == MADAS_USER_GROUP or newstatus == MADAS_REJECTED_GROUP:
            sendApprovedRejectedEmail(request, parsedform['email'], newstatus)
        else:
            sendAccountModificationEmail(request, parsedform['email'])
    #do something based on 'status' (either '' or something new)
    nextview = 'admin:usersearch'
    if newstatus != '':
        if newstatus == MADAS_PENDING_GROUP:
            nextview = 'admin:adminrequests'
        elif newstatus == MADAS_REJECTED_GROUP:
            nextview = 'admin:rejectedUsersearch'
        elif newstatus == MADAS_DELETED_GROUP:
            nextview = 'admin:deletedUsersearch'

    #apply organisational changes
    mail = request.REQUEST['email']
    targetUser, created = User.objects.get_or_create(email=mail)

    try:
        UserOrganisation.objects.filter(user=targetUser).delete()

        try:
            orgid = request.REQUEST['organisation']
            org = Organisation.objects.get(id=orgid)
        except ValueError:
            org = None
        except Organisation.DoesNotExist:
            org = None

        if org:
            uo = UserOrganisation(user=targetUser, organisation=org)
            uo.save()
            logger.debug('added user to org')
    except Exception, e:
        logger.warning('FATAL error adding or removing user from organisation: %s' % (str(e)))

    logger.debug('***admin/user_save : exit ***' )

    return jsonResponse(mainContentFunction=nextview)

@admins_or_nodereps
def node_save(request, *args):
    '''This is called when saving node details in the Node Management.
       Madas Dashboard->Admin->Node Management
       Accessible by Administrators, Node Reps
    '''
    logger.debug('*** node_save : enter ***')
    oldname = str(request.REQUEST.get('originalName', ''))
    newname = str(request.REQUEST.get('name', ''))

    returnval = False
    if oldname!=newname and newname !='':
        if oldname == '':
            if not GroupManager.add_group(newname):
                raise Exception("Couldn't add new node: " + newname)
        else:
            if not GroupManager.rename_group(oldname, newname):
                raise Exception("Couldn't rename node %s to %s" % (oldname, newname))
    else:
        #make no changes.
        logger.warning("Node save: oldname was newname, or newname was empty. Aborting")

    logger.debug( '*** node_save : exit ***' )
    return jsonResponse(mainContentFunction='admin:nodelist')

@admins_or_nodereps
def node_delete(request, *args):
    '''This is called when saving node details in the Node Management.
       Madas Dashboard->Admin->Node Management
       Accessible by Administrators, Node Reps
    '''
    logger.debug('*** node_delete : enter ***')
    #We must make sure 'Administrator' and 'User' groups cannot be deleted.
    delname = str(request.REQUEST.get('name', ''))
    ldelname = delname.lower()
    if ldelname == 'administrators' or ldelname == 'users':
        #Don't delete these sorts of groups.
        pass
    else:
        ret = GroupManager.delete_group(delname)

    logger.debug( '*** node_delete : enter ***' )
    return jsonResponse(mainContentFunction='admin:nodelist')

@admins_or_nodereps
def org_save(request):

    org_id = request.REQUEST.get('id', None)

    if org_id is not None and org_id != '':
        if org_id == '0':
            org = Organisation()
        else:
            org = Organisation.objects.get(id=org_id)

    org.name = request.REQUEST.get('name', 'No Name')
    org.abn = request.REQUEST.get('abn', 'No ABN')

    org.save()

    return jsonResponse()

@admins_or_nodereps
def org_delete(request):

    args = request.REQUEST
    org_id = args['id']

    rows = Organisation.objects.filter(id=org_id)
    rows.delete()

    return jsonResponse()

@authentication_required
def list_organisations(request):

    if request.GET:
        args = request.GET
    else:
        args = request.POST


    # basic json that we will fill in
    output = {'metaData': { 'totalProperty': 'results',
                            'successProperty': 'success',
                            'root': 'rows',
                            'id': 'id',
                            'fields': [{'name':'id'}, {'name':'name'}, {'name':'abn'}]
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': []
              }

    rows = Organisation.objects.all()

    # add row count
    output['results'] = len(rows);

    # add rows
    for row in rows:
        d = {}
        d['id'] = row.id
        d['name'] = row.name
        d['abn'] = row.abn

        output['rows'].append(d)


    output = makeJsonFriendly(output)

    return HttpResponse(json.dumps(output))
