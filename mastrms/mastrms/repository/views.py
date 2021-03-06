import os, stat
import io
import csv
import re
import logging
import zipstream
import json
from decimal import Decimal, DecimalException
from datetime import datetime, timedelta
from itertools import groupby, chain
from django.db.models import get_model, Q
from django.core import urlresolvers
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.mail import mail_admins
from django.core.files.uploadedfile import TemporaryUploadedFile
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.http import HttpResponse, HttpResponseNotFound, HttpResponseForbidden, HttpResponseNotAllowed, HttpResponseRedirect, HttpResponseBadRequest, HttpResponseServerError
from django.http import FileResponse, StreamingHttpResponse
from django.shortcuts import render_to_response, get_object_or_404
from django.utils.encoding import smart_bytes, smart_text
from mastrms.repository.models import Experiment, ExperimentStatus, Organ, AnimalInfo, HumanInfo, PlantInfo, MicrobialInfo, Treatment,  BiologicalSource, SampleClass, Sample, UserInvolvementType, SampleTimeline, UserExperiment, OrganismType, Project, SampleLog, Run, RUN_STATES, RunSample, InstrumentMethod, ClientFile, StandardOperationProcedure, RuleGenerator, Component, NodeClient
from mastrms.quote.models import Organisation, Formalquote
from mastrms.decorators import mastr_users_only
from json_util import makeJsonFriendly
from mastrms.app.utils.data_utils import jsonResponse, zipdir, pack_files
from mastrms.repository.permissions import user_passes_test
from mastrms.users.models import User
from mastrms.repository import rulegenerators
from mastrms.app.utils.mail_functions import FixedEmailMessage
from django.views.generic import View

logger = logging.getLogger('mastrms.general')

class ClientLookupException(Exception):
    """
    exception message will be a json-encoded message suitable to return to the client
    """
    def __init__(self, msg):
        self.output = {
            'success' : False,
            'msg' : msg
            }
        super(ClientLookupException, self).__init__(json.dumps(self.output))

def get_object_by_id_or_error(request, cls, key):
    obj_id = request.POST.get(key, None)
    if obj_id is None:
        raise ClientLookupException('need %s param' % key)
    try:
        obj = cls.objects.get(id=obj_id)
    except cls.DoesNotExist, e:
        raise ClientLookupException(str(e))
    return obj

@mastr_users_only
def create_object(request, model):
    '''
    Allow arbitrary insertion of any data object but requiring certain defaults
    to be defined, particularly for object references in some cases, look up the value
    '''

    # get args and remove the id from it if it exists
    if request.GET:
        args = request.GET
    else:
        args = request.POST

    obj = None
    if model == 'experiment':
        obj = create_experiment(request.user, args, base_experiment_id=args.get('base_experiment_id', None) )

    else:
        #create model object
        model_obj = get_model('repository', model)
        obj = model_obj()

        for key in args.keys():
            obj.__setattr__(key, args[key])

        if model == 'run':
            obj.creator = request.user
            if (obj.rule_generator is not None) and (not obj.rule_generator.is_accessible_by(request.user)):
                return HttpResponseForbidden("Invalid rule generator for run");
            if obj.number_of_methods in ('', 'null'):
                obj.number_of_methods = None
            if obj.order_of_methods in ('', 'null'):
                obj.order_of_methods = None

        obj.save()

        if model == 'project':
            if not args.get('projectManagers'):
                obj.managers.add(request.user)
            else:
                save_project_managers(obj, args.get('projectManagers'))

        if model == 'biologicalsource':
            return records(request, 'organism', 'id', obj.organism.id)

        if model == 'animal' or model == 'plant' or model == 'human':
            o = Organ(source=obj, name='Unknown')
            o.save()

        if model == 'samplelog':
            obj.user = request.user
            obj.save()

    return records(request, model, 'id', obj.id)


def check_experiment_cloneable(request, experiment_id):
    if request.GET:
        args = request.GET
    else:
        args = request.POST
    print args
    print 'experiment ID is :', experiment_id
    success = False
    message = "No base experiment provided"
    if experiment_id is not None:
        success = check_distinct_sample_classes(experiment_id)
        if not success:
            message = "Non unique sample classes detected. Ensure timelines, organs, and treatments will generate unique sample classes."
        else:
            success = check_non_id_sample_classes(experiment_id)
            if not success:
                message = "Some sample classes in the base experiment have no organs/treatment/timelines."
        #insert more checks here if required


    return HttpResponse( json.dumps({'success': success, 'message':message}) )


def check_distinct_sample_classes(experiment):
    sampleclasses = SampleClass.objects.filter(experiment = experiment)
    sampleclassnames = [s.__unicode__() for s in sampleclasses]

    if len(sampleclasses) == len(set(sampleclassnames)):
        return True
    else:
        return False

def check_non_id_sample_classes(experiment):
    sampleclasses = SampleClass.objects.filter(experiment = experiment)
    for sampleclass in sampleclasses:
        if sampleclass.component_abbreviations() == '':
            return False
    return True


def clone_experiment(base_experiment):
    ''' Returns the cloned experiment
        Assumes appropriate checks have been done to ensure experiment is cloneable.
    '''
    base_exp = base_experiment

    exp = Experiment()
    exp.title = "%s (cloned)" % (base_exp.title)
    exp.comment = base_exp.comment
    exp.description = base_exp.description
    exp.project = base_exp.project
    exp.instrument_method = base_exp.instrument_method
    exp.status = base_exp.status
    exp.investigation = base_exp.investigation
    exp.save()

    #users need to be brought across if this is cloned
    base_exp_users = UserExperiment.objects.filter(experiment=base_exp)
    for base_exp_user in base_exp_users:
        exp_user = UserExperiment(user=base_exp_user.user,
                                    experiment=exp,
                                    type=base_exp_user.type,
                                    additional_info=base_exp_user.additional_info)
        exp_user.save()
    #Source
    source = BiologicalSource(experiment=exp)
    base_source = BiologicalSource.objects.get(experiment=base_exp)
    source.type = base_source.type
    source.save()

    #Organs
    base_organs = Organ.objects.filter(experiment=base_exp)
    for base_organ in base_organs:
        organ = Organ(experiment = exp)
        organ.name = base_organ.name
        organ.abbreviation = base_organ.abbreviation
        organ.detail = base_organ.detail
        organ.save()

    #Timelines
    base_timelines = SampleTimeline.objects.filter(experiment=base_exp)
    for base_timeline in base_timelines:
        tl = SampleTimeline(experiment=exp,
                            abbreviation=base_timeline.abbreviation,
                            timeline = base_timeline.timeline)
        tl.save()
    #Treatments
    base_treatments = Treatment.objects.filter(experiment=base_exp)
    for base_treatment in base_treatments:
        tr = Treatment(experiment = exp,
                        abbreviation = base_treatment.abbreviation,
                        name = base_treatment.name,
                        description = base_treatment.description)
        tr.save()

    #Generate sample classes, and then generate samples
    regenerate_sample_classes(exp.id)

    #For each sample class, count all the samples which have that class.
    base_sampleclasses = SampleClass.objects.filter(experiment=base_exp)
    exp_sampleclasses = SampleClass.objects.filter(experiment=exp)
    base_sampleclass_dict = {}
    exp_sampleclass_dict = {}

    #Build the dicts, keyed on the sample class name
    #These should be unique, which should have been determined earlier by
    #calling check_experiment_cloneable
    for base_sampleclass in base_sampleclasses:
        base_sampleclass_dict[base_sampleclass.__unicode__()]=base_sampleclass

    for exp_sampleclass in exp_sampleclasses:
        exp_sampleclass_dict[exp_sampleclass.__unicode__()] = exp_sampleclass

    #Now generate samples for each:
    for classname in base_sampleclass_dict.keys():
        base_sampleclass = base_sampleclass_dict[classname]
        exp_sampleclass = exp_sampleclass_dict.get(classname, None)
        if exp_sampleclass is not None:
            for orig in Sample.objects.filter(sample_class=base_sampleclass):
                s = Sample()
                s.sample_class = exp_sampleclass
                s.label = orig.label
                s.comment = orig.comment
                s.weight = orig.weight
                s.sample_class_sequence = orig.sample_class_sequence
                s.experiment = exp
                s.save()

    return exp


def create_experiment(user, attributes, base_experiment_id = None):
    '''Creates an experiment, and associated objects in the DB.
       If this experiment is based on another experiment, some values from there are
       brought across.
       Returns the created experiment object'''

    base_exp = None
    exp = None
    #Try cloning the experiment if it needs it
    if base_experiment_id is not None:
        try:
            base_exp = Experiment.objects.get(id=base_experiment_id)
            exp = clone_experiment(base_exp)
        except Exception, e:
            #unable to find base experiment
            print 'Error in clone experiment: ', e
            pass
    else:
        exp = Experiment()
        for key in attributes.keys():
            exp.__setattr__(key, attributes[key])
        exp.save()

        #create a single user
        uit, created = UserInvolvementType.objects.get_or_create(name='Principal Investigator')
        ue = UserExperiment()
        ue.experiment = exp
        ue.type = uit
        ue.user = user
        ue.save()

        #Biological Source
        source = BiologicalSource(experiment=exp)
        #default source and organ
        source.type_id=1
        source.save()

        #Organs
        organ = Organ(experiment=exp)
        organ.name='Unknown'
        organ.save()


    return exp

def clone_run(request, run_id):
    result = {'success':False, 'message':"None", 'data':None}
    try:
        base_run = Run.objects.get(id=run_id)
        new_run = Run()
        new_run.experiment        = base_run.experiment
        new_run.method            = base_run.method
        new_run.creator           = base_run.creator
        new_run.title             = "%s (cloned)" % (base_run.title)
        new_run.machine           = base_run.machine
        new_run.generated_output  = base_run.generated_output
        new_run.state             = RUN_STATES.NEW[0]
        new_run.rule_generator    = base_run.rule_generator
        new_run.number_of_methods = base_run.number_of_methods
        new_run.order_of_methods  = base_run.order_of_methods
        new_run.save()

        #samples
        base_rs = RunSample.objects.filter(run=base_run)
        for base_runsample in base_rs:
            new_runsample               = RunSample(run=new_run)
            new_runsample.sample        = base_runsample.sample
            new_runsample.component     = base_runsample.component
            new_runsample.sequence      = base_runsample.sequence
            new_runsample.vial_number   = base_runsample.vial_number
            new_runsample.method_number = base_runsample.method_number
            new_runsample.save()

        #sample count
        #incomplete sample count
        #complete sample count

        result['success'] = True
        result['data'] = {'id':new_run.id}

    except Exception, e:
        result['success'] = False
        result['message'] = 'Exception: %s' % (str(e))

    return HttpResponse(json.dumps(result))


@mastr_users_only
def create_samples(request):
    # get args and remove the id from it if it exists
    if request.GET:
        args = request.GET
    else:
        args = request.POST

    #create model object
    model_obj = get_model('repository', 'sample')

    for i in range(0, int(args['replicates'])):
        obj = model_obj()

        for key in args.keys():
            obj.__setattr__(key, args[key])

        obj.save()

    return records(request, 'sample', 'id', obj.id)


@mastr_users_only
def create_sample_log(request, sample_id, type, description):
    log = SampleLog(type=type,description=description,sample_id=sample_id)
    log.save()


@mastr_users_only
def batch_create_sample_logs(request):
    #get args and remove the id from it if it exists
    if request.GET:
        args = request.GET
    else:
        args = request.POST

    #todo stuff
    type = args.get('type')
    description = args.get('description')
    sampleids = args.get('sample_ids').split(',')

    for sampleid in sampleids:
        create_sample_log(request, sampleid, type, description)
    return records(request, 'samplelog', 'id', 0)


@mastr_users_only
def update_object(request, model, id):

    if id == '0':
        return create_object(request, model)

    if request.GET:
        args = request.GET
    else:
        args = request.POST

    #fetch the object and update all values
    model_obj = get_model('repository', model) # try to get app name dynamically at some point
    params = {'id':id}

    rows = model_obj.objects.filter(**params)

    for row in rows:
        for key in args:
            val = None if args[key] == "null" else args[key]
            row.__setattr__(key, val)

        #TODO clean this stuff up!
        if model == 'run':
            logger.debug('updating run.')
            if (row.rule_generator is not None) and (not row.rule_generator.is_accessible_by(request.user)):
                return HttpResponseForbidden("Invalid rule generator for run");

            if row.number_of_methods in ('', 'null'):
                row.number_of_methods = None
            if row.order_of_methods in ('', 'null'):
                row.order_of_methods = None
        row.save()
        if model == 'project':
            save_project_managers(row, args.get('projectManagers'))


    return records(request, model, 'id', id)


@mastr_users_only
def delete_object(request, model, id):

    if request.GET:
        args = request.GET
    else:
        args = request.POST


    #fetch the object and update all values
    model_obj = get_model('repository', model) # try to get app name dynamically at some point
    params = {'id':id}
    rows = model_obj.objects.filter(**params)
    rows.delete()
    return records(request, model, 'id', id)


@mastr_users_only
def associate_object(request, model, association, parent_id, id):

    if request.GET:
        args = request.GET
    else:
        args = request.POST


    #fetch the object and update all values
    model_obj = get_model('repository', model) # try to get app name dynamically at some point
    params = {'id':parent_id}
    rows = model_obj.objects.filter(**params)

    if model == 'project' and association == 'manager':
        assoc_model_obj = User
    else:
        assoc_model_obj = get_model('repository', association) # try to get app name dynamically at some point
    assoc_params = {'id':id}
    assoc_rows = assoc_model_obj.objects.filter(**assoc_params)

    if len(assoc_rows) > 0:
        assoc_item = assoc_rows[0]
        assoc_name = association + 's'

        for row in rows:
            current_set = row.__getattribute__(assoc_name)
            current_set.add(assoc_item)
            row.save()

    return records(request, model, 'id', parent_id)


@mastr_users_only
def dissociate_object(request, model, association, parent_id, id):

    if request.GET:
        args = request.GET
    else:
        args = request.POST


    #fetch the object and update all values
    model_obj = get_model('repository', model) # try to get app name dynamically at some point
    params = {'id':parent_id}
    rows = model_obj.objects.filter(**params)

    assoc_name = association + 's'

    for row in rows:
        current_set = row.__getattribute__(assoc_name)
        found = current_set.filter(id=id)
        for obj in found:
            current_set.remove(obj)
        row.save()

    return records(request, model, 'id', parent_id)


@mastr_users_only
def records(request, model, field, value):

    if request.GET:
        args = request.GET
    else:
        args = request.POST

    # basic json that we will fill in
    output = {'metaData': { 'totalProperty': 'results',
                            'root': 'rows',
                            'id': 'id',
                            'successProperty': 'success',
                            'fields': []
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': []
              }

    model_obj = get_model('repository', model) # try to get app name dynamically at some point
    params = {str(field):str(value)}
    rows = model_obj.objects.filter(**params)

    # add fields to meta data
    for f in model_obj._meta.fields:
        output['metaData']['fields'].append({'name':f.name})

    # add many to many
    for f in model_obj._meta.many_to_many:
        output['metaData']['fields'].append({'name':f.name})

    # add row count
    output['results'] = len(rows);

    # add rows
    for row in rows:
        d = {}
        for f in model_obj._meta.fields:
            d[f.name] = f.value_from_object(row)

        if model == 'run':
            #Runs should not return their collection of RunSamples, as they cannot be json serialized
            pass
        else:
            for f in model_obj._meta.many_to_many:
                vals = []
                for val in f.value_from_object(row):
                    vals.append(makeJsonFriendly(val))
                d[f.name] = vals

        output['rows'].append(d)

    print output
    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

def save_project_managers(project, project_manager_ids):
    requested_proj_managers = set([int(id) for id in project_manager_ids.split(',')])
    current_proj_managers = set([row['id'] for row in project.managers.values('id')])
    to_remove = current_proj_managers - requested_proj_managers
    to_add = requested_proj_managers - current_proj_managers
    if to_add:
        project.managers.add(*to_add)
    if to_remove:
        project.managers.remove(*to_remove)

@mastr_users_only
def recordsProject(request, project_id):
    output = json_records_template(['id', 'title', 'client', 'managers'])
    project = Project.objects.get(pk=project_id)
    def manager_details(manager):
        return {"id": manager.id, "email": "%s (%s)" % (manager.Name, manager.email)}
    managers = map(manager_details, project.managers.all())
    managers = filter(lambda x: x is not None, managers)
    output['rows'].append({
            'id': project.id,
            'title': project.title,
            'description': project.description,
            'client': project.client_id,
            'managers': managers
        })

    output['results'] = len(output['rows'])

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recent_projects(request):
    output = json_records_template(['id', 'title', 'client'])
    user = request.user
    ninety_days_ago = datetime.now() - timedelta(90)
    projects = Project.objects.filter(
        Q(client=user) |
        Q(managers=user)
        ).filter(created_on__gt=ninety_days_ago)
    for project in projects:
        output['rows'].append({
            'id': project.id,
            'title': project.title,
            'client': project.client.email
        })

    output['results'] = len(output['rows'])

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recent_experiments(request):
    output = json_records_template(['id', 'title', 'status'])
    user = request.user
    ninety_days_ago = datetime.now() - timedelta(90)
    experiments = Experiment.objects.filter(
        Q(project__client=user) |
        Q(project__managers=user) |
        Q(users=user)
        ).filter(created_on__gt=ninety_days_ago)
    for experiment in experiments:
        output['rows'].append({
            'id': experiment.id,
            'title': experiment.title,
            'status': experiment.status.name if experiment.status else None
        })

    output['results'] = len(output['rows'])

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recent_runs(request):
    output = json_records_template(['id', 'title', 'method', 'machine', 'state'])
    user = request.user
    ninety_days_ago = datetime.now() - timedelta(90)
    runs = Run.objects.filter(creator=user, created_on__gt=ninety_days_ago)
    for run in runs:
        output['rows'].append({
            'id': run.id,
            'title': run.title,
            'machine': str(run.machine),
            'method': str(run.method),
            'state': RUN_STATES.name(run.state)
        })

    output['results'] = len(output['rows'])

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recordsMAStaff(request):
    args = request.REQUEST
    output = json_records_template(['key', 'value'])

    for user in User.objects.all():
        if not user.IsClient:
            output["rows"].append({
                "key": user.id,
                "value": "%s (%s)" % (user.get_full_name(), user.email)
            })

    output["rows"].sort(key=lambda r: r["value"])

    output['results'] = len(output['rows'])

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))


@mastr_users_only
def recordsClientList(request):
    args = request.REQUEST
    output = json_records_template(['id', 'is_client', 'name', 'email', 'organisationName', 'displayValue'])

    qs = User.objects.all()

    if not args.get('allUsers'):
        qs = qs.extra(where=["id IN (SELECT DISTINCT client_id FROM repository_project ORDER BY client_id)"])

    nodemembers = []
    mastaff = []
    nodereps = []
    clients = []

    for user in qs:
        record = {
            "id": user.id,
            "is_client": "Yes" if user.IsClient else "No",
            "name": user.Name,
            "email": user.email or '<None>',
            "displayValue": "%s (%s)" % (user.Name, user.email or '<None>'),
            "organisationName": user.organisation_set.all()[0].name if user.organisation_set.exists() else ''
        }

        if args.get('sortUsers') and request.user:
            if user.PrimaryNode == request.user.PrimaryNode:
                nodemembers.append(record)
            elif user.IsStaff or user.IsMastrStaff or user.IsMastrAdmin:
                mastaff.append(record)
            elif not user.IsClient:
                nodereps.append(record)
            else:
                clients.append(record)

        else:
            clients.append(record)

    #sort each list if there are members
    for l in [nodemembers, mastaff, nodereps, clients]:
        if len(l) > 1:
            l.sort(key=lambda r: r['displayValue'])

    output["rows"] = nodemembers + mastaff + nodereps + clients

    output['results'] = len(output['rows'])

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

def _merge_duplicate_entries(entries):
    "Merges duplicate folders within an extjs node tree."
    merged = []
    text = lambda e: e["text"]
    entries.sort(key=text)
    for k, items in groupby(entries, text):
        items = list(items)
        head = items[0]
        children = list(chain(*(item.get("children", []) or [] for item in items)))
        head["children"] = _merge_duplicate_entries(children) if children else None
        merged.append(head)
    return merged

def _client_files_subtree(client_files, path):
    """
    Produces an extjs node tree for the client_files queryset, within
    the subtree given by path.
    """
    entries = []
    client_files = client_files.filter(filepath__startswith=path)

    for client_file in client_files:
        text = client_file.filepath[len(path):]
        subfiles = client_files.filter(filepath__startswith=client_file.filepath + "/")
        if "/" not in text:
            filepath = real_file_path(client_file.experiment, client_file.filepath)
            entries.append({
                'text': text,
                'leaf': filepath is not None and not os.path.isdir(filepath),
                'id': client_file.id,
            })
        elif not subfiles.exists():
            # create parent folders for this entry
            entry = None
            for part in reversed(client_file.filepath[len(path):].split("/")):
                entry = {
                    'id': client_file.id if entry is None else None,
                    'text': part,
                    'leaf': entry is None,
                    'children': [entry] if entry else None,
                }
            entries.append(entry)

    return entries

def _client_files_experiments(client_files):
    "Makes an extjs tree node list of experiments which the client has access to"
    exps = [f.experiment for f in client_files.distinct("experiment")]
    return [{
        'id': "exp%s" % exp.id,
        'text': 'Experiment: %s' % exp.title,
        'leaf': False,
        'metafile': True,
    } for exp in exps]

def _client_files_list(client_files, nodeid):
    if nodeid.startswith("exp"):
        # Experiment top level -- seed list with the 2 standard folders.
        exp_id = nodeid[3:]
        client_files = client_files.filter(experiment_id=exp_id)
        path = ""
        output = [{
            "text": folder,
            "leaf": False,
            "metafile": True,
            "children": _client_files_subtree(client_files, folder + "/"),
        } for folder in ("Raw Data", "QC Data")]
    elif nodeid.startswith("xnode"):
        # if nodeid is an xnode then client wants to expand an empty node
        return []
    else:
        # Sub-folder within an experiment
        client_file = client_files.get(id=nodeid)
        client_files = client_files.filter(experiment_id=client_file.experiment_id)
        path = client_file.filepath + "/"
        output = []

    output.extend(_client_files_subtree(client_files, path))

    return _merge_duplicate_entries(output)

def recordsClientFiles(request):
    root = 'dashboardFilesRoot'
    node = request.REQUEST.get('node', None) or root

    rows = ClientFile.objects.filter(experiment__users=request.user)
    rows = rows.order_by("experiment", "filepath")

    if node == root:
        output = _client_files_experiments(rows)
    else:
        output = _client_files_list(rows, node)

    return HttpResponse(json.dumps(output))

@mastr_users_only
def populate_select(request, model=None, key=None, value=None, field=None, match=None):

    if request.GET:
        args = request.GET
    else:
        args = request.POST


    model_whitelist = {'organism': ['id', 'name', 'type'],
                       'organ': ['id', 'name', 'source_id', 'tissue', 'cell_type', 'subcellular_cell_type'],
                       'genotype': ['id', 'name'],
                       'gender': ['id', 'name'],
                       'animalinfo': ['id', 'sex', 'sex__name', 'age', 'parental_line'],
                       'location': ['id', 'name'],
                       'origindetails': ['id', 'detailed_location', 'information'],
                       'treatmentvariation': ['id','name','treatment'],
                       'treatment': ['id','name','source','description','type'],
                       'treatmenttype': ['id','name'],
                       'user': ['id','email'],
                       'standardoperationprocedure' : ['id', 'label'],
                       'organismtype' : ['id','name'],
                       'userinvolvementtype' : ['id', 'name'],
                       'plantinfo' : ['development_stage'],
                       'growthcondition' : ['id', 'greenhouse_id', 'greenhouse__name', 'detailed_location', 'lamp_details'],
                       'organisation': ['id', 'name'],
                       'sampleclass': ['id', 'class_id', 'experiment__id'],
                       'formalquote': ['id', 'toemail'],
                       'instrumentmethod': ['id','title'],
                       'rulegenerator': ['id','full_name'],
                       'machine': ['id','station_name'],
                       'experimentstatus':['id','name']
                       }


    # TODO do we need this with the decorators in place? ABM
    authenticated = request.user.is_authenticated()
    authorized = True # need to change this
    main_content_function = "" # may need to change this

    if not authenticated or not authorized:
        output = select_widget_json(authenticated=authenticated,authorized=authorized,main_content_function=main_content_function,success=False,input=[])
        return HttpResponse(output, status=401)


    try:

        if model not in model_whitelist.keys():
            raise ObjectDoesNotExist()

        if model == 'organisation':
            model_obj = get_model('quote', 'organisation')
        elif model == 'user':
            model_obj = get_model('auth', 'user')
        elif model == 'formalquote':
            model_obj = get_model('quote', 'formalquote')
        elif model == 'machine':
            model_obj = get_model('mdatasync_server', 'nodeclient')
        else:
            model_obj = get_model('repository', model)

        if field and match:
            params = {str(field):str(match)}
            rows = model_obj.objects.filter(**params)
        else:
            rows = model_obj.objects

        if key not in model_whitelist[model]:
            raise ObjectDoesNotExist("Field not allowed.")
        if value and value not in model_whitelist[model]:
            raise ObjectDoesNotExist("Field not allowed.")

        values = []

        if not key:
            raise ObjectDoesNotExist()
        for item in rows.all():
            values.append({"key":getattr(item, key), "value":getattr(item, value or key)})

        output = select_widget_json(authenticated=authenticated,authorized=authorized,main_content_function=main_content_function,success=True,input=values)
        return HttpResponse(output)


    except ObjectDoesNotExist:
        output = select_widget_json(authenticated=authenticated,authorized=authorized,main_content_function=main_content_function,success=False,input=[])
        return HttpResponseNotFound(output)


@mastr_users_only
def update_single_source(request, exp_id):

    args = request.GET.copy()
    # fixme: a ModelForm could be used here

    for key in args.keys():
        if args[key] in ('', 'null', 'undefined'):
            del args[key]

    def arg_str(key):
        return args.get(key, "")

    def arg_sex():
        return arg_str("sex") or u'U'

    def arg_int(key):
        if key in args:
            try: return int(args[key])
            except ValueError: pass
        return None

    def arg_decimal(key):
        if key in args:
            try: return Decimal(args[key])
            except DecimalException: pass
        return None

    def arg_date(key):
        if key in args:
            try: return datetime.strptime(args[key], "%Y-%m-%d").date()
            except ValueError: pass
        return None

    output = {'metaData': { 'totalProperty': 'results',
                            'successProperty': 'success',
                            'root': 'rows',
                            'id': 'id',
                            'fields': []
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': []
              }

    e = Experiment.objects.get(id=exp_id)

    if e is None:
        output['success'] = False
    else:
        try:
            # fixme: what about multiple objects returned?
            bs = BiologicalSource.objects.get(experiment=e)
        except ObjectDoesNotExist:
            bs = BiologicalSource(experiment=e)

        bs.type = OrganismType.objects.get(id=args['type'])
        bs.information = arg_str('information')
        bs.ncbi_id = arg_int('ncbi_id')
        #bs.label = arg_str('label')
        bs.save()

        info = bs.get_info()

        #process additional info
        if isinstance(info, MicrobialInfo):
            info.genus = arg_str('genus')
            info.species = arg_str('species')
            info.culture_collection_id = arg_str('culture')
            info.media = arg_str('media')
            info.fermentation_vessel = arg_str('vessel')
            info.fermentation_mode = arg_str('mode')
            info.innoculation_density = arg_decimal('density')
            info.fermentation_volume = arg_decimal('volume')
            info.temperature = arg_decimal('temperature')
            info.agitation = arg_decimal('agitation')
            info.ph = arg_decimal('ph')
            info.gas_type = arg_str('gastype')
            info.gas_flow_rate = arg_decimal('flowrate')
            info.gas_delivery_method = arg_str('delivery')
        elif isinstance(info, PlantInfo):
            info.development_stage = arg_str('development_stage')
        elif isinstance(info, AnimalInfo):
            info.sex = arg_sex()
            info.age = arg_int('age')
            info.parental_line = arg_str('parental_line')
            info.location = arg_str('location')
            # info.notes = arg_str('notes')
        elif isinstance(info, HumanInfo):
            info.sex = arg_sex()
            info.date_of_birth = arg_date('date_of_birth')
            info.bmi = arg_decimal('bmi')
            info.diagnosis = arg_str('diagnosis')
            info.location = arg_str('location')
            # info.notes = arg_str('notes')

        if info:
            try:
                info.save()
            except Exception, e:
                output['success'] = False
                output['msg'] = str(e)

    return HttpResponse(json.dumps(output))


@mastr_users_only
def recreate_sample_classes(request, experiment_id):

    if request.GET:
        args = request.GET
    else:
        args = request.POST

    regenerate_sample_classes(experiment_id)
    return recordsSampleClasses(request, experiment_id)

def regenerate_sample_classes(experiment_id):
    combos = []

    for biosource in BiologicalSource.objects.filter(experiment__id=experiment_id):
        combosForBioSource = []
        for organ in Organ.objects.filter(experiment__id=experiment_id):
            combosForOrgan = []

            base = { 'bs': biosource.id, 'o': organ.id }
            bcombos = [base]

            combosForTreatment = []
            for treatment in Treatment.objects.filter(experiment__id=experiment_id):
                for combo in bcombos:
                    tmp = combo.copy()
                    tmp['treatment'] = treatment.id
                    combosForTreatment.append(tmp.copy())
            if len(combosForTreatment) == 0:
                combosForTreatment = bcombos[:]

            combosForOrgan = combosForTreatment[:]

            combosForTimeline = []
            for timeline in SampleTimeline.objects.filter(experiment__id=experiment_id):
                for combo in combosForOrgan:
                    tmp = combo.copy()
                    tmp['time'] = timeline.id
                    combosForTimeline.append(tmp.copy())
            if len(combosForTimeline) > 0:
                combosForOrgan = combosForTimeline[:]

            combosForBioSource = combosForBioSource + combosForOrgan

        combos = combos + combosForBioSource


    #iterate over combos and current sampleclasses
    #if they already exist, fine
    #if they no longer exist, delete
    #if they don't exist, create
    currentsamples = SampleClass.objects.filter(experiment__id = experiment_id)
            #    print currentsamples[0]

    #determine what to delete and what to add
    foundclasses = set()

    for combo in combos:
        #look for item in currentsamples, if it exists, add it to the foundclasses set

        a = currentsamples

        #item for adding
        sc = SampleClass()
        sc.experiment_id = experiment_id
        sc.class_id = 'sample class'

        b = ''
        for key in combo.keys():
            if key == 'treatment':
                sc.treatments_id = combo[key]
                a = a.filter(treatments__id = combo[key])
            elif key == 'bs':
                sc.biological_source_id = combo[key]
                a = a.filter(biological_source__id = combo[key])
            elif key == 'o':
                sc.organ_id = combo[key]
                a = a.filter(organ__id = combo[key])
            elif key == 'time':
                sc.timeline_id = combo[key]
                a = a.filter(timeline__id = combo[key])
            b = b + ' ' + str(key) + ' ' + str(combo[key])

            #        print 'filtering with '+b

        if a:
            combo['id'] = a[0].id
            foundclasses.add(a[0].id)
            sc = a[0]
                #            print 'found'
        else:
            #if not found, add it on the spot
            sc.save()
            foundclasses.add(sc.id)
        #now check if we can auto-assign a name based on abbreviations
        if str(sc) != '':
            sc.class_id = str(sc)
            sc.save()

        #renumber all the samples
        count = 1
        for sample in sc.sample_set.all().order_by('sample_class_sequence', 'id'):
            sample.sample_class_sequence = count
            count = count + 1
            sample.save()

    #purge anything not in foundclasses
    purgeable = currentsamples.exclude(id__in=foundclasses)
    purgeable.delete()



@mastr_users_only
def recordsSampleClasses(request, experiment_id):

    if request.GET:
        args = request.GET
    else:
        args = request.POST


    # basic json that we will fill in
    output = {'metaData': { 'totalProperty': 'results',
                            'successProperty': 'success',
                            'root': 'rows',
                            'id': 'id',
                            'fields': [{'name':'id'}, {'name':'class_id'}, {'name':'treatment'}, {'name':'timeline'}, {'name':'organ'},  {'name':'enabled'}, {'name':'count'}]
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': []
              }

    # TODO do we need this with decorator in place ABM
    authenticated = request.user.is_authenticated()
    authorized = True # need to change this
    if not authenticated or not authorized:
        return HttpResponse(json.dumps(output), status=401)


    rows = SampleClass.objects.filter(experiment__id=experiment_id)

    # add row count
    output['results'] = len(rows);

    # add rows
    for row in rows:
        d = {}
        d['id'] = row.id
        d['class_id'] = row.class_id
        d['enabled'] = row.enabled

        if row.treatments:
            d['treatment'] = row.treatments.name

        if row.timeline:
            d['timeline'] = str(row.timeline)
        else:
            d['timeline'] = ''

        if row.organ:
            d['organ'] = row.organ.name

        d['count'] = row.sample_set.count()

        output['rows'].append(d)


    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))


@mastr_users_only
def recordsExperiments(request):
   return recordsExperimentsForProject(request, None)

@mastr_users_only
def recordsSamplesForExperiment(request):
    args = request.REQUEST

    # basic json that we will fill in
    output = {'metaData': {
                  'successProperty': 'success',
                  'root': 'rows',
                  'idProperty': 'id',
                  'fields': [{
                            "type": "int",
                            "name": "id"
                        },{
                            "type": "auto",
                            "name": "sample_id"
                        }, {
                            "type": "auto",
                            "name": "sample_class"
                        }, {
                            "type": "string",
                            "name": "sample_class__unicode"
                        }, {
                            "type": "auto",
                            "name": "experiment"
                        }, {
                            "type": "string",
                            "name": "experiment__unicode"
                        }, {
                            "type": "auto",
                            "name": "label"
                        }, {
                            "type": "auto",
                            "name": "comment"
                        }, {
                            "type": "auto",
                            "name": "weight"
                        }, {
                            "type": "int",
                            "name": "sample_class_sequence"
                        }
                    ],
                },
             'rows': []}

    experiment_id = args['experiment__id__exact']
    rows = Sample.objects.filter(experiment__id=experiment_id)

    randomise = args.get('randomise', False)


    if not randomise:
        sort_by = args.get('sort', 'sample_class') #sort by default on sample class
        if sort_by == 'sample_class':
            sort_by = 'sample_class__class_id'
        sort_dir = args.get('dir', 'ASC')
        if sort_dir == 'DESC':
            sort1 = '-' + sort_by
        else:
            sort1 = sort_by

        #Always sort with sequence second (mostly will be for class).
        sort2 = 'sample_class_sequence'

        rows = rows.order_by(sort1, sort2)

    # add rows
    for row in rows:
        d = {}
        d['comment'] = row.comment
        d['weight'] = row.weight
        d['sample_class__unicode'] = unicode(row.sample_class)
        d['sample_class_sequence'] = row.sample_class_sequence
        d['label'] = row.label
        d['experiment'] = row.experiment.id
        d['sample_id'] = row.sample_id
        d['experiment__unicode'] = unicode(row.experiment)
        d['id'] = row.id
        d['sample_class'] = row.sample_class.id if row.sample_class else None

        output['rows'].append(d)

    if randomise:
        import random
        random.shuffle(output['rows'])

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

def json_records_template(fields):
    fields_list = [{'name': f} for f in fields]
    return {
        'metaData': {
            'totalProperty': 'results',
            'successProperty': 'success',
            'root': 'rows',
            'id': 'id',
            'fields': fields_list
        },
        'results': 0,
        'authenticated': True,
        'authorized': True,
        'success': True,
        'rows': []
    }

@mastr_users_only
def recordsRuns(request):
    args = request.REQUEST

    # basic json that we will fill in
    output = json_records_template([
        'id', 'machine__unicode', 'sample_count', 'creator', 'method__unicode',
        'creator__unicode', 'state', 'machine', 'created_on', 'experiment',
        'complete_sample_count', 'rule_generator', 'number_of_methods', 'order_of_methods',
        'generated_output', 'title', 'method', 'incomplete_sample_count', 'experiment__unicode'
        ])

    condition = None

    experiment_id = request.REQUEST.get('experiment__id')
    if experiment_id:
        condition = Q(experiment__id = experiment_id)

    if not request.user.IsAdmin:
        extra_condition = Q(samples__experiment__project__managers=request.user)|Q(samples__experiment__users=request.user) | Q(creator=request.user)
        if condition:
            condition = condition & extra_condition
        else:
            condition = extra_condition

    if condition:
        rows = Run.objects.filter(condition)
    else:
        rows = Run.objects.all()

    output['results'] = len(rows)

    # add rows
    for row in rows:
        d = {}
        d['id'] = row.id
        d['machine__unicode'] = unicode(row.machine) if row.machine else ''
        d['sample_count'] = row.sample_count
        d['creator'] = row.creator_id
        d['method__unicode'] = unicode(row.method) if row.method else ''
        d['creator__unicode'] = unicode(row.creator)
        d['state'] = row.state
        d['machine'] = row.machine_id
        d['created_on'] = row.created_on
        d['experiment'] = row.experiment_id
        d['complete_sample_count'] = row.complete_sample_count
        d['rule_generator'] = row.rule_generator_id
        d['number_of_methods'] = row.number_of_methods
        d['order_of_methods'] = row.order_of_methods
        d['generated_output'] = row.generated_output
        d['title'] = row.title
        d['method'] = row.method_id
        d['incomplete_sample_count'] = row.incomplete_sample_count
        d['experiment__unicode'] = unicode(row.experiment) if row.experiment else ''

        output['rows'].append(d)

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recordsSamplesForRun(request):
    args = request.REQUEST

    # basic json that we will fill in
    output = json_records_template([
                "id", "sample_id", "sample_class", "sample_class__unicode",
                "experiment", "experiment__unicode", "label", "comment",
                "weight", "sample_class_sequence"])

    run_id = args['run_id']
    rows = Sample.objects.filter(run__id=run_id)

    sort_by = args.get('sort', 'runsample__sequence')
    sort_dir = args.get('dir', 'ASC')
    if sort_dir == 'DESC':
        sort = '-' + sort_by
    else:
        sort = sort_by

    rows = rows.order_by(sort)

    # add rows
    for row in rows:
        d = {}
        d['id'] = row.id
        d['sample_id'] = row.sample_id
        d['sample_class'] = row.sample_class_id
        d['sample_class__unicode'] = unicode(row.sample_class)
        d['experiment'] = row.experiment.id
        d['experiment__unicode'] = unicode(row.experiment)
        d['label'] = row.label
        d['comment'] = row.comment
        d['weight'] = row.weight
        d['sample_class_sequence'] = row.sample_class_sequence

        output['rows'].append(d)

    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))



def formatRuleGenResults(rows):
# basic json that we will fill in
    output = json_records_template([
        'id', 'name', 'version', 'full_name', 'description', 'state_id', 'state', 'accessibility_id', 'accessibility', 'created_by', 'apply_sweep_rule', 'apply_sweep_rule_display', 'node', 'startblock', 'sampleblock', 'endblock'
        ])

    output['results'] = len(rows)

    # add rows
    for row in rows:
        output['rows'].append(rulegenerators.convert_to_dict(row))

    output = makeJsonFriendly(output)
    return output

@mastr_users_only
def recordsRuleGenerators(request):
    args = request.REQUEST
    rows = rulegenerators.listRuleGenerators(request.user, accessibility=False, showEnabledOnly=False)
    output = formatRuleGenResults(rows)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recordsRuleGeneratorsAccessibility(request):
    args = request.REQUEST
    rows = rulegenerators.listRuleGenerators(request.user, accessibility=True, showEnabledOnly=False)
    output = formatRuleGenResults(rows)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recordsRuleGeneratorsAccessibilityEnabled(request):
    args = request.REQUEST
    rows = rulegenerators.listRuleGenerators(request.user, accessibility=True, showEnabledOnly=True)
    output = formatRuleGenResults(rows)
    return HttpResponse(json.dumps(output))


@mastr_users_only
def recordsRuleGenerators(request):
    args = request.REQUEST
    rows = RuleGenerator.objects.all()
    output = formatRuleGenResults(rows)
    return HttpResponse(json.dumps(output))


@mastr_users_only
def recordsExperimentsForProject(request, project_id):
    if request.GET:
        args = request.GET
    else:
        args = request.POST

    # basic json that we will fill in
    output = {'metaData': { 'totalProperty': 'results',
                            'successProperty': 'success',
                            'root': 'rows',
                            'id': 'id',
                            'fields': [{'name':'id'}, {'name':'status'}, {'name': 'status_text'}, {'name':'title'}, {'name':'job_number'}, {'name':'client'},  {'name':'principal'}, {'name':'description'}]
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': []
              }

    authenticated = request.user.is_authenticated()
    authorized = True # need to change this
    if not authenticated or not authorized:
        return HttpResponse(json.dumps(output), status=401)


    # Recreate the queryset filtering ExperimentAdmin does, since it's easier
    # than writing a custom serialiser for the Experiment model to fill in the
    # principal and client.
    if request.user.is_superuser:
        rows = Experiment.objects.all().order_by('status__id','id')
    else:
        rows = Experiment.objects.filter(Q(project__managers=request.user.id)|Q(users__id=request.user.id)).order_by('status__id','id')

    if project_id is not None:
        rows = rows.filter(project__id=project_id)

    # add row count
    output['results'] = len(rows);

    # add rows
    for row in rows:
        d = {}
        d['id'] = row.id
        d['status'] = row.status.id if row.status else None
        d['status_text'] = row.status.name if row.status else None
        d['title'] = row.title
        d['description'] = row.description
        d['job_number'] = row.job_number
        try:
            d['client'] = UserExperiment.objects.filter(type__id=3, experiment__id=row.id)[0].user.email
        except:
            d['client'] = ''
        try:
            d['principal'] = UserExperiment.objects.filter(type__id=1, experiment__id=row.id)[0].user.email
        except:
            d['principal'] = ''

        output['rows'].append(d)


    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))

@mastr_users_only
def recordsComponents(request):
    # basic json that we will fill in
    output = {'metaData': { 'totalProperty': 'results',
                            'successProperty': 'success',
                            'root': 'rows',
                            'id': 'id',
                            'fields': [{'name':'id'}, {'name':'component'}]
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': [],
              }

    rows = Component.objects.filter(id__gte=1) #only get components with id >= 1, which excludes samples
    output['results'] = len(rows);

    for row in rows:
        output['rows'].append({'id':row.id, 'component' : row.sample_type})
    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))


@mastr_users_only
def recordsSamples(request, experiment_id):
    if request.GET:
        args = request.GET
    else:
        args = request.POST


    # basic json that we will fill in
    output = {'metaData': { 'totalProperty': 'results',
                            'successProperty': 'success',
                            'root': 'rows',
                            'id': 'id',
                            'fields': [{'name':'id'}, {'name':'label'}, {'name':'weight'}, {'name':'comment'}, {'name':'sample_class'}, {'name':'last_status'}]
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': []
              }

    rows = Experiment.objects.get(id=experiment_id).sample_set.all()

    # add row count
    output['results'] = len(rows);

    # add rows
    for row in rows:
        d = {}
        d['id'] = row.id
        d['label'] = row.label
        d['weight'] = row.weight
        d['comment'] = row.comment
        d['sample_class'] = row.sample_class_id
        try:
            status = row.samplelog_set.all().order_by('-changetimestamp')[0]
            d['last_status'] = str(status)
        except:
            d['last_status'] = ''

        output['rows'].append(d)


    output = makeJsonFriendly(output)
    return HttpResponse(json.dumps(output))


@mastr_users_only
def recordsSamplesForClient(request, client):

    if request.GET:
        args = request.GET
    else:
        args = request.POST


    # basic json that we will fill in
    output = {'metaData': { 'totalProperty': 'results',
                            'successProperty': 'success',
                            'root': 'rows',
                            'id': 'id',
                            'fields': [{'name':'id'}, {'name':'experiment_id'}, {'name':'experiment_title'}, {'name':'label'}, {'name':'weight'}, {'name':'comment'}, {'name':'sample_class'}, {'name':'last_status'}]
                            },
              'results': 0,
              'authenticated': True,
              'authorized': True,
              'success': True,
              'rows': []
              }

    rows = Sample.objects.filter(experiment__users__email=client)

    # add row count
    output['results'] = len(rows);

    # add rows
    for row in rows:
        d = {}
        d['id'] = row.id
        d['label'] = row.label
        d['weight'] = row.weight
        d['comment'] = row.comment
        d['sample_class'] = row.sample_class_id
        d['experiment_id'] = row.experiment_id
        d['experiment_title'] = row.experiment.title
        try:
            status = row.samplelog_set.all().order_by('-changetimestamp')[0]
            d['last_status'] = str(status)
        except:
            d['last_status'] = ''

        output['rows'].append(d)


    output = makeJsonFriendly(output)

    return HttpResponse(json.dumps(output))


@mastr_users_only
def moveFile(request):
    output = {'success':'', 'newlocation':''}

    if not request.POST:
        return HttpResponseBadRequest("need to POST the request")

    target = request.POST.get('target', None)
    fname = request.POST.get('file', None)
    experiment_id = request.POST.get('experiment_id', None)
    rename = request.POST.get('rename', False)

    if not target or not fname or experiment_id is None:
        return HttpResponseBadRequest("need target and file params")

    exp = get_object_or_404(Experiment, id=experiment_id)

    if target == "experimentRoot":
        target = ""

    source = real_file_path(exp, fname)
    dest = real_file_path(exp, target)
    if rename:
        dest_filename = dest
    else:
        dest_filename = os.path.join(dest, os.path.split(source)[1])

    # don't allow overwriting of files
    if not (rename or os.path.isdir(dest)) or os.path.exists(dest_filename):
        return HttpResponseBadRequest("can't overwrite %s" % dest_filename)

    try:
        logger.debug("Renaming \"%s\" -> \"%s\"" % (source, dest))
        os.rename(source, dest_filename)
    except OSError, e:
        logger.exception("rename didn't work")
        output['msg'] = str(e)
    else:
        # rename the shared client files as well
        for client_file in exp.clientfile_set.filter(filepath__startswith=fname):
            endpart = client_file.filepath[len(os.path.dirname(fname)):].lstrip("/")
            filepath = os.path.join(target, endpart).lstrip("/")
            logger.debug("rename ClientFile %r -> %r" % (client_file.filepath, filepath))
            if os.path.exists(real_file_path(exp, filepath)):
                client_file.filepath = filepath
                client_file.save()
            else:
                logger.debug(" -> doesn't exist!")
                client_file.delete()

        output['success'] = True
        output['newlocation'] = dest

    return HttpResponse(json.dumps(output))

@mastr_users_only
def deleteFile(request):
    output = {'success':False}

    if not request.POST:
        return HttpResponseBadRequest("need to POST the request")

    target = request.POST.get('target', None)
    experiment_id = request.POST.get('experiment_id', None)

    if not target or experiment_id is None:
        return HttpResponseBadRequest("need target and experiment_id params")

    exp = get_object_or_404(Experiment, id=experiment_id)

    if target == "experimentRoot":
        return HttpResponseBadRequest("can't delete experiment directory");

    file_path = real_file_path(exp, target)

    try:
        if os.path.isdir(file_path):
            os.rmdir(file_path)
        else:
            os.unlink(file_path)
    except OSError, e:
        logger.exception("couldn't remove file/directory", e)
        output = {'success': False, 'msg': str(e)}
    else:
        # delete the shared files entries as well
        exp.clientfile_set.filter(filepath=target).delete()

        output['success'] = True

    return HttpResponse(json.dumps(output))


def _experiment_files(exp, sharedList, path):
    """
    Generates extjs tree nodes for a certain node id path under an
    experiment.

    It's complicated because experiment data is grafted onto the "Raw
    Data" folder and run data is grafted onto the "QC Data" folder.
    """
    def list_experiment_files(exp, path, prefix):
        return _fileList(exp.experiment_dir, path or "", sharedList, prefix=prefix)

    def list_run_files(exp, runid, path, prefix):
        run = get_object_or_404(Run, experiment_id=exp.id, id=runid)
        return _fileList(run.run_dir, path or "", sharedList, prefix="%s/%s" % (prefix, runid))

    def list_runs(exp, prefix):
        def make_entry(runid):
            return {
                'text': str(runid),
                'leaf': False,
                'id': "%s/%s" % (prefix, runid),
            }
        return map(make_entry, exp.run_set.values_list("id", flat=True))

    def list_other_files(exp, path, prefix):
        if path:
            files = []
        else:
            # graft experiment and runs data at toplevel
            files = [{
                'text': "Raw Data",
                'leaf': False,
                'id': "Raw Data",
            }, {
                'text': "QC Data",
                'leaf': False,
                'id': "QC Data",
            }]

        return files + _fileList(exp.other_files_dir, path, sharedList, prefix=prefix)

    routes = [
        (r"^(?P<prefix>Raw Data)(/(?P<path>.*))?", list_experiment_files),
        (r"^(?P<prefix>QC Data)/(?P<runid>\w+)(/(?P<path>.*))?", list_run_files),
        (r"^(?P<prefix>QC Data)", list_runs),
        (r"^(?P<prefix>)(experimentRoot)?(?P<path>.*)", list_other_files),
    ]

    for route, list_files in routes:
        m = re.match(route, path)
        if m:
            return list_files(exp, **m.groupdict())

    return []

def real_file_path(experiment, path):
    """
    Translates an imaginary files tree path name into a real path name
    in the repo for an experiment.
    """
    def get_experiment_path(path):
        return experiment.get_file_path(path or "")
    def get_run_path(path, runid):
        return experiment.run_set.get(id=runid).get_file_path(path or "")
    def get_all_runs_path():
        #return os.path.join(settings.REPO_FILES_ROOT, "runs")
        return None
    def get_other_path(path):
        return experiment.get_other_file_path(path or "")

    paths = [
        (r"^Raw Data(/(?P<path>.*))?", get_experiment_path),
        (r"^QC Data/(?P<runid>\d+)(/(?P<path>.*))?", get_run_path),
        (r"^QC Data$", get_all_runs_path),
        (r"^(?P<path>.*)", get_other_path),
    ]

    for route, get_base_path in paths:
        m = re.match(route, path)
        if m:
            return get_base_path(**m.groupdict())

    return None

@mastr_users_only
def experimentFilesList(request):
    args = request.GET
    if not (args and 'node' in args and 'experiment' in args):
        return HttpResponseBadRequest("need 'node' and 'experiment' args in a GET request")

    path = args['node']
    if path == 'experimentRoot':
        path = ''

    exp = get_object_or_404(Experiment, id=args['experiment'])
    exp.ensure_dir()

    sharedList = set(exp.clientfile_set.values_list("filepath", flat=True))

    files = _experiment_files(exp, sharedList, path)

    return HttpResponse(json.dumps(files))

@mastr_users_only
def runFilesList(request):
    args = request.GET

    if not (args and 'node' in args and 'run' in args):
        return HttpResponseBadRequest("need 'node' and 'run' args in a GET request")

    path = args['node']

    if path == 'runsRoot':
        path = ''

    run = get_object_or_404(Run, id=args['run'])

    runpath = run.ensure_dir()

    files = _fileList(runpath, path)

    return HttpResponse(json.dumps(files))

def _fileList(basepath, subdir="", sharedList=None, replace_subdir=None, prefix=None):
    """
    Returns a list of filenames in the directory basepath/subdir.
    The list of filenames are not prefixed with basepath. They are
    prefixed with prefix if it's given.

    If sharedList is given, then files which appear in that list will
    get the "checked" property.

    If replace_subdir is given, the returned list will have IDs
    starting with that instead of subdir.
    """
    logger.debug("_filelist(%r, %r, %r, %r)" % (basepath, subdir, sharedList, replace_subdir))

    output = []

    basepath = os.path.abspath(basepath)
    files_dir = os.path.join(basepath, subdir)

    #verify that there is no up-pathing hack happening
    if not files_dir.startswith(basepath) or not os.path.isdir(files_dir):
        return []

    for filename in sorted(os.listdir(files_dir)):
        filepath = os.path.join(files_dir, filename)

        if os.access(filepath, os.R_OK):
            nodeid = os.path.join(replace_subdir or subdir, filename)
            if prefix:
                nodeid = os.path.join(prefix, nodeid)
            entry = {
                'text': filename,
                'leaf': not os.path.isdir(filepath),
                'id': nodeid,
            }
            if sharedList is not None:
                entry["checked"] = entry["id"] in sharedList
            output.append(entry)

    return output

@mastr_users_only
def shareFile(request, *args):
    args = request.POST

    file = args['file']
    checked = args['checked'] == 'true'

    exp = get_object_or_404(Experiment, id=args['experiment_id'])
    exp.ensure_dir()

    if checked:
        client_file, created = ClientFile.objects.get_or_create(filepath=file, experiment=exp,
                                                                defaults={"sharedby": request.user})
        client_file.sharedby=request.user
        client_file.save()
    else:
        client_file = ClientFile.objects.filter(filepath=file, experiment=exp).delete()

    return HttpResponse(json.dumps({'success':True}))

def normalise_files(exp, files):
    logger.debug('files, pre normalise: %s' % (str(files)) )

    def get_path(filename):
        # Replace special value 'experimentDir' with the ''
        if filename == "experimentRoot":
            filename = ""

        # Add full path for every file
        return (real_file_path(exp, filename), filename)

    files = map(get_path, files)

    def ensure_sep(f):
        return f + os.path.sep if not f.endswith(os.path.sep) else f

    # If a parent dir has been selected we want to avoid adding subdirs and files included in it
    dirs = [ensure_sep(f) for (f, n) in files if os.path.isdir(f)]

    # Add each item that isn't contained in a dir
    for d in dirs:
        files = filter(lambda (f, n): f == d or not f.startswith(d), files)

    logger.debug('files, post normalise: %s' % (str(files)) )
    return files

@mastr_users_only
def packageFilesForDownload(request):
    args = request.REQUEST

    exp = get_object_or_404(Experiment, id=args['experiment_id'])
    exp.ensure_dir()

    package_type = args.get('package_type', '')
    if package_type not in ('zip', 'tgz', 'tbz2', 'tar'):
        package_type = 'zip'
    package_name = "experiment_%s_files.%s" % (exp.id, package_type)

    files = json.loads(args['files'])

    request.session[package_name] = {
        'experiment_id': exp.id,
        'files': normalise_files(exp, files)
    }
    return HttpResponse(json.dumps({
                'success':True,
                'package_name': package_name
        }))

def fileDownloadResponse(realfile, filename=None):
    if filename is None:
        filename = os.path.basename(realfile)
    response = FileResponse(open(realfile, "rb"), content_type='application/download')
    response['Content-Disposition'] = 'attachment;  filename=\"%s\"' % filename
    return response

@mastr_users_only
def downloadPackage(request):
    package_name = request.REQUEST.get('packageName')
    package_info = request.session.pop(package_name, {})
    files = package_info.get('files')
    experiment_id = package_info.get('experiment_id')
    experiment = get_object_or_404(Experiment, pk=experiment_id)

    zipped = zipstream.ZipFile(mode="w", compression=zipstream.ZIP_DEFLATED)
    for f in files:
        zipped.write(f[0])

    response = StreamingHttpResponse(zipped, content_type='application/download')
    response['Content-Disposition'] = 'attachment; filename={}'.format(package_name)
    return response

@mastr_users_only
def downloadFile(request, *args):
    args = request.REQUEST

    exp = get_object_or_404(Experiment, id=args['experiment_id'])
    exp.ensure_dir()

    filename = real_file_path(exp, args['file'])

    name = os.path.basename(filename)

    if os.path.isdir(filename):
        tmpfilename = "/tmp/madas-zip-"+name
        zipdir(filename, tmpfilename)
        filename = tmpfilename
        name = name + ".zip"

    return fileDownloadResponse(filename, name)

@mastr_users_only
def downloadSOPFileById(request, sop_id):
    from django.core.urlresolvers import reverse
    sop = StandardOperationProcedure.objects.get(id=sop_id)
    return HttpResponseRedirect(reverse('downloadSOPFile',
                kwargs={'sop_id': sop.id,
                        'filename': os.path.basename(sop.attached_pdf.name)}))

@mastr_users_only
def downloadSOPFile(request, sop_id, filename):
    sop = StandardOperationProcedure.objects.get(id=sop_id)
    if filename != os.path.basename(sop.attached_pdf.name):
        return HttpResponseForbidden()

    response = FileResponse(open(sop.attached_pdf.name, "rb"),
                            content_type='application/download')
    response['Content-Disposition'] = 'attachment;'
    return response

def downloadClientFile(request, filepath):
    pathbits = filepath.split('/')
    file_id = pathbits[0]

    cf = get_object_or_404(ClientFile, id=file_id, experiment__users=request.user)
    exp = cf.experiment
    exp.ensure_dir()

    filename = real_file_path(exp, cf.filepath)
    if len(pathbits) > 1:
        filename = os.path.join(filename, *pathbits[1:])

    logger.debug('filename is '+filename)

    if os.path.exists(filename):
        outputname = os.path.basename(filename)

        if os.path.isdir(filename):
            tmpfilename = "/tmp/madas-zip-"+outputname
            zipdir(filename, tmpfilename)
            filename = tmpfilename
            outputname = outputname + ".zip"

        from django.core.files import File
        wrapper = File(open(filename, "rb"))
        content_disposition = 'attachment;  filename=\"%s\"' % outputname
        response = HttpResponse(wrapper, content_type='application/download')
        response['Content-Disposition'] = content_disposition
        response['Content-Length'] = os.path.getsize(filename)
    else:
        response = HttpResponseNotFound("Cannot download file")

    return response

def downloadRunFile(request):
    args = request.REQUEST

    run = get_object_or_404(Run, id=args['run_id'])
    run.ensure_dir()

    filename = run.get_file_path(args['file'])
    name = os.path.basename(filename)

    logger.debug('download run file: ' + filename)

    if os.path.isdir(filename):
        tmpfilename = "/tmp/madas-zip-"+name
        zipdir(filename, tmpfilename)
        filename = tmpfilename
        name += ".zip"

    from django.core.files import File
    wrapper = File(open(filename, "rb"))
    content_disposition = 'attachment;  filename=\"%s\"' % name
    response = HttpResponse(wrapper, content_type='application/download')
    response['Content-Disposition'] = content_disposition
    response['Content-Length'] = os.path.getsize(filename)
    return response



@mastr_users_only
def uploadFile(request):

    args = request.POST

    ############# FILE UPLOAD ########################
    output = { 'success': True }

    try:
        experiment_id = args.get('experimentId', '');
        parent_folder = args.get('parentId', '');

        if not experiment_id:
            output = {"success": False, "msg": "Need to supply an experimentId" }
            return HttpResponse(json.dumps(output))

        logger.debug("parentId is %s" % parent_folder)

        #TODO handle file uploads - check for error values
        print request.FILES.keys()
        if request.FILES.has_key('attachfile'):
            f = request.FILES['attachfile']
            print '\tuploaded file name: ', f._get_name()
            translated_name = f._get_name().replace(' ', '_')
            print '\ttranslated name: ', translated_name
            _handle_uploaded_file(f, translated_name, experiment_id, parent_folder)
            attachmentname = translated_name
        else:
            print '\tNo file attached.'
    except Exception, e:
        logger.exception('Exception uploading file')
        output = { 'success': False }

    return HttpResponse(json.dumps(output))


def _handle_uploaded_file(f, name, experiment_id, parent_folder):
    '''Handles a file upload to the projects WRITABLE_DIRECTORY
       Expects a django InMemoryUpload object, and a filename'''
    logger.debug('*** _handle_uploaded_file: enter ***')
    retval = False
    try:
        logger.debug('exp is ' + experiment_id)

        exp = Experiment.objects.get(id=experiment_id)
        exppath = exp.ensure_dir()

        # clean folder name
        dest_dir = real_file_path(exp, parent_folder) if parent_folder else None
        dest_dir = dest_dir or exp.other_files_dir
        dest = os.path.join(dest_dir, name)

        logger.debug('writing to file: ' + dest)
        destination = open(dest, 'wb+')
        for chunk in f.chunks():
            destination.write(chunk)

        import grp
        groupinfo = grp.getgrnam(settings.CHMOD_GROUP)
        gid = groupinfo.gr_gid

        os.fchown(destination.fileno(), os.getuid(), gid)
        os.fchmod(destination.fileno(), stat.S_IRUSR|stat.S_IWUSR|stat.S_IRGRP|stat.S_IWGRP)

        destination.close()

        retval = True
    except Exception, e:
        retval = False
        logger.exception('Exception uploading file')
    logger.debug('*** _handle_uploaded_file: exit ***')
    return retval

@mastr_users_only
def newFolder(request):
    if not request.POST:
        return HttpResponseBadRequest("POST method only")

    if "experiment_id" not in request.POST or not request.POST.get("name", None):
        output = { "success": False, "msg": "Need to supply experiment_id and name" }
        return HttpResponse(json.dumps(output))

    parent = request.POST.get("parent", "")
    name = request.POST["name"]
    if parent:
        name = os.path.join(parent, name)

    try:
        exp = Experiment.objects.get(id=request.POST["experiment_id"])
    except Experiment.DoesNotExist:
        output = { "success": False, "msg": "Experiment does not exist" }
        return HttpResponse(json.dumps(output))
    else:
        path = real_file_path(exp, name)
        if path:
            try:
                os.mkdir(path)
            except OSError, e:
                logger.exception("Couldn't create new folder")
                output = { "success": False, "msg": str(e) }
            else:
                output = { "success": True }
        else:
            output = { "success": False, "msg": "Can't create a folder here." }

        return HttpResponse(json.dumps(output))

class CSVUploadView(View):
    http_method_names = ['post']
    @classmethod
    def get_file(cls, request):
        if request.FILES.has_key(cls.file_field_name):
            return request.FILES[cls.file_field_name]
        else:
            raise ClientLookupException("No file attached.")

class CSVUploadViewFile(CSVUploadView):
    file_field_name = 'samplecsv'

    @method_decorator(login_required)
    def post(self, request, *args, **kwargs):
        try:
            fd = CSVUploadViewFile.get_file(request)
            experiment = get_object_by_id_or_error(request, Experiment, 'experiment_id')
        except ClientLookupException, e:
            return HttpResponseBadRequest(e.message)

        status = self.handle_csv(fd, experiment)
        return HttpResponse(json.dumps(status))

    @classmethod
    def handle_csv(cls, csvfile, experiment):
        """
        Read a file object of CSV text and create samples from it.
        Returns a "success" dict suitable for returning to the client.
        """
        output = { "success": True,
                   "num_created": 0,
                   "num_updated": 0 }

        for sid, label, weight, comment in _read_uploaded_sample_csv(csvfile, output):
            # If a valid sample id is provided, try to update exising
            # sample, otherwise create a new one.
            samples = Sample.objects.filter(experiment=experiment, id=sid)
            if sid and len(samples) == 1:
                s = samples[0]
                output["num_updated"] += 1
            else:
                s = Sample()
                output["num_created"] += 1

            s.label = label
            if weight is not None:
                s.weight = weight
            s.comment = comment
            s.experiment = experiment
            s.save()

        return output

class CSVUploadViewCaptureCSV(CSVUploadView):
    file_field_name = 'runcapturecsv'

    @method_decorator(login_required)
    def post(self, request, *args, **kwargs):
        try:
            fd = CSVUploadViewCaptureCSV.get_file(request)
            experiment = get_object_by_id_or_error(request, Experiment, 'experiment_id')
            machine = get_object_by_id_or_error(request, NodeClient, 'machine_id')
            method = get_object_by_id_or_error(request, InstrumentMethod, 'method_id')
            title = request.POST.get('title', None)
            if title is None:
                raise ClientLookupException("need title param")
        except ClientLookupException, e:
            return HttpResponseBadRequest(e.message)

        status = self.handle_csv(fd, experiment, machine, method, title, request.user)
        return HttpResponse(json.dumps(status))

    @classmethod
    def handle_csv(cls, csvfile, experiment, machine, method, title, user):
        """
        Read a file object of CSV text and create RunSample instances from it.
        Returns a "success" dict suitable for returning to the client.
        """
        run = Run(
            method = method,
            creator = user,
            title = title,
            experiment = experiment,
            machine = machine,
            state = RUN_STATES.NEW[0],
            )
        run.save()

        output = {
            "success" : True,
            "num_created" : 0
        }

        try:
            # sample_id ignored for now.. probably could be got rid of
            run_samples = []
            for sample_id, filename in _read_uploaded_run_capture_csv(csvfile, output):
                rs = RunSample(run=run, filename=filename)
                output['num_created'] += 1
                rs.save()
        except ClientLookupException, e:
            output = e.output
        except Exception, e:
            output = {
                "success" : False,
                "msg" : str(e)
            }

        # examine output, if we've failed destroy the Run we created
        if not output['success']:
            run.delete()
        return output

def _read_uploaded_run_capture_csv(csvfile, output):
    """
    this generates (sample_id, filename) tuples from
    the CSV text.
    """
    required = ["FILENAME"]
    cleanup = lambda *args: args
    return _read_csv(csvfile, output, required, cleanup)

def _read_uploaded_sample_csv(csvfile, output):
    """
    This generates (sample_id, label, weight, comment) triples from
    the CSV text.
    """
    maybe_decimal = lambda v: Decimal(v) if v else None
    required = ["LABEL", "WEIGHT", "COMMENT"]
    cleanup = lambda label, weight, comment: (label, maybe_decimal(weight), comment)
    return _read_csv(csvfile, output, required, cleanup)

def _read_csv(csvfile, output, column_names, convert_fn):
    """
    The `output` dict is updated if there are parse
    errors, etc. In addition to required cols in `column_names` and
    optional ID column is looked for. `convert_fn` is called with values
    in the same order as `column_names` for any casts, cleanup requried,
    before the values are yielded.
    """
    max_error = 10
    def add_format_error(output, msg):
        "This function makes a note in the output dict that a value was wrong"
        invalid_lines = output.setdefault("invalid_lines", [])
        if len(invalid_lines) < max_error:
            invalid_lines.append(num)
        else:
            output["max_error"] = max_error
        output.update({ "success": False, "msg": msg })

    try:
        snuff = csvfile.read(1024)
        csvfile.seek(0)

        if len(snuff.strip()) == 0:
            output.update({ "success": False, "msg": "File is empty" })
            raise StopIteration

        dialect = csv.excel
        has_header = False
        if len(snuff.splitlines()) > 1:
            try:
                has_header = csv.Sniffer().has_header(snuff)
            except csv.Error:
                maybe_header = [t.strip().upper() for t in snuff.splitlines()[0].split(dialect.delimiter)]
                nfound = 0
                for column in column_names:
                    if column in maybe_header:
                        nfound += 1
                if nfound > 0:
                    has_header = True

        # Switch on "universal newlines" mode for the uploaded file,
        # so unix, dos, and mac line endings are all supported.
        csvfile = _universal_newlines(csvfile)

        data = csv.reader(csvfile, dialect=dialect)

        if has_header:
            header = [h.upper().strip() for h in data.next()]
            def find(name):
                try:
                    return header.index(name)
                except ValueError:
                    return -1
            cols = map(find, column_names)
            start_line = 2

            # check for required columns
            missing = [column_names[i] for i,j in enumerate(cols) if j < 0]
            if missing:
                missing = ("Column %s is missing" % m for m in missing)
                output.update({ "success": False,
                                "msg": ", ".join(missing) })
                raise StopIteration

            # check for the optional id column. The javascript CSV
            # export puts a hash sign before it.
            id_col = find("ID")
            if id_col < 0:
                id_col = find("# ID")
        else:
            id_col = -1
            cols = range(len(column_names))
            start_line = 1

        def parse_id(row):
            "Converts the optional id cell to either an int or None"
            if id_col < 0 or not row[id_col].strip():
                return None
            else:
                return int(row[id_col])

        for num, row in enumerate(data, start_line):
            if len(row) == 0:
                continue
            try:
                yield [parse_id(row)] + list(convert_fn(*[row[t] for t in cols]))
            except ValueError, e:
                add_format_error(output, "Incorrectly formatted id integer")
            except DecimalException, e:
                add_format_error(output, "Incorrectly formatted decimal number")

    except EnvironmentError, e:
        logger.exception('Exception uploading file')
        output.update({ "success": False,
                        "msg": "Exception uploading file: %s" % e })

def _universal_newlines(csvfile):
    """
    This wraps a django uploaded file object using the python 2.6 io
    library so that all newline formats are understood.
    """
    if isinstance(csvfile, TemporaryUploadedFile):
        # This csv upload was a largish file so Django put it on
        # the filesystem -- reopen the file.
        return io.open(csvfile.temporary_file_path(), newline=None)
    else:
        # This csv upload was smaller than the limit so Django
        # stored the data in memory -- make a copy.
        return io.StringIO(smart_text(csvfile.read(), errors='ignore'), newline=None)

@mastr_users_only
def sample_class_enable(request, id):

    if request.GET:
        args = request.GET
    else:
        args = request.POST

    sc = SampleClass.objects.get(id=id)

    if sc.enabled:
        sc.enabled = False
    else:
        sc.enabled = True

    sc.save()

    return recordsSampleClasses(request, sc.experiment.id)

def json_error(msg='Unknown error'):
    return HttpResponse(json.dumps({'success': False, 'msg': msg}))

@mastr_users_only
def get_rule_generator(request):
    rulegen_id = request.REQUEST.get('id')
    try:
        rg = RuleGenerator.objects.get(pk=rulegen_id)
    except ObjectDoesNotExist:
        return json_error("Rulegenerator with id %s doesn't exist" % rulegen_id)

    return HttpResponse(json.dumps({'success':True, 'rulegenerator': rulegenerators.convert_to_dict(rg)}))

@mastr_users_only
def create_rule_generator(request):

    name = request.POST.get('name', "Unnamed")
    description = request.POST.get('description', "This rule generator was not given a description")
    accessibility = request.POST.get('accessibility')
    apply_sweep_rule = request.POST.get('apply_sweep_rule')
    if apply_sweep_rule is not None:
        apply_sweep_rule = True if apply_sweep_rule == 'true' else False
    startblockvars = json.loads(request.POST.get('startblock', []))
    sampleblockvars = json.loads(request.POST.get('sampleblock', []))
    endblockvars = json.loads(request.POST.get('endblock', []))

    try:
        message = rulegenerators.create_rule_generator(name,
            description,
            accessibility,
            request.user,
            apply_sweep_rule,
            request.user.PrimaryNode,
            startblockvars,
            sampleblockvars,
            endblockvars)
        return HttpResponse(json.dumps({'success':True,'msg':message}))
    except Exception, e:
        return HttpResponse(json.dumps({'success':False, 'msg' : str(e)}))

@mastr_users_only
def edit_rule_generator(request):
    rg_id = request.POST.get('rulegen_id')
    if rg_id is None:
       return json_error('Rule Generator id not submitted')

    description = request.POST.get('description', None)
    apply_sweep_rule = request.POST.get('apply_sweep_rule')
    if apply_sweep_rule is not None:
        apply_sweep_rule = True if apply_sweep_rule == 'true' else False
    name = request.POST.get('name', None)
    accessibility = request.POST.get('accessibility', None)
    startblockvars = json.loads(request.POST.get('startblock', 'null'))
    sampleblockvars = json.loads(request.POST.get('sampleblock', 'null'))
    endblockvars = json.loads(request.POST.get('endblock', 'null'))
    state = json.loads(request.POST.get('state', 'null'))

    try:
        message = rulegenerators.edit_rule_generator(rg_id, request.user,
                                                        name=name,
                                                        description=description,
                                                        accessibility=accessibility,
                                                        apply_sweep_rule=apply_sweep_rule,
                                                        startblock=startblockvars,
                                                        sampleblock=sampleblockvars,
                                                        endblock=endblockvars,
                                                        state=state)
        return HttpResponse(json.dumps({'success':True, 'msg': message}))
    except Exception, e:
        return HttpResponse(json.dumps({'success':False, 'msg': str(e)}))

@mastr_users_only
def create_new_version_of_rule_generator(request):
    rg_id = request.REQUEST['id']
    try:
        new_id = rulegenerators.create_new_version_of_rule_generator(rg_id, request.user)
    except Exception, e:
        raise Exception("Couldn't create new version of rule generator")
    return HttpResponse(json.dumps({'success':True, 'new_id': new_id}))

@mastr_users_only
def clone_rule_generator(request):
    rg_id = request.REQUEST['id']
    try:
        new_id = rulegenerators.clone_rule_generator(rg_id, request.user)
    except Exception, e:
        raise Exception("Couldn't clone rule generator")
    return HttpResponse(json.dumps({'success':True, 'new_id': new_id}))

@mastr_users_only
def generate_worklist(request, run_id):
    run = Run.objects.get(id=run_id)
    from runbuilder import RunBuilder, RunBuilderException

    rb = RunBuilder(run)
    try:
        rb.generate()
    except RunBuilderException, e:
        # Shortcut on Error!
        return HttpResponse(str(e), content_type="text/plain")
    else:
        output = {'success': True}

    return HttpResponse(json.dumps(output))

@mastr_users_only
def display_worklist(request, run_id):
    run = get_object_or_404(Run, id=run_id)
    samples = run.runsample_set.order_by('sequence')

    if run.method.template == "csv":
        return _display_worklist_csv(request, run, samples)

    return HttpResponseServerError("InstrumentMethod has unknown template")

def _display_worklist_csv(request, run, samples):
    """
    Returns a HttpResponse in CSV format with a worklist.
    I assume this CSV format is understood by the MassHunter software.
    """
    response = HttpResponse(content_type='text/plain; charset=utf-8')

    w = csv.writer(response)

    for sample in samples:
        row = [request.user.email, run.machine.default_data_path,
               sample.filename,
               run.method.method_path, run.method.method_name,
               sample.sample_name]
        w.writerow(map(smart_bytes, row))

    return response

@mastr_users_only
def mark_run_complete(request, run_id):
    samples = RunSample.objects.filter(run__id=run_id)

    for sample in samples:
        sample.complete = True
        sample.save()

    run = Run.objects.get(id=run_id)
    run.state = RUN_STATES.COMPLETE[0]
    run.save()


    try:
        e = FixedEmailMessage(subject='MASTR-MS Run ('+run.title+') Complete', body='Run ('+run.title+') has been marked as complete', from_email = settings.RETURN_EMAIL, to = [run.creator.email])
        e.send()
    except e:
        pass

    return HttpResponse(json.dumps({ "success": True }), content_type="text/plain")


def select_widget_json(authenticated=False, authorized=False, main_content_function=None, success=False, input=""):

    output = {}
    output["authenticated"] = authenticated
    output["authorized"] = authorized

    output["response"] = {"value": {"items": input,
                                    "total_count":len(input),
                                    "version":1
                                    }}
    output["success"] = success
    return json.dumps(output)


@mastr_users_only
def add_samples_to_run(request):
    '''Takes a run_id and a list of sample_ids and adds samples to the run after checking permissions etc.'''
    logger.debug("add_samples_to_run is entered")
    if request.method == 'GET':
        return HttpResponseNotAllowed(['POST'])

    run_id = request.POST.get('run_id', None)
    if not run_id:
        return HttpResponseBadRequest("No run_id provided.\n")

    try:
        run = Run.objects.get(id=run_id)
    except ObjectDoesNotExist:
        return HttpResponseNotFound("Run with id %s not found.\n" % run_id)

    sample_id_str = request.POST.get('sample_ids', None)
    if not sample_id_str:
        return HttpResponseBadRequest("No sample_ids provided.\n")

    sample_ids = [int(X) for X in sample_id_str.split(',')]
    #The following generated queryset will be in databaseid order, not
    #in the order specified by the sample_ids list. We will need to
    #reorder it before we send it to the run for processing.
    #we do this later (see below)
    queryset = Sample.objects.filter(id__in=sample_ids)
    logger.debug("Samples to add to run: %s" % (str(sample_ids) ) )
    if len(queryset) != len(sample_ids):
        return HttpResponseNotFound("At least one of the samples can not be found.\n")

    # check that each sample is permitted
    # user could be in experiment OR be a project manager
    # I have done this with two Q objects OR'ed together.
    # The alternative approach would be to do it as a nested if:
    # only check that the user is in the experiment access list IF the user
    # isn't already a PM.

    samples = Sample.objects.filter(Q(experiment__users=request.user)|Q(experiment__project__managers=request.user))
    allowed_set = set(list(samples))
    qs_set = set(list(queryset))
    if not qs_set.issubset(allowed_set):
        return HttpResponseForbidden('Some samples do not belong to this user.\n')

    # check that each sample is valid
    for s in queryset:
        if not s.is_valid_for_run():
            logger.debug('Sample %d not valid for run' % (s.id))
            return HttpResponseNotFound("Run NOT created as sample (%s, %s) does not have sample class or its class is not enabled.\n" % (s.label, s.experiment))
        else:
            logger.debug('Sample %d valid for run' % (s.id))

    # by the time you we get here we should have a valid run and valid samples
    #the samples aren't necessarily in the correct order though, because of the call to filter (they are returned in order of database id, not the sequence given in the passed in id list)
    #so we will make a list that is in the correct order
    sampleslist = []
    for id in sample_ids:
        try:
            #any sample not found in the qs is ignored by
            #this try catch
            sampleslist.append(queryset.get(id=id) )
        except Exception, e:
            logger.debug("Adding sample %d to list failed: %s" % (id, e))
    logger.debug("Actually adding the samples to the samplelist")
    run.add_samples(sampleslist)
    logger.debug("Finished adding the samples to the samplelist")

    return HttpResponse()

@mastr_users_only
def add_samples_to_class(request):
    '''Takes a run_id and a list of sample_ids and adds samples to the run after checking permissions etc.'''

    if request.method == 'GET':
        return HttpResponseNotAllowed(['POST'])

    class_id = request.POST.get('class_id', None)
    if not class_id:
        return HttpResponseBadRequest("No class_id provided.\n")

    try:
        sampleclass = SampleClass.objects.get(id=class_id)
    except ObjectDoesNotExist:
        return HttpResponseNotFound("Class with id %s not found.\n" % class_id)

    sample_id_str = request.POST.get('sample_ids', None)
    if not sample_id_str:
        return HttpResponseBadRequest("No sample_ids provided.\n")

    sample_ids = [int(X) for X in sample_id_str.split(',')]
    queryset = Sample.objects.filter(id__in=sample_ids)

    if len(queryset) != len(sample_ids):
        return HttpResponseNotFound("At least one of the samples can not be found.\n")

    # check that each sample is permitted
    samples = Sample.objects.filter(experiment__users=request.user)
    allowed_set = set(list(samples))
    qs_set = set(list(queryset))
    if not qs_set.issubset(allowed_set):
        return HttpResponseForbidden('Some samples do not belong to this user.\n')

    # by the time you we get here we should have a valid run and valid samples
    for sample in samples:
        sample.sample_class = sampleclass
        sample.save()

    return HttpResponse()

@mastr_users_only
def remove_samples_from_run(request):
    '''Takes a run_id and a list of sample_ids and remove samples from the run after checking permissions etc.'''

    if request.method == 'GET':
        return HttpResponseNotAllowed(['POST'])

    run_id = request.POST.get('run_id', None)
    if not run_id:
        return HttpResponseBadRequest("No run_id provided.\n")

    try:
        run = Run.objects.get(id=run_id)
    except ObjectDoesNotExist:
        return HttpResponseNotFound("Run with id %s not found.\n" % run_id)

    sample_id_str = request.POST.get('sample_ids', None)
    if not sample_id_str:
        return HttpResponseBadRequest("No sample_ids provided.\n")

    sample_ids = [int(X) for X in sample_id_str.split(',')]
    queryset = Sample.objects.filter(id__in=sample_ids)

    # do it
    run.remove_samples(queryset)

    return HttpResponse()

def report_error(request):
    success = True
    try:
        subject = 'MADAS client-side error'
        message = """
A client-side error has been reported from IP: %s.

Additional notes entered by the user:
%s

Details of the error:

%s""" % (request.META.get('REMOTE_ADDR'), request.REQUEST.get('notes'), request.REQUEST.get('details'))

        mail_admins(subject, message, fail_silently=False)
    except:
        success = False
        raise

    output = {}
    output["success"] = success
    return HttpResponse(json.dumps(output))
