//run.js needs to manage a few things
//the list of runs
//the current run being manipulated
//the current samples in the run being manipulated

MA.CreateNewRun = function() {
    Ext.getCmp('runlistview').clearSelections();
    Ext.getCmp("runDetails").createRun();
};


MA.ReloadRunStores = function(spec) {
    var params = {'experiment__id': MA.ExperimentController.currentId()};
    var options = {'params': params};
    if (spec && spec.callback) {
        options.callback = spec.callback;
    }
    newRunsStore.load(options);
    experimentRunStore.load({'params': params});
};

MA.RunCmpRowSelect = function(view, nodes) {
    if (nodes.length == 0) {
//        Ext.getCmp("runDetails").createRun();
    } else {
        var r = view.getSelectedRecords()[0];

        Ext.getCmp("runDetails").selectRun(r);
    }
};

MA.RunDeleteCallback = function() {
    MA.ReloadRunStores();
    MA.ClearCurrentRun();
};

MA.ClearCurrentRun = function() {
    Ext.getCmp("runDetails").clearRun();
};

// Create a component we can use both here and from the run list.
MA.RunDetail = Ext.extend(Ext.form.FormPanel, {
    constructor: function(config, mode) {
        var self = this;
        this.allowCreatingNewRun = false;
        this.allowAddingSamples = false;
        if (Ext.isDefined(mode)) {
            if (mode.allowCreatingNewRun) {
                this.allowCreatingNewRun = true;
            }
            if (mode.allowAddingSamples) {
                this.allowAddingSamples = true;
            }
        }

        this.pendingSampleSelModel = new Ext.grid.CheckboxSelectionModel({ width: 25 });
        this.sampleSelModel = new Ext.grid.CheckboxSelectionModel({ width: 25 });

        this.pendingSampleFields = [
            { name: "id", type: "int" }
        ];

        this.pendingSampleStore = new Ext.data.ArrayStore({
            fields: this.pendingSampleFields,
            idIndex: 0,
            sortInfo: {
                field: "id",
                direction: "DESC"
            }
        });

        this.PendingSampleRecord = Ext.data.Record.create(this.pendingSampleFields);

        this.runSampleStore = new Ext.data.JsonStore({
            autoLoad: false,
            remoteSort: true,
            restful: true,
            url: wsBaseUrl + "recordsSamplesForRun"
        });

        var defaultConfig = {
            autoScroll: true,
            labelWidth: 160,
            items: [
                {
                    xtype: 'hidden',
                    itemId: 'id'
                },
                new Ext.form.Label({
                    fieldLabel: "State",
                    itemId: "state",
                    style: "position: relative; top: 3px;",
                    text: renderRunState(0)
                }),
                new Ext.form.Label({
                    fieldLabel: "Progress",
                    itemId: "progress",
                    style: "position: relative; top: 3px;",
                    text: ''
                }),
                {
                    fieldLabel: 'Title',
                    xtype: 'textfield',
                    itemId: 'title',
                    value: 'New Untitled Run',
                    allowBlank: false
                },
                new Ext.form.ComboBox({
                    fieldLabel: 'Instrument method',
                    itemId: 'method',
                    name: 'method',
                    editable: false,
                    forceSelection: true,
                    displayField: 'value',
                    valueField: 'key',
                    hiddenName: 'method',
                    lazyRender: true,
                    allowBlank: false,
                    typeAhead: false,
                    triggerAction: 'all',
                    listWidth: 230,
                    width: 200,
                    store: methodStore
                }),
                new Ext.form.ComboBox({
                    fieldLabel: 'Machine',
                    itemId: 'machine',
                    name: 'machine',
                    editable: false,
                    forceSelection: true,
                    displayField: 'station_name',
                    valueField: 'id',
                    hiddenName: 'machine',
                    lazyRender: true,
                    allowBlank: false,
                    typeAhead: false,
                    triggerAction: 'all',
                    mode: 'local',
                    listWidth: 230,
                    width: 200,
                    store: machineStore,
                    itemSelector: 'div.search-item',
                    tpl: new Ext.XTemplate(
                    '<tpl for="."><div style="padding:8px;padding-top:5px;padding-bottom:5px;border-bottom:1px solid #ccc;" class="search-item">',
                    '{station_name}<br /><span style="color:#666;">{organisation_name} > {site_name}</span>',
                    '</div></tpl>'
                    )
                }),
                new Ext.form.ComboBox({
                    fieldLabel: 'Rule Generator',
                    itemId: 'rule_generator',
                    name: 'rule_generator',
                    editable: false,
                    forceSelection: true,
                    displayField: 'full_name',
                    valueField: 'id',
                    hiddenName: 'rule_generator',
                    lazyRender: true,
                    allowBlank: false,
                    typeAhead: false,
                    triggerAction: 'all',
                    listWidth: 230,
                    width: 200,
                    queryaction: 'all',
                    lastQuery: '',
                    store: enabledRuleGeneratorStore,
                    mode: 'local'
                }), {
                    //xtype: 'compositefield', // breaks getComponent() !
                    xtype: 'container',
                    itemId: 'methods',
                    layout: 'hbox',
                    fieldLabel: 'Number/Order of Methods',
                    items: [
                        new Ext.form.NumberField({
                            fieldLabel: 'Number of Methods',
                            itemId: 'number_of_methods',
                            name: 'number_of_methods',
                            allowDecimals: false,
                            allowNegative: false,
                            maxValue: 100,
                            minValue: 2,
                            width: 50
                        }),
                        new Ext.form.ComboBox({
                            fieldLabel: 'Order of Methods',
                            itemId: 'order_of_methods',
                            name: 'order_of_methods',
                            editable: false,
                            forceSelection: true,
                            displayField: 'value',
                            valueField: 'key',
                            hiddenName: 'order_of_methods',
                            lazyRender: true,
                            typeAhead: false,
                            triggerAction: 'all',
                            mode: 'local',
                            listWidth: 150,
                            width: 150,
                            store: new Ext.data.ArrayStore({
                                id: 0,
                                fields: ['key', 'value'],
                                data: [['', 'None'], [1, 'resampled vial'], [2, 'individual vial']]
                            })
                        })
                    ]
                },{
                    fieldLabel: 'Samples to Add',
                    itemId: 'samplesToAdd',
                    xtype: 'grid',
                    width: 310,
                    height: 120,
                    store: this.pendingSampleStore,
                    loadMask: true,
                    columns: [
                        self.pendingSampleSelModel,
                        {header: "ID", dataIndex: 'id', sortable: true}
                    ],
                    viewConfig: {
                        forceFit: true
                    },
                    sm: self.pendingSampleSelModel,
                    style: 'background:white;',
                    autoScroll: true,
                    reserveScrollOffset: true,
                    bbar: {
                        items: [
                            {
                                text: 'Remove Samples',
                                cls: 'x-btn-text-icon',
                                icon: 'static/images/delete.png',
                                listeners: {
                                    'click': function(e) {
                                        //save changes to selected entries
                                        if (self.pendingSampleSelModel.getCount() > 0) {
                                            var selections = self.pendingSampleSelModel.getSelections();

                                            if (!Ext.isArray(selections)) {
                                                selections = [selections];
                                            }

                                            var ids = [];
                                            for (var idx in selections) {
                                                if (!Ext.isObject(selections[idx])) {
                                                    continue;
                                                }

                                                self.pendingSampleStore.remove(selections[idx]);
                                            }
                                        }
                                    }
                                }
                            }
                        ]
                    }
                },
                {
                    fieldLabel: 'Samples',
                    xtype: 'grid',
                    width: 310,
                    itemId: 'samples',
                    height: 120,
                    store: this.runSampleStore,
                    loadMask: true,
                    columns: [
                        self.sampleSelModel,
                        {header: "ID", dataIndex: 'id', sortable: false, menuDisabled: true},
                        {header: "Label", dataIndex: 'label', sortable: false, menuDisabled: true},
                        {header: "Class", dataIndex: 'sample_class__unicode', sortable: false, menuDisabled: true},
                       { header: "Seq", sortable: false, dataIndex: 'sample_class_sequence', menuDisabled: true}
                    ],
                    viewConfig: {
                        forceFit: true
                    },
                    sm: self.sampleSelModel,
                    style: 'background:white;',
                    autoScroll: true,
                    reserveScrollOffset: true,
                    bbar: {
                        items: [
                            {
                                text: 'Remove Samples',
                                cls: 'x-btn-text-icon',
                                icon: 'static/images/delete.png',
                                itemId: 'removeBtn',
                                listeners: {
                                    'click': function(e) {
                                        //save changes to selected entries
                                        if (self.sampleSelModel.getCount() > 0) {
                                            var selections = self.sampleSelModel.getSelections();

                                            if (!Ext.isArray(selections)) {
                                                selections = [selections];
                                            }

                                            var ids = [];
                                            for (var idx in selections) {
                                                if (!Ext.isObject(selections[idx])) {
                                                    continue;
                                                }

                                                ids.push(selections[idx].data.id);
                                            }

                                            var saver = new Ajax.Request(
                                                wsBaseUrl + 'remove_samples_from_run/',
                                                {
                                                    parameters: {
                                                        run_id: self.runId,
                                                        sample_ids: ids.join(",")
                                                    },
                                                    asynchronous: true,
                                                    evalJSON: 'force',
                                                    onSuccess: function() {
                                                        self.runSampleStore.load({ params: { run_id: self.runId } });
                                                    }
                                                }
                                            );
                                        }
                                    }
                                }
                            }
                        ]
                    }
                },
                {
                   fieldLabel: 'Files - PBQCs, QCs and Sweeps',
                   xtype: 'treepanel',
                   border: true,
                   autoScroll: true,
                   itemId: 'runTree',
                   animate: true,
                   useArrows: true,
                   height: 200,
                   dataUrl: wsBaseUrl + 'runFiles',
                   requestMethod: 'GET',
                    tbar: [
                        {
                            xtype: 'tbtext',
                            text: 'Click a filename to download'
                        }
                    ], root: {
                       nodeType: 'async',
                       text: 'Files',
                       draggable: false,
                       id: 'runsRoot',
                       'metafile': true
                   },
                   selModel: new Ext.tree.DefaultSelectionModel(
                       { listeners:
                           {
                               selectionchange: function(sm, node) {
                                  if (node != null && !node.attributes.metafile) {
                                       window.location = wsBaseUrl + 'downloadRunFile?file=' + node.id + '&run_id=' + self.runId;
                                   }
                               }
                           }
                       }),
                   listeners: {
                        render: function() {
                            self.getComponent('runTree').getLoader().on("beforeload", function(treeLoader, node) {
                                treeLoader.baseParams.run = self.runId;
                                }, this);
                        }
                    }
               },
                {
                    fieldLabel: 'Related Experiments',
                    xtype: 'listview',
                    itemId: 'runRelatedExperiments',
                    store: runRelatedExperimentStore,
                    loadingText: 'Loading...',
                    columnSort: false,
                    hideHeaders: true,
                    height: 50,
                    columns: [
                    {header: "Double-click to view experiment", dataIndex: 'title'}
                    ],
                    listeners: {
                        'dblclick': function(dv, idx, node, e) {
                            val = dv.getRecord(node);
                            if (Ext.isDefined(val)) {
                                MA.currentProjectId = val.data.project;
                                MA.ExperimentController.loadExperiment(val.data.id);
                            }
                        },
                        'render': function() {
                        }
                    },
                    viewConfig: {
                        forceFit: true
                    },
                    singleSelect: true,
                    multiSelect: false,
                    style: 'background:white;border: 1px solid #99BBE8;',
                    autoScroll: true,
                    reserveScrollOffset: true
                }
            ],
            buttons: [
                {
                    text: 'Delete Run',
                    disabled: true,
                    itemId: 'deleteButton',
                    handler: function() {
                        self.deleteRun();
                    }
                },
                {
                    text: 'Generate Worklist',
                    disabled: true,
                    itemId: 'generateWorklistButton',
                    handler: function() {
                        if (self.runId == 0) {
                            Ext.Msg.alert('Save Required', 'Before you can generate a worklist, this Run must be Saved');
                        } else {
                            // TODO this doesn't seem right
                            // we should do it based on the Run's state that we display
                            if (this.getText() === 'Display Worklist') {
                                window.open(wsBaseUrl + 'display_worklist/' + self.runId, 'worklist');
                            } else {
                                var msg = Ext.Msg.wait("Generating Worklist");
                                Ext.Ajax.request({
                                    url: wsBaseUrl + "generate_worklist/" + self.runId,
                                    success: function() {
                                        msg.hide();
                                        self.onStateChangedToInProgress();

                                        window.open(wsBaseUrl + 'display_worklist/' + self.runId, 'worklist');
                                        self.fireEvent("save", self);
                                    },
                                    failure: function() {
                                        msg.hide();
                                        Ext.Msg.alert('Error', "An error occured and your worklist couldn't be generated");
                                    }
                                });
                            }
                        }
                    }
                },
                {
                    text: "Mark Complete",
                    itemId: 'markCompleteButton',
                    disabled: true,
                    handler: function() {
                        var agreed = window.confirm("Are you sure you wish to mark this run as having been fully completed?");
                        if (agreed) {
                            Ext.Ajax.request({
                                url: wsBaseUrl + "mark_run_complete/" + self.runId,
                                success: function() {
                                    self.getComponent("state").setText(renderRunState(2));
                                    self.getComponent("progress").setText(renderCompleteRunProgress(), false);
                                    self.fireEvent("save", self.runId);
                                }
                            });
                        }
                    }
                },
                {
                    text: 'Save Run',
                    itemId: 'saveButton',
                    disabled: !this.allowCreatingNewRun,
                    handler: function() {
                        if (self.isValid()) {
                            var runSaveCallback = function(store, records, options) {
                                self.runId = records[0].data.id;
                                self.savePendingSamples();

                                self.getFooterToolbar().getComponent("generateWorklistButton").enable();
                                self.getFooterToolbar().getComponent("markCompleteButton").enable();

                                self.fireEvent("save", records[0].data.id);
                            };

                            var values = {};
                            values.title = self.getComponent('title').getValue();
                            values.method_id = self.getComponent('method').getValue();
                            values.machine_id = self.getComponent('machine').getValue();
                            values.rule_generator_id = self.getComponent('rule_generator').getValue();
                            values.number_of_methods = self.getComponent('methods').getComponent('number_of_methods').getValue();
                            values.order_of_methods = self.getComponent('methods').getComponent('order_of_methods').getValue();
                            var restOfSaveFn = function() {
                                if (self.runId == 0) {
                                    //create new
                                    values.experiment_id = MA.ExperimentController.currentId();
                                    MA.CRUDSomething('create/run/', values, runSaveCallback);
                                } else {
                                    //update
                                    values.id = self.runId;

                                    MA.CRUDSomething('update/run/' + values.id + '/', values, runSaveCallback);
                                }
                            };
                            if (values.number_of_methods > 5) {
                                Ext.Msg.confirm('Large Number of Methods', 'The Number of Methods you entered (' +
                                    values.number_of_methods + ') is unusually high. This could cause the generation of a very long Worklist. Are you sure you want to proceed?',
                                    function(button) {
                                        if (button == 'yes') {
                                            restOfSaveFn();
                                        } else {
                                            self.getComponent('methods').getComponent('number_of_methods').focus();
                                        }
                                    });
                            } else {
                                restOfSaveFn();
                            }
                       }
                    }
                }
            ],
            isValid: function() {
                var valid = true;
                var methodsCmp = this.getComponent("methods");
                if (this.getComponent("machine").getValue() == "None" ||
                    this.getComponent("machine").getValue() == "") {
                    valid = false;
                    this.getComponent("machine").markInvalid("Required");
                }
                if (this.getComponent("method").getValue() == "None" ||
                    this.getComponent("method").getValue() == "") {
                    valid = false;
                    this.getComponent("method").markInvalid("Required");
                }
                if (this.getComponent("rule_generator").getValue() == "None" ||
                    this.getComponent("rule_generator").getValue() == "") {
                    valid = false;
                    this.getComponent("rule_generator").markInvalid("Required");
                }

                if (!methodsCmp.getComponent("number_of_methods").validate()) {
                    valid = false;
                } else {
                    if (methodsCmp.getComponent("number_of_methods").getValue() > 1) {
                        if (!methodsCmp.getComponent("order_of_methods").getValue()) {
                            valid = false;
                            methodsCmp.getComponent("order_of_methods").markInvalid("Required if Number Of Methods is set");
                        }
                    } else {
                        methodsCmp.getComponent("order_of_methods").clearInvalid();
                    }
                }
                return valid;
            }
        };

        config = Ext.apply(defaultConfig, config);

        MA.RunDetail.superclass.constructor.call(this, config);

        this.addEvents("delete", "save", "save-samples");

        this.pendingSampleStore.removeAll();
        this.runId = 0;

        if (!this.allowAddingSamples) {
            this.remove("samplesToAdd");
            this.getComponent('samples').setHeight(200);
        } else {
            this.remove('runTree');
        }

        self.setAutoScroll(true);
    },
    clearSamples: function() {
        this.pendingSampleStore.removeAll();
    },
    addSample: function(sample_id) {
        if (Ext.isArray(sample_id)) {
            for (var i = 0; i < sample_id.length; i++) {
                this.addSample(sample_id[i]);
            }
            return;
        }

        this.pendingSampleStore.add(new this.PendingSampleRecord({ 'id': sample_id }, sample_id));
    },
    clearRun: function() {
        this.createRun();
    },
    createRun: function() {
        this.runId = 0;
        //this.pendingSampleStore.removeAll();

        if (this.allowCreatingNewRun) {
            this.getFooterToolbar().getComponent("saveButton").enable();
        } else {
            this.getFooterToolbar().getComponent("saveButton").disable();
        }
        this.getComponent("title").setValue("New Untitled Run");
        this.getComponent("state").setText(renderRunState(0));
        this.getComponent("progress").setText(renderNoRunProgress(), false);

        this.getComponent("method").clearValue();
        this.getComponent("machine").clearValue();
        this.getComponent("rule_generator").clearValue();
        this.getComponent("methods").getComponent("number_of_methods").setValue('');
        this.getComponent("methods").getComponent("order_of_methods").clearValue();
        this.getComponent("rule_generator").enable();
        this.getComponent("methods").getComponent("number_of_methods").enable();
        this.getComponent("methods").getComponent("order_of_methods").enable();

        this.getFooterToolbar().getComponent("generateWorklistButton").disable();
        this.getFooterToolbar().getComponent("generateWorklistButton").setText('Generate Workflow');
        this.getFooterToolbar().getComponent("markCompleteButton").disable();
        this.getFooterToolbar().getComponent("deleteButton").disable();

        this.getComponent('samples').getBottomToolbar().getComponent('removeBtn').enable();

        runRelatedExperimentStore.removeAll();

        this.runSampleStore.load({ params: { run_id: this.runId } });
    },
    deleteRun: function() {
        var self = this;

        if (this.runId == 0) {
            this.clearRun();
            this.fireEvent("delete", 0);
        } else {
            var agreed = window.confirm("Are you sure you wish to delete this Run?");
            if (agreed) {
                MA.CRUDSomething('delete/run/' + this.runId + '/', null, function() {
                    self.clearRun();
                    self.fireEvent("delete", self.runId);
                });
            }
        }
    },
    savePendingSamples: function() {
        if (this.pendingSampleStore.getCount() > 0) {
            if (this.runId) {
                var self = this;

                var ids = [];
                this.pendingSampleStore.each(function(record) {
                    ids.push(record.data.id);
                });

                Ext.Ajax.request({
                    url: wsBaseUrl + "add_samples_to_run/",
                    method: "POST",
                    params: {
                        run_id: this.runId,
                        sample_ids: ids.join(",")
                    },
                    success: function() {
                        self.pendingSampleStore.removeAll();
                        self.runSampleStore.load({ params: { run_id: self.runId } });
                        self.fireEvent("save-samples");
                    },
                    failure: function(response, options) {
                        var message = "An error occurred while saving the "
                            + "sample list for this run. More detail may be "
                            + "available below:"
                            + "<br /><br />"
                            + response.responseText;

                        Ext.Msg.alert("Error", message);
                    }
                });
            }
            else {
                throw new Error("Pending samples can only be saved if a run has already been created.");
            }
        }
    },
    onStateChangedToInProgress: function() {
        this.getComponent("rule_generator").disable();
        this.getComponent("methods").getComponent("number_of_methods").disable();
        this.getComponent("methods").getComponent("order_of_methods").disable();
        this.getFooterToolbar().getComponent("generateWorklistButton").setText('Display Worklist');
    },
    selectRun: function(record) {
        var isNewRun = (record.data.state === 0);
        this.runId = record.data.id;
        //this.pendingSampleStore.removeAll();
        var numberOfMethods = record.data.number_of_methods;
        var orderOfMethods = record.data.order_of_methods;

        this.getComponent("state").setText(renderRunState(record.data.state));
        this.getComponent("progress").setText(renderRunProgress(undefined, undefined, record), false);
        this.getComponent("title").setValue(record.data.title);
        this.getComponent("method").setValue(record.data.method);
        this.getComponent("machine").setValue(record.data.machine);
        this.getComponent("rule_generator").setValue(record.data.rule_generator);
        if (isNaN(numberOfMethods)) {
            this.getComponent("methods").getComponent("number_of_methods").setValue('');
        } else {
            this.getComponent("methods").getComponent("number_of_methods").setValue(numberOfMethods);
        }
        if (isNaN(orderOfMethods)) {
            this.getComponent("methods").getComponent("order_of_methods").clearValue();
        } else {
            this.getComponent("methods").getComponent("order_of_methods").setValue(orderOfMethods);
        }


        this.getComponent("rule_generator").setDisabled(!isNewRun);
        this.getComponent("methods").getComponent("number_of_methods").setDisabled(!isNewRun);
        this.getComponent("methods").getComponent("order_of_methods").setDisabled(!isNewRun);

        this.getFooterToolbar().getComponent("saveButton").enable();
        this.getFooterToolbar().getComponent("generateWorklistButton").enable();
        this.getFooterToolbar().getComponent("markCompleteButton").enable();
        this.getFooterToolbar().getComponent("deleteButton").enable();

        if (isNewRun) {
            this.getFooterToolbar().getComponent("generateWorklistButton").setText('Generate Worklist');
        } else {
            this.getFooterToolbar().getComponent("generateWorklistButton").setText('Display Worklist');
        }


        if (record.data.state == 2) {
            //this.getFooterToolbar().getComponent("generateWorklistButton").disable();
            this.getFooterToolbar().getComponent("markCompleteButton").disable();
            this.getFooterToolbar().getComponent("deleteButton").disable();
        }

        if (record.data.state == 0) {
            this.getComponent('samples').getBottomToolbar().getComponent('removeBtn').enable();
        } else {
            this.getComponent('samples').getBottomToolbar().getComponent('removeBtn').disable();
        }

        this.runSampleStore.load({ params: { run_id: this.runId } });

        if (this.getComponent('runTree')) {
            this.getComponent('runTree').getLoader().clearOnLoad = true;
            this.getComponent('runTree').getLoader().load(this.getComponent('runTree').getRootNode());
        }

        runRelatedExperimentStore.proxy.conn.url = wsBaseUrl + 'records/experiment/run__id/' + this.runId;
        runRelatedExperimentStore.load({ });
    }
});

MA.RunCmp = new Ext.Window({
    id: 'runCmp',
    title: 'Current Run',
    width: 680,
    height: 530,
    minHeight: 530,
    x: 170,
    y: 50,
    closeAction: 'hide',
    layout: 'border',
    tbar: [{
        text: 'Create New',
        cls: 'x-btn-text-icon',
        icon: 'static/images/add.png',
        handler: function() {
                MA.CreateNewRun();
            }
        }
    ],
    listeners: {
        "beforeshow": function(w) {
            MA.ReloadRunStores();
            //selectableRunStore.load();
        }
    },
    items: [

        {
            xtype: 'listview',
            id: 'runlistview',
            region: 'west',
            width: 150,
            store: newRunsStore,
            loadingText: 'Loading...',
            columnSort: false,
            columns: [
                {header: "Or select existing", dataIndex: 'title',
                    tpl: '<div style="padding:4px"><b>{title}</b><br><div style="color:#666"><i>{method__unicode}<br>{creator__unicode}</i></div></div>'}
            ],
            listeners: {
                'selectionchange': MA.RunCmpRowSelect,
                'render': function() {
                    //register to be notified when the runstore loads so that we can update current sel

                    newRunsStore.addListener("load", function() {
                        var record = newRunsStore.getById(Ext.getCmp('runDetails').runId);
                        if (record != null) {
                            var list = Ext.getCmp("runlistview");
                            list.refresh();

                            list.select(record);
                            list.getNode(record).scrollIntoView(list.innerBody.dom.parentNode);
                        } else {
                            MA.CreateNewRun();
                        }
                    });
                    //selectableRunStore.load();
                }
            },
            viewConfig: {
                forceFit: true
            },
            singleSelect: true,
            multiSelect: false,
            style: 'background:white;',
            autoScroll: true,
            reserveScrollOffset: true
        },
        new MA.RunDetail({
            id: 'runDetails',
            autoScroll: true,
            region: 'center',
            bodyStyle: 'padding:20px;background:transparent;border-top:none;border-bottom:none;border-right:none;',
            listeners: {
                "delete": MA.RunDeleteCallback,
                "save": function(id) {
                    MA.ReloadRunStores();
                }
            }
        }, {allowCreatingNewRun: true, allowAddingSamples: true})
    ]
});
