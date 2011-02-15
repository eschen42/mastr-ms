from madas.repository.models import RunSample, RUN_STATES, SampleNotInClassException, InstrumentSOP
from django.http import HttpResponse

class RunBuilder(object):
    def __init__(self, run):
        self.run = run
        # for now, just load up a default instrument SOP (don't touch the database)
        self.instrumentSOP = InstrumentSOP()

    def validate(self):
        for sample in self.run.samples.all():
            sample.run_filename(self.run)
            
        return True
        
    def layout(self):
        #validate first, this will throw an exception if failed
        self.validate()
        
        layout = GCRunLayout(self.run, self.instrumentSOP)
        layout.perform_layout()
        print layout.layout
        #end result of a perform_layout is new RunSample entries to represent all other line items
        
    def generate(self, request):
        try:
            print 'validation started'
            self.validate()
            #if the validate fails we throw an exception
            print 'validation finished'
        except Exception, e:
            return 'Run validation error ' + str(e)
        except SampleNotInClassException, e:
            return 'Samples in the run need to be in sample classes before they can be used in a run'
   
        if self.run.state == RUN_STATES.NEW[0]:
            self.layout()
        
        from mako.template import Template
        
        mytemplate = Template(self.run.method.template)
        #we need to set a non ascii output encoding,
        #because we have unicode chars that break 
        #mako (>128)
        mytemplate.output_encoding = "utf-8"

        #create the variables to insert
        render_vars = {'username':request.user.username,'run':self.run,'runsamples':RunSample.objects.filter(run=self.run).order_by('sequence')}
        
        print 'writing filenames'
        #write filenames into DB
        for rs in RunSample.objects.filter(run=self.run):
            if rs.type == 0:
                rs.filename = rs.sample.run_filename(self.run)
                rs.save()
        print 'finished writing filenames'
        #mark the run as in-progress and save it
        if self.run.state == RUN_STATES.NEW[0]:
            self.run.state = RUN_STATES.IN_PROGRESS[0] 
            self.run.save()
        
        #render
        response = HttpResponse(content=mytemplate.render(**render_vars), content_type='text/html; charset=utf-8')

        return response
        
class TrayLayoutDelegate(object):
    def __init__(self):
        self.vials = 98
        self.trays = 1
        self.sample_positions = range(1,84)
        self.reagent_positions = [98]
        self.solvent_positions = [97]
        self.tms_positions = range(89,95)
        self.sweep_positions = [85,86]
        
class RunGroup(object):
    def __init__(self, instrumentSOP):
        self.samples = []
        self._layout = []
        self.sweeps = []
        self.pooled_qcs = []
        self.run = None
        self.instrumentSOP = instrumentSOP
        
    def add_pooled_QCs(self):
        #add pooled QCs into group
        if len(self.samples) >= self.instrumentSOP.split_size:
            print 'add pbqc randomly'
            
            qc = RunSample()
            qc.run = self.run
            qc.type = 2
            qc.save()
            qc.filename = u'pbqc_%s-%s.d' % (self.run.id, qc.id)
            qc.save()
            
            self.pooled_qcs = [qc]
            import random
            randloc = random.randint(0,len(self.samples))
            self.samples.insert(randloc,qc)
            
    def layout(self):
        print 'layout'
        if len(self._layout) == 0:
            return self.samples
        else:
            return self._layout

        
class GenericRunLayout(object):
    def __init__(self, run, instrumentSOP):
        self.run = run
        self.instrumentSOP = instrumentSOP
        
    def clear(self):
        print 'clearing'
        self.groups = []
        self.layout = []
        self.solvents = []
        self.reagents = []
        self.intrument_qcs = []
        self.pooled_qcs = []
        # and purge these meta samples from the RunSample table for this run
        RunSample.objects.filter(run=self.run,type__gt=0).delete()
        
        #populate with RunSample entries that are now only sourced from Samples
        self.samples = []
        runsamples = RunSample.objects.filter(run=self.run)
        for rs in runsamples:
            self.samples.append(rs)
        
    def perform_layout(self):
        print 'perform_layout'
        #process self.samples, following general rules to layout the rest
        self.clear()

        self.randomize()
        self.split(self.instrumentSOP.split_size)
        map(RunGroup.add_pooled_QCs, self.groups)
        self.interleave_solvents()
        self.add_pooled_QC_padding()
        self.add_reagent_blank()
        self.add_solvent_padding()
        self.update_sequences()
        
    def randomize(self):
        #randomizes all items in the layout
        print 'randomize'
        import random
        random.shuffle(self.samples)
        self.layout = self.samples
        
    def split(self, length):
        print 'split'
        #if 20 or less samples, do not split
        if len(self.samples) <= self.instrumentSOP.split_threshhold:
            b = RunGroup(self.instrumentSOP)
            b.samples = self.samples[:]
            b.run = self.run
            self.groups = [b]
        else:
            #split samples into groups
            f = lambda A, n=length: [A[i:i+n] for i in range(0, len(A), n)]
            grps = f(self.samples)
            
            self.groups = []
            for a in grps:
                b = RunGroup(self.instrumentSOP)
                b.samples = a
                b.run = self.run
                self.groups.append(b)

    def interleave_solvents(self):
        print 'interleave solvents'
        #adds solvents between groups in layout
        self.layout = []
        
        for counter in range(len(self.groups) - 1):
            self.layout.extend(self.groups[counter].layout())
            #create a solvent entry
            solvent = RunSample()
            solvent.run = self.run
            solvent.type = 4
            solvent.save()
            solvent.filename = u'solvent_%s-%s.d' % (self.run.id, solvent.id)
            solvent.save()
            
            self.solvents.append(solvent)
            self.layout.append(solvent)
            
        self.layout.extend(self.groups[len(self.groups) - 1].layout())
        
    def add_pooled_QC_padding(self):
        print 'add pbqc padding'
        #add PBQCs at beginning and end of layout
        qc = RunSample()
        qc.run = self.run
        qc.type = 2
        qc.save()
        qc.filename = u'pbqc_%s-%s.d' % (self.run.id, qc.id)
        qc.save()
        
        self.layout.insert(0,qc)
        
        qc = RunSample()
        qc.run = self.run
        qc.type = 2
        qc.save()
        qc.filename = u'pbqc_%s-%s.d' % (self.run.id, qc.id)
        qc.save()
        
        self.layout.append(qc)
        
    def add_reagent_blank(self):
        print 'add reagent blank'
        #add a blank to the beginning of the layout
        rb = RunSample()
        rb.run = self.run
        rb.type = 5
        rb.save()
        rb.filename = u'reagent_%s-%s.d' % (self.run.id, rb.id)
        rb.save()
        
        self.layout.insert(0,rb)
        
    def add_solvent_padding(self):
        print 'add solvent padding'
        #add 1 solvent at beginning, 2 at end
        solvent = RunSample()
        solvent.run = self.run
        solvent.type = 4
        solvent.save()
        solvent.filename = u'solvent_%s-%s.d' % (self.run.id, solvent.id) 
        solvent.save()
        
        self.layout.insert(0,solvent)
        
        solvent = RunSample()
        solvent.run = self.run
        solvent.type = 4
        solvent.save()
        solvent.filename = u'solvent_%s-%s.d' % (self.run.id, solvent.id)
        solvent.save()
        
        self.layout.append(solvent)
        
        solvent = RunSample()
        solvent.run = self.run
        solvent.type = 4
        solvent.save()
        solvent.filename = u'solvent_%s-%s.d' % (self.run.id, solvent.id)
        solvent.save()
        
        self.layout.append(solvent)
        
    def update_sequences(self):
        print 'updating sequences'
        count = 1
        for rs in self.layout:
            rs.sequence = count
            rs.save()
            count = count + 1
        
class GCRunLayout(GenericRunLayout):
    def perform_layout(self):
        #override generic layout to add instrument QC padding and sweeps
        self.clear()
                
        self.randomize()
        self.split(self.instrumentSOP.split_size)
        map(RunGroup.add_pooled_QCs, self.groups)
        self.interleave_solvents()
        self.add_pooled_QC_padding()
        self.add_instrument_QC_padding()
        self.add_reagent_blank()
        self.add_solvent_padding()
        self.interleave_sweeps()
        self.update_sequences()

    def add_instrument_QC_padding(self):
        print 'add IQC padding'
        qc = RunSample()
        qc.run = self.run
        qc.type = 3
        qc.save()
        qc.filename = u'iqc_%s-%s.d' % (self.run.id, qc.id)
        qc.save()
        
        self.layout.insert(0,qc)
        
        qc = RunSample()
        qc.run = self.run
        qc.type = 3
        qc.save()
        qc.filename = u'iqc_%s-%s.d' % (self.run.id, qc.id)
        qc.save()
        
        self.layout.append(qc)
        
        
    def interleave_sweeps(self):
        print 'interleave sweeps'
        #insert sweeps after each sample
        self._layout = []
        self.sweeps = []
        
        for counter in range(len(self.layout)):
            self._layout.append(self.layout[counter])
            
            #add a sweep after any sample, standard, pbqc or iqc
            if self.layout[counter].type < 4:
                #create a sweep entry
                sweep = RunSample()
                sweep.run = self.run
                sweep.type = 6
                sweep.save()
                sweep.filename = u'sweep_%s-%s.d' % (self.run.id, sweep.id)
                sweep.save()
                
                self.sweeps.append(sweep)
                self._layout.append(sweep)
            
        self.layout = self._layout
