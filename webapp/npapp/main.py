import os

from threading import Thread

from bokeh.models.widgets import RadioGroup, Slider, Div, Select, CheckboxGroup
from bokeh.models.widgets import Toggle, Button, TextInput, PreText
from bokeh.layouts import row
from bokeh.models import CustomJS
from bokeh.plotting import figure, curdoc
from bokeh.models import ColumnDataSource, HoverTool, TapTool, CDSView, IndexFilter, MultiLine
from bokeh.models.markers import Circle
from bokeh.models.renderers import GraphRenderer, GlyphRenderer
from bokeh.models.graphs import StaticLayoutProvider, NodesOnly
from bokeh.events import LODEnd, LODStart, Reset

from nplinker.metabolomics import Spectrum, MolecularFamily, SingletonFamily
from nplinker.genomics import GCF, BGC
from nplinker.annotations import gnps_url, GNPS_KEY
from nplinker.scoring.methods import LinkCollection

from searching import SEARCH_OPTIONS, Searcher

from tables_functions import NA_ID

# TODO all of this code doesn't have to be in the same module, could benefit from splitting up by
# functionality (e.g. scoring, searching, UI, ...)

# TOOLS="crosshair,pan,wheel_zoom,zoom_in,zoom_out,box_zoom,undo,redo,reset,tap,save,box_select,poly_select,lasso_select,"
TOOLS = "crosshair,pan,wheel_zoom,box_zoom,reset,save,box_select,tap"

PW, PH = 500, 500

POINT_FACTOR = 130.0
POINT_RATIO = 1 / POINT_FACTOR

ACTIVE_BG_FILL = '#ffffff'
INACTIVE_BG_FILL = '#eeeeee'
ACTIVE_BG_ALPHA = 1.0
INACTIVE_BG_ALPHA = 0.555

SCORING_GEN_TO_MET = 'TO <------ FROM'
SCORING_MET_TO_GEN = 'FROM ------> TO'

PLOT_TOGGLES = ['Show MiBIG BGCs', 'Show singleton families', 'Preserve colours when selecting', 'Only show results with shared strains']
PLOT_MIBIG_BGCS, PLOT_SINGLETONS, PLOT_PRESERVE_COLOUR, PLOT_ONLY_SHARED_STRAINS = range(len(PLOT_TOGGLES))
PLOT_TOGGLES_ENUM = [PLOT_MIBIG_BGCS, PLOT_SINGLETONS, PLOT_PRESERVE_COLOUR, PLOT_ONLY_SHARED_STRAINS]

GENOMICS_SCORING_MODES = ['manual', 'GCF']
METABOLOMICS_SCORING_MODES = ['manual', 'MolFam']

GENOMICS_MODE_BGC, GENOMICS_MODE_GCF = range(2)
METABOLOMICS_MODE_SPEC, METABOLOMICS_MODE_MOLFAM = range(2)

# Scoring modes are:
# 1. BGCs (i.e. the immediate parent GCFs) against Spectra
# 2. GCFs (i.e. all GCFs containing selected BGCs) against Spectra
# 3. BGCs (...) against MolFams
# 4. GCFs (...) against MolFams
# 5. Spectra against GCFs
# 6. MolFams against GCFs
# (there are 6 and not 8 because BGCs aren't "first-class" objects as far as scoring is concerned)

# for display/output purposes, 1+3 and 2+4 are basically identical, because you still end
# up having a list of GCFs as input to the scoring function

SCO_MODE_BGC_SPEC, SCO_MODE_BGC_MOLFAM,\
    SCO_MODE_GCF_SPEC, SCO_MODE_GCF_MOLFAM,\
    SCO_MODE_SPEC_GCF, SCO_MODE_MOLFAM_GCF = range(6)

SCO_MODE_NAMES = ['BGCs to Spectra', 'BGCs to MolFams', 'GCFs to Spectra', 'GCFs to MolFams',
                'Spectra to GCFs', 'MolFams to GCFs']

SCO_GEN_TO_MET = [True, True, True, True, False, False]

# basic template for a bootstrap "card" element to display info about a
# given GCF/Spectrum etc
# format params:
# - unique id for header, e.g spec_heading_1
# - colour for header
# - unique id for body, e.g. spec_body_1
# - title text
# - id for body, same as #2
# - parent id 
# - main text
TMPL = open(os.path.join(os.path.dirname(__file__), 'templates/tmpl.basic.py.html')).read()

# same as above but with an extra pair of parameters for the "onclick" handler and button id
TMPL_ON_CLICK = open(os.path.join(os.path.dirname(__file__), 'templates/tmpl.onclick.py.html')).read()

TMPL_SEARCH = open(os.path.join(os.path.dirname(__file__), 'templates/tmpl.basic.search.py.html')).read()

TMPL_SEARCH_ON_CLICK = open(os.path.join(os.path.dirname(__file__), 'templates/tmpl.onclick.search.py.html')).read()

HOVER_CALLBACK_CODE = """
    // these are the currently hovered indices, which can seemingly include
    // markers that are non-visible
    var indices = cb_data.index.indices;

    // data source column names
    let ds = cb_data.renderer.data_source;
    let keys = Object.keys(ds.data);
    // check if this is the genomics or metabolomics plot using the column names
    let gen_mode = keys.indexOf('gcf') != -1;

    // access data_source.data via cb_data.renderer.data_source.data

    if (indices.length > 0)
    {
        // selected (as opposed to hovered/inspected) indices
        let selected_indices = ds.selected.indices;

        let obj_col = gen_mode ? 'index' : 'index';
        let parent_col = gen_mode ? 'gcf_name' : 'family';
        let objtype = gen_mode ? 'GCF ID(s): ' : 'Spectrum ID(s): ';

        // idea here is to display info about the object(s) being hovered over, 
        // subject to these conditions:
        // - if there are no selected points, can respond to any indices being hovered
        // - if there ARE selected points, only want to respond to those indices (otherwise
        //  what happens is that the non-visible nonselected glyphs can still trigger the 
        //  callback output, and that is very confusing!)

        let objects = null;
        let parents = null;
        if(selected_indices.length > 0) {
            // only include those ids that are also selected
            indices = indices.filter(element => selected_indices.includes(element));
        }
        objects = indices.map(element => ds.data[obj_col][element]);
        parents = indices.map(element => ds.data[parent_col][element]);

        // now to update the text in the output divs
        // 1st div: show either "(nothing under cursor)" or "x BGCs in y GCFs" / "x spectra in y MolFams"
        if(objects.length == 0) {
            $(output_div_id).html('<small>' + empty_text + '</small>');
            $(output_div_id_ext).html('');
        } else {
            let ohtml = '<small>';
            // remove dups
            objects = objects.filter((element, index) => objects.indexOf(element) === index);
            parents = parents.filter((element, index) => parents.indexOf(element) === index);

            if(gen_mode) {
                ohtml += objects.length + ' BGCs in ' + parents.length + ' GCFs';

                var bgcs = '<strong>BGCs</strong>: ';
                var gcfs = '<strong>GCFs</strong>: ';
                for(var i=0;i<objects.length;i++) {
                    bgcs += ds.data['name'][objects[i]] + ',';
                    gcfs += ds.data['gcf_name'][objects[i]] + ',';
                }
                $(output_div_id_ext).html('<small>' + bgcs + '<br/>' + gcfs + '</small>');
            } else {
                ohtml += objects.length + ' spectra in ' + parents.length + ' MolFams';

                var specs = '<strong>Spectra</strong>: ';
                var fams = '<strong>MolFams</strong>: ';
                for(var i=0;i<objects.length;i++) {
                    specs += ds.data['name'][objects[i]] + ',';
                    fams += ds.data['family'][objects[i]] + ',';
                }
                $(output_div_id_ext).html('<small>' + specs + '<br/>' + fams + '</small>');
            }
            ohtml += '<small>';
            $(output_div_id).html(ohtml);
        }
    } 
    else
    {
        $(output_div_id).html('<small>' + empty_text + '</small>');
        if(gen_mode)
            $(output_div_id_ext).html('<small><strong>BGCs</strong>: <br/><strong>GCFs</strong>:</small>');
        else
            $(output_div_id_ext).html('<small><strong>Spectra</strong>: <br/><strong>MolFams</strong>:</small>');
    }
"""

# what gets displayed in the <div> for results when there are no results
RESULTS_PLACEHOLDER = """
    <div class="result-placeholder"><h3>No results to display</h3></div>
"""

def get_radius(datasource):
    """Utility method that aims to keep marker sizes reasonable when plots are zoomed"""
    minx = min(datasource.data['x'])
    maxx = max(datasource.data['x'])
    return (maxx - minx) / POINT_FACTOR

class ScoringHelper(object):
    """
    This class is a wrapper around some of the details of handling the various
    different scoring modes supported by the webapp
    """

    GENOMICS_LOOKUP  = {GENOMICS_MODE_BGC: [SCO_MODE_BGC_SPEC, SCO_MODE_BGC_MOLFAM],
                        GENOMICS_MODE_GCF: [SCO_MODE_GCF_SPEC, SCO_MODE_GCF_MOLFAM]}

    METABOLOMICS_LOOKUP = [SCO_MODE_SPEC_GCF, SCO_MODE_MOLFAM_GCF]

    def __init__(self, nh):
        self.genomics_mode = GENOMICS_MODE_BGC
        self.metabolomics_mode = METABOLOMICS_MODE_SPEC
        self.mode = SCO_MODE_BGC_SPEC
        # True if scoring is currently being done *from* genomics side *to* metabolomics
        self.from_genomics = True
        self.nh = nh
        self.clear()

    def clear(self):
        self.bgcs = []
        self.gcfs = []
        self.spectra = []
        self.molfams = []
        self.inputs = set()
        self.scoring_results = LinkCollection()
    
    def set_genomics(self):
        self.from_genomics = True
        self._update()

    def set_metabolomics(self):
        self.from_genomics = False
        self._update()

    def update_genomics(self, m):
        self.from_genomics = True
        self.genomics_mode = m
        self._update()

    def update_metabolomics(self, m):
        self.from_genomics = False
        self.metabolomics_mode = m
        self._update()

    def gen_to_met(self):
        return SCO_GEN_TO_MET[self.mode]

    def _update(self):
        if self.from_genomics:
            self.mode = ScoringHelper.GENOMICS_LOOKUP[self.genomics_mode][self.metabolomics_mode]
        else:
            self.mode = ScoringHelper.METABOLOMICS_LOOKUP[self.metabolomics_mode]

    def set_results(self, scoring_results):
        self.scoring_results = scoring_results

    @property
    def mode_name(self):
        return SCO_MODE_NAMES[self.mode]

    def generate_scoring_objects(self, bgcs, spectra, tsne_id):
        """
        This method handles constructing the set of objects to be used as input to
        the nplinker "get_links" scoring method, based on the type of objects selected
        from the plots and the currently selected TSNE projection (if any, currently not used).

        For most scoring modes it doesn't need to do anything much, but for the
        GCF to Spec and GCF to MolFam modes, the initial selection of BGCs 
        may reference GCFs that don't appear in the current projection 
        (as in these modes we are saying "select ALL GCFs that contain ANY of 
        the selected BGCs"). In this case the unavailable GCFs need to be filtered
        out. 
        """
        scoring_objs = set()

        # store these for later use
        self.bgcs = bgcs
        self.spectra = spectra

        if self.mode == SCO_MODE_BGC_SPEC or self.mode == SCO_MODE_BGC_MOLFAM:
            # in either of these two modes, we simply want to select the set 
            # of "parent" GCFs for each of the selected BGCs, which is simple
            # (although have to filter out dups here!)
            scoring_objs = set([bgc.parent for bgc in bgcs])
            self.gcfs = list(scoring_objs)
        elif self.mode == SCO_MODE_GCF_SPEC or self.mode == SCO_MODE_GCF_MOLFAM:
            # this pair of modes are more complex. first build a set of *all* 
            # GCFs that contain *any* of the selected BGCs

            # this is the set of GCFs available in the current TSNE projection
            available_gcfs = self.nh.available_gcfs[tsne_id]
            all_bgcs = set()

            # for each selected BGC...
            for bgc in bgcs:
                if bgc not in self.nh.bgc_gcf_lookup:
                    print('WARNING: missing BGC in lookup: {}'.format(bgc))
                    continue

                # ... get the set of GCFs of which it is a member...
                gcfs_for_bgc = self.nh.bgc_gcf_lookup[bgc]
                print('BGC {} parents = {}'.format(bgc, gcfs_for_bgc))

                # ... and filter out any not in the current TSNE projection
                gcfs_for_bgc = [gcf for gcf in gcfs_for_bgc if gcf in available_gcfs]

                scoring_objs.update(gcfs_for_bgc)

                for gcf in gcfs_for_bgc:
                    all_bgcs.update(gcf.bgcs)

            print('Selection of {} BGCs => {} GCFs'.format(len(bgcs), len(scoring_objs)))
            self.gcfs = list(scoring_objs)
            # now need to update the set of BGCs selected to include all of those
            print('Final total of BGCs: {}'.format(len(all_bgcs)))
            self.bgcs = list(all_bgcs)
        elif self.mode == SCO_MODE_SPEC_GCF:
            # in this mode can simply use the list of spectra
            scoring_objs = spectra
        elif self.mode == SCO_MODE_MOLFAM_GCF:
            # TODO
            # here i think will need to go from Spectra -> MolFams in same way as
            # for BGCs -> GCFs above
            print('NOT DONE YET')
            return None
        else:
            raise Exception('Unknown scoring mode! {}'.format(self.mode))

        return list(scoring_objs)

class ZoomHandler(object):
    """
    This class deals with scaling the circle glyphs on the scatterplots so that
    they stay roughly the same size during zooming. 
    """

    def __init__(self, plot, renderer):
        self.lod_mode = False
        self.renderer = renderer
        self.ratio = POINT_RATIO

        # want to listen for changes to the start/end of the x/y ranges displayed by the plot...
        # can probably get away with only listening for one of these? (and y events seem to be
        # delivered after x)
        # plot.x_range.on_change('end', self.x_cb)
        plot.y_range.on_change('end', self.y_cb)

        # ... and also entering/leaving "level of detail" mode, a lower-detail mode activated
        # during panning/zooming etc. this allows changes producing by panning which we don't
        # care about because it doesn't alter the zoom level
        plot.on_event(LODStart, self.lod_event)
        plot.on_event(LODEnd, self.lod_event)
        plot.on_event(Reset, self.reset_event)

        self.plot = plot

    def reset_event(self, e):
        radius = get_radius(self.renderer.data_source)
        self.renderer.glyph.radius = radius

    def update_ratio(self):
        # handle init state when x_/y_range are None
        if self.plot.y_range.start is not None:
            self.renderer.glyph.radius = self.ratio * min(self.plot.x_range.end - self.plot.x_range.start, self.plot.y_range.end - self.plot.y_range.start)

    def x_cb(self, attr, old, new):
        """ 
        Callback for changes to the x range. attr = 'start'/'end', 'old' and 'new'
        give the previous and current values.
        """
        self.update_ratio()

    def y_cb(self, attr, old, new):
        """ 
        Callback for changes to the y range. attr = 'start'/'end', 'old' and 'new'
        give the previous and current values.
        """
        self.update_ratio()

    def lod_event(self, event):
        self.lod_mode = isinstance(event, LODStart)


class NPLinkerBokeh(object):

    def __init__(self, helper, table_session_data):
        # handle the "helper" object from the server_lifecycle module, which deals
        # with loading the dataset and creating data ready to be wrapped in DataSources
        self.nh = helper
        self.table_session_data = table_session_data

        # default to TSNE with all bgcs
        # NOTE: currently we're using the dynamically generated networkx layouts, TSNE stuff inactive
        self.bgc_tsne_id = '<networkx>'
        self.bgc_tsne_id_list = list(self.nh.bgc_data.keys())

        # hide singletons by default (spec_nonsingleton_indices holds the indices of 
        # all spectra with non-singleton families) ie the IndexFilter accepts a 
        # list of indices that you want to *show*
        self.hide_singletons = True

        # similar thing for MiBIG BGCs, hide them by default
        self.hide_mibig = True

        # these methods set up all the require datasources, filters and views for the plots
        self._configure_metabolomics()
        self._configure_genomics()

        self.bgc_zoom = None
        self.spec_zoom = None

        self.score_helper = ScoringHelper(self.nh)

        # by default filter out scoring results where the pair of objects has no shared strains
        self.filter_no_shared_strains = True

        self.searcher = Searcher(self.nh.nplinker)

    def _configure_metabolomics(self):
        # datasource containing all spectra
        self.spec_datasource = ColumnDataSource(data=self.nh.spec_data)
        # equivalent containing edges between the spectra
        self.spec_edges_datasource = ColumnDataSource(data=self.nh.spec_edge_data)
        # indices of spectra that are NOT in singleton molfams
        self.spec_nonsingleton_indices = [i for i in range(len(self.spec_datasource.data['x'])) if not self.spec_datasource.data['singleton'][i]]
        # a filter which ONLY shows the above indices, i.e. it ONLY shows NON-singleton spectra
        self.spec_index_filter = IndexFilter(self.spec_nonsingleton_indices)
        # a "view" onto the full datasource, with the above filter applied
        self.spec_datasource_view = CDSView(source=self.spec_datasource, filters=[self.spec_index_filter])
        # same idea with these 3 objects, but for edges rather than nodes
        self.spec_edges_nonsingleton_indices = [i for i in range(len(self.spec_edges_datasource.data['singleton'])) if not self.spec_edges_datasource.data['singleton'][i]]
        self.spec_edges_index_filter = IndexFilter(self.spec_edges_nonsingleton_indices)
        self.spec_edges_datasource_view = CDSView(source=self.spec_edges_datasource, filters=[self.spec_edges_index_filter])

    def _configure_genomics(self):
        # datasource containing all BGCs
        self.bgc_datasource = ColumnDataSource(data=self.nh.bgc_data[self.bgc_tsne_id])
        # equivalent containing edges between the BGCs
        self.bgc_edges_datasource = ColumnDataSource(data=self.nh.bgc_edge_data[self.bgc_tsne_id])
        # indices of BGCs that are not MiBIG 
        self.bgc_nonmibig_indices = [i for i in range(len(self.bgc_datasource.data['x'])) if not self.bgc_datasource.data['mibig'][i]]
        # a filter which ONLY shows the above indices, i.e. it ONLY shows NON-MiBIG BGCs
        self.bgc_index_filter = IndexFilter(self.bgc_nonmibig_indices)
        # a "view" onto the full datasource, with the above filter applied
        self.bgc_datasource_view = CDSView(source=self.bgc_datasource, filters=[self.bgc_index_filter])
        # same idea with these 3 objects, but for edges rather than nodes
        self.bgc_edges_nonmibig_indices = [i for i in range(len(self.bgc_edges_datasource.data['mibig'])) if not self.bgc_edges_datasource.data['mibig'][i]]
        self.bgc_edges_index_filter = IndexFilter(self.bgc_edges_nonmibig_indices)
        self.bgc_edges_datasource_view = CDSView(source=self.bgc_edges_datasource, filters=[self.bgc_edges_index_filter])

    @property
    def bgc_data(self):
        return self.nh.bgc_data[self.bgc_tsne_id]

    @property
    def bgc_edge_data(self):
        return self.nh.bgc_edge_data[self.bgc_tsne_id]

    @property
    def nplinker(self):
        return self.nh.nplinker

    @property
    def spec_data(self):
        return self.nh.spec_data

    @property
    def spec_edge_data(self):
        return self.nh.spec_edge_data

    @property
    def spec_indices(self):
        return self.nh.spec_indices

    @property
    def bgc_indices(self):
        return self.nh.bgc_indices[self.bgc_tsne_id]

    def create_plots(self):
        # define JS callbacks that will be executed when the user hovers over a 
        # node. this is set up to display information about the object under the cursor
        # in a small <div> under the plot itself
        bgc_hover_callback = CustomJS(args={'output_div_id': '#bgc_hover_info', 
                                  'output_div_id_ext': '#bgc_hover_info_ext',
                                    'empty_text' : '(nothing under cursor)'}, 
                                    code=HOVER_CALLBACK_CODE)
        spec_hover_callback = CustomJS(args={'output_div_id': '#spec_hover_info', 
                                  'output_div_id_ext': '#spec_hover_info_ext',
                                    'empty_text' : '(nothing under cursor)'}, 
                                    code=HOVER_CALLBACK_CODE)

        self.fig_bgc, self.ren_bgc, self.hover_bgc = self.create_bokeh_plot(self.bgc_datasource,
                                                                            self.bgc_datasource_view,
                                                                            self.bgc_edges_datasource,
                                                                            self.bgc_edges_datasource_view,
                                                                            'BGCs (n={})'.format(len(self.bgc_data['x'])),
                                                                            'fig_bgc',
                                                                            self.nh.bgc_positions,
                                                                            bgc_hover_callback)
        self.ds_bgc = self.ren_bgc.node_renderer.data_source
        self.ds_edges_bgc = self.ren_bgc.edge_renderer.data_source

        self.fig_spec, self.ren_spec, self.hover_spec = self.create_bokeh_plot(self.spec_datasource,
                                                                               self.spec_datasource_view,
                                                                               self.spec_edges_datasource,
                                                                               self.spec_edges_datasource_view,
                                                                               'Spectra (n={})'.format(len(self.spec_data['x'])),
                                                                               'fig_spec',
                                                                               self.nh.spec_positions,
                                                                               spec_hover_callback)




        self.ds_spec = self.ren_spec.node_renderer.data_source
        self.ds_edges_spec = self.ren_spec.edge_renderer.data_source

    def create_bokeh_plot(self, node_ds, node_view, edge_ds, edge_view, title, name, layout_data, hover_callback):
        radius = get_radius(node_ds)

        # this is the base figure, which shows a plot area and the bokeh plot tools
        # but doesn't deal with any actual data
        fig = figure(tools=TOOLS,
                       toolbar_location='above',
                       title=title,
                       sizing_mode='scale_width',
                       name=name,
                       x_range=self.nh.plot_x_range(),
                       y_range=self.nh.plot_y_range())

        # create a bokeh GraphRenderer object, and tell it that we want to supply
        # our own static layout data
        bgr = GraphRenderer()
        bgr.layout_provider = StaticLayoutProvider(graph_layout=layout_data)

        # now lots of renderer configuration... 
        # setting GraphRenderer attributes like this avoids a rendering bug (vs setting individually)

        # for the node renderer:
        # - data_source is the full genomics datasource object
        # - view is the filtered view of this full collection
        # - the various glyphs correspond to different possible states for a node
        # - nonselected glyphs are hidden (i.e. when *some* are selected, the remainder are hidden)
        # - the positions of the nodes are drawn from the layout provider above
        # - the radii are set to the fixed value from get_radius() 
        bgr.node_renderer = GlyphRenderer(data_source=node_ds,
                                          glyph=Circle(radius=radius,
                                                       radius_dimension='max',
                                                       fill_color='fill',
                                                       fill_alpha=0.9,
                                                       line_color=None),
                                          selection_glyph=Circle(radius=radius,
                                                                 radius_dimension='max',
                                                                 fill_color='fill',
                                                                 fill_alpha=1,
                                                                 line_color=None),
                                          nonselection_glyph=Circle(radius=radius,
                                                                    radius_dimension='max',
                                                                    fill_color='#333333',
                                                                    fill_alpha=0.0,
                                                                    line_alpha=0.0,
                                                                    line_color=None),
                                          view=node_view)

        # for the edge renderer:
        # - data_source/view are handled as above
        # - line coords come from the datasource
        # - "nonselection" lines are supposed to be hidden from view
        bgr.edge_renderer = GlyphRenderer(data_source=edge_ds,
                                          glyph=MultiLine(line_color='#bbbbbb',
                                                          xs='xs',
                                                          ys='ys',
                                                          line_width=1),
                                          selection_glyph=MultiLine(line_color='#bbbbbb',
                                                                    xs='xs',
                                                                    ys='ys',
                                                                    line_width=1,
                                                                    line_alpha=0.7),
                                          nonselection_glyph=MultiLine(line_color='#000000',
                                                                       xs='xs',
                                                                       ys='ys',
                                                                       line_width=0,
                                                                       line_alpha=0),
                                          view=edge_view)

        # mouseover and clicks should only interact with nodes (not edges as well)
        bgr.inspection_policy = NodesOnly()
        bgr.selection_policy = NodesOnly()

        # manually append the GraphRenderer to the set managed by the figure
        fig.renderers.append(bgr)

        # this object will watch for zoom changes and adjust the node radius in response
        zoomhandler = ZoomHandler(fig, bgr.node_renderer)

        # add the "HoverTool" object to the set of plot tools, and tell it to use this callback
        hover_tool = HoverTool(tooltips=None, callback=hover_callback, renderers=[bgr.node_renderer])
        fig.add_tools(hover_tool)
        # TODO workaround for an apparent bug
        # see https://github.com/bokeh/bokeh/issues/9237
        fig.add_tools(HoverTool(tooltips=None))

        return fig, bgr, hover_tool

    def set_tap_behavior(self, plot, btype):
        # this basically prevents clicks from selecting anything on the plot
        taptool = plot.select(type=TapTool)[0]
        taptool.behavior = btype

    def configure_bgc_selchanged_listener(self, enabled):
        if enabled:
            self.ds_bgc.selected.on_change('indices', self.bgc_selchanged)
        else:
            self.ds_bgc.selected.remove_on_change('indices', self.bgc_selchanged)

    def configure_spec_selchanged_listener(self, enabled):
        if enabled:
            self.ds_spec.selected.on_change('indices', self.spec_selchanged)
        else:
            self.ds_spec.selected.remove_on_change('indices', self.spec_selchanged)

    def set_inactive_plot(self, inactive):
        if self.fig_bgc == inactive:
            if 'indices' in self.ds_bgc.selected._callbacks and len(self.ds_bgc.selected._callbacks['indices']) > 0:
                self.configure_bgc_selchanged_listener(False)
            self.clear_selections()
            self.fig_spec.background_fill_color = ACTIVE_BG_FILL
            self.fig_spec.background_fill_alpha = ACTIVE_BG_ALPHA
            self.fig_bgc.background_fill_color = INACTIVE_BG_FILL
            self.fig_bgc.background_fill_alpha = INACTIVE_BG_ALPHA
            self.configure_spec_selchanged_listener(True)

            self.set_tap_behavior(self.fig_bgc, 'inspect')
            self.set_tap_behavior(self.fig_spec, 'select')

            self.fig_spec.outline_line_color = 'navy'
            self.fig_spec.outline_line_width = 2
            self.fig_bgc.outline_line_color = None
        else:
            if 'indices' in self.ds_spec.selected._callbacks and len(self.ds_spec.selected._callbacks['indices']) > 0:
                self.configure_spec_selchanged_listener(False)

            self.clear_selections()
            self.fig_bgc.background_fill_color = ACTIVE_BG_FILL
            self.fig_bgc.background_fill_alpha = ACTIVE_BG_ALPHA
            self.fig_spec.background_fill_color = INACTIVE_BG_FILL
            self.fig_spec.background_fill_alpha = INACTIVE_BG_ALPHA
            self.configure_bgc_selchanged_listener(True)

            self.set_tap_behavior(self.fig_bgc, 'select')
            self.set_tap_behavior(self.fig_spec, 'inspect')

            self.fig_bgc.outline_line_color = 'navy'
            self.fig_bgc.outline_line_width = 2
            self.fig_spec.outline_line_color = None

    def generate_gcf_spec_result(self, pgindex, gcf, link_data):
        # generate a GCF >> Spectra nested list

        body = '<div class="accordion" id="accordion_gcf_{}">'.format(pgindex)
        # generate the body content of the card by iterating over the spectra it matched
        j = 0
        
        # sort by metcalf scoring if possible 
        # TODO this will need some more thought when other methods added!
        if self.scoring_objects['metcalf'] in self.score_helper.scoring_results.methods:
            sorted_links = self.score_helper.scoring_results.get_sorted_links(self.scoring_objects['metcalf'], gcf)
        else:
            sorted_links = link_data.values()

        for link in sorted_links:
            spec = link.target
            score = '[' + ', '.join('{}={}'.format(m.name, m.format_data(link[m])) for m in link.methods) + ']'

            spec_hdr_id = 'spec_result_header_{}_{}'.format(pgindex, j)
            spec_body_id = 'spec_body_{}_{}'.format(pgindex, j)
            spec_title = 'Spectrum(parent_mass={:.4f}, id={}), scores <strong>{}</strong>, shared strains=<strong>{}</strong>'.format(spec.parent_mz, spec.spectrum_id, score, len(link.shared_strains))
            if spec.has_annotations():
                spec_title += ', # annotations={}'.format(len(spec.annotations))

            spec_body = self.generate_spec_info(spec, link.shared_strains)

            # set up the chemdoodle plot so it appears when the entry is expanded 
            spec_btn_id = 'spec_btn_{}_{}'.format(pgindex, j)
            spec_plot_id = 'spec_plot_{}_{}'.format(pgindex, j)
            spec_body += '<center><canvas id="{}"></canvas></center>'.format(spec_plot_id)
            span_badges = '&nbsp|&nbsp;'.join(['<span class="badge badge-info">{}</span>'.format(m.name) for m in link.methods])

            # note annoying escaping required here, TODO better way of doing this?
            spec_onclick = 'setupPlot(\'{}\', \'{}\', \'{}\');'.format(spec_btn_id, spec_plot_id, spec.to_jcamp_str())

            hdr_color = 'ffe0b5'
            if len(spec.annotations) > 0:
                hdr_color = 'ffb5e0'
            body += TMPL_ON_CLICK.format(hdr_id=spec_hdr_id, hdr_color=hdr_color, btn_target=spec_body_id, btn_onclick=spec_onclick, btn_id=spec_btn_id, 
                                         btn_text=spec_title, span_badges=span_badges, body_id=spec_body_id, body_parent='accordion_gcf_{}'.format(pgindex), body_body=spec_body)
            j += 1

        body += '</div>'
        return body

    def generate_spec_gcf_result(self, pgindex, spec, link_data):
        # generate a Spectrum >> GCF nested list

        body = '<div class="accordion" id="accordion_spec_{}>'.format(pgindex)
        # now generate the body content of the card by iterating over the GCFs it matched
        
        j = 0

        # sort by metcalf scoring if possible 
        # TODO this will need some more thought when other methods added!
        if self.scoring_objects['metcalf'] in self.score_helper.scoring_results.methods:
            sorted_links = self.score_helper.scoring_results.get_sorted_links(self.scoring_objects['metcalf'], spec)
        else:
            sorted_links = link_data.values()

        for link in sorted_links:
            gcf= link.target
            score = '[' + ', '.join('{}={}'.format(m.name, m.format_data(link[m])) for m in link.methods) + ']'

            # construct a similar object info card header for this GCF
            gcf_hdr_id = 'gcf_result_header_{}_{}'.format(pgindex, j)
            gcf_body_id = 'gcf_body_{}_{}'.format(pgindex, j)
            gcf_title = 'GCF(id={}), scores <strong>{}</strong>, shared strains=<strong>{}</strong>'.format(gcf.gcf_id, score, len(link.shared_strains))
            gcf_body = self.generate_gcf_info(gcf, link.shared_strains)
            span_badges = '&nbsp|&nbsp;'.join(['<span class="badge badge-info">{}</span>'.format(m.name) for m in link.methods])
            body += TMPL.format(hdr_id=gcf_hdr_id, hdr_color='ffe0b5', btn_target=gcf_body_id, btn_text=gcf_title, span_badges=span_badges,
                                body_id=gcf_body_id, body_parent='accordion_spec_{}'.format(pgindex), body_body=gcf_body)

            j += 1

        body += '</div>'
        return body

    def update_results_gcf_spec(self):
        """
        Generates scoring output for mode BGC/GCF -> Spectra. 

        This will be of the form:
            GCF 1
                <collapsible list of spectra linked to GCF 1>
            GCF 2
                <collapsible list of spectra linked to GCF 2>
            ...
        """
        pgindex = 0
        unsorted = []

        # iterate over every GCF and its associated set of linked objects + scores
        for gcf, link_data in self.score_helper.scoring_results.links.items():
            hdr_id = 'gcf_result_header_{}'.format(pgindex)
            body_id = 'gcf_result_body_{}'.format(pgindex)

            title = '{} spectra linked to GCF(id={})'.format(len(link_data), gcf.gcf_id)

            # first part of the body content is basic info about the GCF itself
            body = self.generate_gcf_info(gcf)
            body += '<hr/><h5>Linked objects:</h5>'
            # the second part is the list of linked spectra for the GCF, which is
            # generated by calling this method
            body += self.generate_gcf_spec_result(pgindex, gcf, link_data)

            # finally, store the complete generated HTML string in a list along with
            # the number of links this GCF has, so we can sort the eventual list
            # by number of links 
            unsorted.append((len(link_data), TMPL.format(hdr_id=hdr_id, hdr_color='adeaad', 
                                                           btn_target=body_id, btn_text=title, span_badges='',
                                                           body_id=body_id, body_parent='accordionResults', 
                                                           body_body=body)))
            pgindex += 1

        content = '<h4>GCFs={}, linked spectra={}</h4>'.format(len(self.score_helper.scoring_results), len(self.ds_spec.selected.indices))

        for _, text in sorted(unsorted, key=lambda x: -x[0]):
            content += text

        return content

    def update_results_gcf_molfam(self):
        return '' # TODO

    def update_results_spec_gcf(self):
        pgindex = 0
        unsorted = []
        gcf_count = 0
        all_gcfs = set()

        # for each original input object
        for spec, link_data in self.score_helper.scoring_results.links.items():
            hdr_id = 'spec_result_header_{}'.format(pgindex)
            body_id = 'spec_result_body_{}'.format(pgindex)

            title = '{} GCFs linked to {}'.format(len(link_data), spec)

            all_gcfs.update(link_data.keys())


            body = 'Spectrum information'
            body += self.generate_spec_info(spec)
            
            # set up the chemdoodle plot so it appears when the entry is expanded 
            btn_id = 'spec_btn_{}'.format(pgindex)
            plot_id = 'spec_plot_{}'.format(pgindex)
            body += '<center><canvas id="{}"></canvas></center>'.format(plot_id)
            # note annoying escaping required here, TODO better way of doing this?
            onclick = 'setupPlot(\'{}\', \'{}\', \'{}\');'.format(btn_id, plot_id, spec.to_jcamp_str())
    
            body += '<hr/><h5>Linked objects:</h5>'
            body += self.generate_spec_gcf_result(pgindex, spec, link_data)

            unsorted.append((len(link_data), TMPL_ON_CLICK.format(hdr_id=hdr_id, hdr_color='b4c4e8', 
                                                         btn_target=body_id, btn_onclick=onclick, btn_id=btn_id, 
                                                         btn_text=title, span_badges='', body_id=body_id, 
                                                         body_parent='accordionResults', body_body=body)))

            pgindex += 1
        
        content = '<h4>Spectra={}, linked GCFs={}</h4>'.format(len(self.score_helper.scoring_results), len(all_gcfs))

        for _, text in sorted(unsorted, key=lambda x: -x[0]):
            content += text

        return content

    def update_results_molfam_gcf(self):
        return '' # TODO

    def update_results(self, div):
        """
        Called after scoring is completed. Generates HTML to display results and 
        assigns it to the content of a bokeh div to insert into the DOM
        """

        # if selections have been cleared or are empty, then clear the output from last time
        if len(self.ds_bgc.selected.indices) == 0 and len(self.ds_spec.selected.indices) == 0:
            div.text = RESULTS_PLACEHOLDER
            return

        content = ''
        if self.score_helper.mode == SCO_MODE_BGC_SPEC or self.score_helper.mode == SCO_MODE_GCF_SPEC:
            # both of these have the same format
            self.debug_log('update_results_gcf_spec')
            content = self.update_results_gcf_spec()
        elif self.score_helper.mode == SCO_MODE_BGC_MOLFAM or self.score_helper.mode == SCO_MODE_GCF_MOLFAM:
            # as do these
            self.debug_log('update_results_gcf_molfam')
            content = self.update_results_gcf_molfam()
        elif self.score_helper.mode == SCO_MODE_SPEC_GCF:
            self.debug_log('update_results_spec_gcf')
            content = self.update_results_spec_gcf()
        elif self.score_helper.mode == SCO_MODE_MOLFAM_GCF:
            self.debug_log('update_results_molfam_gcf')
            content = self.update_results_molfam_gcf()
        else:
            self.debug_log('Unknown scoring mode selected! {}'.format(self.score_helper.mode))

        div.text = content

    def get_links(self):
        """
        Handles the scoring process
        """
        nplinker = self.nh.nplinker
        sel_indices = []

        self.debug_log('Starting scoring process', False)

        # first check the scoring mode, which is determined by the state 
        # of the two sets of UI controls. based on the current mode, 
        # extract the appropriate set of selected indices from the plot
        bgcs = []
        spectra = []
        if self.score_helper.from_genomics:
            sel_indices = self.ds_bgc.selected.indices
            for i in range(len(sel_indices)):
                bgcs.append(nplinker.lookup_bgc(self.bgc_data['name'][sel_indices[i]]))
        else:
            sel_indices = self.ds_spec.selected.indices
            for i in range(len(sel_indices)):
                spectra.append(nplinker.lookup_spectrum(self.spec_data['name'][sel_indices[i]]))

        mode = self.score_helper.mode

        if len(sel_indices) == 0:
            self.update_alert('No objects selected', 'danger')
            self.debug_log('No objects selected')
            return

        # obtain a list of objects to be passed to the scoring function
        scoring_objs = self.score_helper.generate_scoring_objects(bgcs, spectra, self.bgc_tsne_id)
        if scoring_objs is None:
            self.update_alert('ERROR: something went wrong in scoring!', 'danger')
            self.clear_selections()
            return

        # an extra step before continuing for some scoring modes is to expand the 
        # set of selected objects. For example, in GCF->Spec mode, the initial selection
        # of BGCs may grow as a result of selecting all GCFs containing any of those
        # BGCs (and similarly for MolFam selection modes with Spectra)
        # TODO this will also need done for GCF->MolFam and MolFam->GCF
        if mode == SCO_MODE_GCF_SPEC:
            self.debug_log('Updating selection for GCF->Spec mode to include {} BGCs'.format(len(self.score_helper.bgcs)))
            # disable the selection change listener briefly
            self.configure_bgc_selchanged_listener(False)
            # need to lookup the indices of each BGC in the bokeh datasource to get the indices
            sel_indices = []
            lookup = self.nh.bgc_indices[self.bgc_tsne_id]
            for bgc in self.score_helper.bgcs:
                if bgc.name not in lookup:
                    # this might happen due to TSNE projection in use, or ???
                    self.debug_log('WARNING: missing BGC in bgc_indices = {}'.format(bgc.name))
                    continue

                sel_indices.append(lookup[bgc.name])
            self.ds_bgc.selected.indices = sel_indices
            self.debug_log('Finished updating selection')
            self.configure_bgc_selchanged_listener(True)

        current_method = self.current_scoring_methods()
        scoring_results = nplinker.get_links(scoring_objs, current_method)

        self.display_links(scoring_results, mode, self.results_div)

    def display_links(self, scoring_results, mode, div, include_only=None):
        nplinker = self.nh.nplinker
        current_methods = self.current_scoring_methods()
        selected = set()
        all_shared_strains = {}

        # 1. need to filter out linked objects that don't appear in the tables 
        # in their current state
        if include_only is not None:
            scoring_results.filter_targets(lambda x: x in include_only)

        # 2. if the linked objects are GCFs, need to filter out those results for
        # which the GCFs are not currently "available" due to possible use of a
        # limited projection (this is tied to old TSNE plot projection stuff but
        # keeping it here in case still useful)
        if mode == SCO_MODE_SPEC_GCF or mode == SCO_MODE_MOLFAM_GCF:
            scoring_results.filter_targets(lambda x: x in self.nh.available_gcfs[self.bgc_tsne_id])

        # 3. filter out any results with no shared strains, if that option is enabled
        if self.filter_no_shared_strains:
            scoring_results.filter_no_shared_strains()

        # final step here is to take the remaining list of scoring objects and map them
        # back to indices on the plot so they can be highlighted
        # NOTE: extra step required if hide_singletons/hide_mibig are True. in this situation
        # need to filter out such objects from results too
        self.debug_log('finding indices')
        objs_to_remove = []
        score_obj_indices = []
        if mode == SCO_MODE_BGC_SPEC or mode == SCO_MODE_GCF_SPEC:
            # for these modes, we can just lookup spectrum indices directly

            # filter out spectra from singleton families if necessary
            def filter_singletons(link):
                return not isinstance(link.target.family, SingletonFamily)
            if self.hide_singletons:
                scoring_results.filter_links(filter_singletons)

            for target in scoring_results.get_all_targets():
                if target.spectrum_id not in self.spec_indices:
                    self.debug_log('Warning: missing index for Spectrum: {}'.format(target))
                    continue
                score_obj_indices.append(self.spec_indices[target.spectrum_id])
        elif mode == SCO_MODE_SPEC_GCF or mode == SCO_MODE_MOLFAM_GCF:
            # here we have a list of GCFs, but the plot shows BGCs. so in order
            # to highlight the BGCs, need to iterate over the list of GCFs 
            score_obj_indices = set()

            # filter out GCFs that only contain MiBIG BGCs if necessary
            def filter_mibig(gcf):
                return not gcf.only_mibig()
            if self.hide_mibig:
                scoring_results.filter_targets(filter_mibig)

            for target in scoring_results.get_all_targets():
                # get list of BGCs from the GCF. if we're not showing MiBIG BGCs and these
                # are the only ones in this GCF, don't show it 
                bgcs = target.bgcs if not self.hide_mibig else target.non_mibig_bgcs
                for n in bgcs:
                    try:
                        score_obj_indices.add(self.bgc_indices[n.name])
                    except KeyError:
                        self.debug_log('Warning: missing index for BGC: {} in GCF: {}'.format(n.name, target))
            score_obj_indices = list(score_obj_indices)

        # add these indices to the overall set
        selected.update(score_obj_indices)

        if len(scoring_results) > 0:
            self.debug_log('set_results')
            self.score_helper.set_results(scoring_results)
        else:
            self.score_helper.clear()

        self.debug_log('update_ui_post_scoring')
        self.update_ui_post_scoring(selected, div)

    def update_ui_post_scoring(self, selected_indices, div):
        if len(self.score_helper.scoring_results) > 0:
            # take the indexes of the corresponding spectra from the datasource and highlight on the other plot
            # also need to hide edges manually
            if self.score_helper.gen_to_met():
                # first select the set of nodes on the linked plot (spectra here)
                self.ds_spec.selected.indices = list(selected_indices)
                # then also select the associated set of edges on the same plot
                self.ds_edges_spec.selected.indices = self.nh.get_spec_edges(selected_indices)
                # hide the renderer to show no edges if none are selected (otherwise they all appear)
                self.ren_spec.edge_renderer.visible = len(self.ds_edges_spec.selected.indices) > 0
                # finally, need to select the set of edges associated with the nodes on 
                # the source plot (BGCs here), or hide them all if none selected
                self.ds_edges_bgc.selected.indices = self.nh.get_bgc_edges(self.ds_bgc.selected.indices)
                self.ren_bgc.edge_renderer.visible = len(self.ds_edges_bgc.selected.indices) > 0
            else:
                # first select the set of nodes on the linked plot (BGCs here)
                self.ds_bgc.selected.indices = list(selected_indices)
                # then also select tee associated set of edges on the same plot
                self.ds_edges_bgc.selected.indices = self.nh.get_bgc_edges(selected_indices)
                # hide the renderer if no edges visible
                self.ren_bgc.edge_renderer.visible = len(self.ds_edges_bgc.selected.indices) > 0
                # finally, need to select the set of edges associated with the nodes on
                # the source plot (spectra here), or hide them all if none selected
                self.ds_edges_spec.selected.indices = self.nh.get_spec_edges(self.ds_spec.selected.indices)
                self.ren_spec.edge_renderer.visible = len(self.ds_edges_spec.selected.indices) > 0

            self.update_alert('{} objects with links found'.format(len(self.score_helper.scoring_results)), 'primary')
        else:
            self.debug_log('No links found!')
            # clear any selection on the "output" plot
            if self.score_helper.gen_to_met():
                self.ds_spec.selected.indices = []
                self.ds_edges_spec.selected.indices = []
                self.ren_spec.edge_renderer.visible = True
            else:
                self.ds_bgc.selected.indices = []
                self.ds_edges_bgc.selected.indices = []
                self.ren_bgc.edge_renderer.visible = True
            self.update_alert('No links found for last selection', 'danger')

        self.debug_log('update_results')
        self.update_results(div)

    def bgc_selchanged(self, attr, old, new):
        """
        Callback for changes to the BGC datasource selected indices
        """
        if self.suppress_selection_bgc:
            print('IGNORING SELECTION spec_selchanged')
            return

        # if the new selection contains any valid indices (it may be empty)
        if len(new) > 0:
            # get links for the selected BGCs and update the plots
            self.get_links()
        else:
            # if selection is now empty, clear the selection on the spectra plot too
            self.ds_spec.selected.indices = []
            self.ds_edges_spec.selected.indices = []
            self.ren_spec.edge_renderer.visible = True
            self.ren_bgc.edge_renderer.visible = True

            self.update_results(self.results_div)

    def spec_selchanged(self, attr, old, new):
        """
        Callback for changes to the Spectra datasource selected indices
        """
        if self.suppress_selection_spec:
            print('IGNORING SELECTION spec_selchanged')
            return

        # if the new selection contains any valid indices (it may be empty)
        if len(new) > 0:
            # get links for the selected spectra and update the plots
            self.get_links()
        else:
            # if selection is now empty, clear the selection on the BGC plot too
            self.ds_bgc.selected.indices = []
            self.ds_edges_bgc.selected.indices = []
            self.ren_bgc.edge_renderer.visible = True
            self.ren_spec.edge_renderer.visible = True

            self.update_results(self.results_div)

    def metcalf_standardised_callback(self, attr, old, new):
        obj = self.scoring_objects['metcalf']
        # don't attempt to set the same value it already has 
        # (this is using 0 = False, 1 = True)
        if obj.standardised == new:
            return
        obj.standardised = (new == 1)
        self.get_links()

    def metcalf_cutoff_callback(self, attr, old, new):
        self.scoring_objects['metcalf'].cutoff = new
        self.get_links()

    def testscore_value_callback(self, attr, old, new):
        self.scoring_objects['testscore'].value = new
        self.get_links()
        
    def scoring_method_callback(self, attr, old, new):
        # update visibility of buttons
        for i in range(len(self.scoring_methods)):
            self.scoring_method_buttons[self.scoring_methods[i]].visible = i in new
        # self.get_links()

    def clear_selections(self):
        self.ds_bgc.selected.indices = []
        self.ds_spec.selected.indices = []
        self.ds_edges_bgc.selected.indices = []
        self.ds_edges_spec.selected.indices = []
        self.ren_bgc.edge_renderer.visible = True
        self.ren_spec.edge_renderer.visible = True

    def get_scoring_mode_text(self):
        return SCO_MODE_NAMES[self.score_helper.mode]

    def sco_mode_changed(self):
        self.update_alert('Scoring mode is now <strong>{}</strong>'.format(self.get_scoring_mode_text()))

    def mb_mode_callback(self, attr, old, new):
        # only change active plot (and clear selections) if going from
        # genomics mode -> metabolomics mode
        if self.score_helper.from_genomics:
            self.set_inactive_plot(self.fig_bgc)
        self.score_helper.update_metabolomics(new)
        self.sco_mode_changed()
        self.update_plot_select_state(False)
        # update scoring based on current selection + new mode
        self.get_links()

    def ge_mode_callback(self, attr, old, new):
        # only change active plot (and clear selections) if going from
        # metabolomics mode -> genomics mode
        if not self.score_helper.from_genomics:
            self.set_inactive_plot(self.fig_spec)
        self.score_helper.update_genomics(new)
        self.sco_mode_changed()
        self.update_plot_select_state(True)
        # update scoring based on current selection + new mode
        self.get_links()

    def plot_toggles_callback(self, attr, old, new):
        if PLOT_MIBIG_BGCS in new and PLOT_MIBIG_BGCS not in old:
            self.ren_bgc.node_renderer.view.filters = []
            self.ren_bgc.edge_renderer.view.filters = []
            self.fig_bgc.x_range.start, self.fig_bgc.x_range.end = self.nh.plot_x_range(True)
            self.fig_bgc.y_range.start, self.fig_bgc.y_range.end = self.nh.plot_y_range(True)
            self.hide_mibig = False
        elif PLOT_MIBIG_BGCS in old and PLOT_MIBIG_BGCS not in new:
            self.ren_bgc.node_renderer.view.filters = [self.bgc_index_filter]
            self.ren_bgc.edge_renderer.view.filters = [self.bgc_edges_index_filter]
            self.fig_bgc.x_range.start, self.fig_bgc.x_range.end = self.nh.plot_x_range(False)
            self.fig_bgc.y_range.start, self.fig_bgc.y_range.end = self.nh.plot_y_range(False)
            self.hide_mibig = True

        if PLOT_SINGLETONS in new:
            self.ren_spec.node_renderer.view.filters = []
            self.ren_spec.edge_renderer.view.filters = []
            self.fig_spec.x_range.start, self.fig_spec.x_range.end = self.nh.plot_x_range(True)
            self.fig_spec.y_range.start, self.fig_spec.y_range.end = self.nh.plot_y_range(True)
            self.hide_singletons = False
        elif PLOT_SINGLETONS in old and PLOT_SINGLETONS not in new:
            self.ren_spec.node_renderer.view.filters = [self.spec_index_filter]
            self.ren_spec.edge_renderer.view.filters = [self.spec_edges_index_filter]
            self.fig_spec.x_range.start, self.fig_spec.x_range.end = self.nh.plot_x_range(False)
            self.fig_spec.y_range.start, self.fig_spec.y_range.end = self.nh.plot_y_range(False)
            self.hide_mibig = True

        if PLOT_PRESERVE_COLOUR in new and PLOT_PRESERVE_COLOUR not in old:
            self.ren_bgc.selection_glyph.fill_color = 'fill'
            self.ren_spec.selection_glyph.fill_color = 'fill'
        elif PLOT_PRESERVE_COLOUR in old and PLOT_PRESERVE_COLOUR not in new:
            self.ren_bgc.selection_glyph.fill_color = '#ff7f00'
            self.ren_spec.selection_glyph.fill_color = '#ff7f00'

        self.filter_no_shared_strains = PLOT_ONLY_SHARED_STRAINS in new

    def tsne_id_callback(self, attr, old, new):
        self.clear_selections()
        self.bgc_tsne_id = new
        self.update_alert('TSNE set to {}'.format(new), 'primary')
        # messy but it works and seems to be best way of doing this
        newdata = self.nh.bgc_data[new]
        self.bgc_datasource.data.update(
                                            x=newdata['x'],
                                            y=newdata['y'],
                                            name=newdata['name'],
                                            strain=newdata['strain'],
                                            gcf=newdata['gcf'],
                                            fill=newdata['fill'])

    def update_alert(self, msg, alert_class='primary'):
        self.alert_div.text = '<div class="alert alert-{}" role="alert">{}</div>'.format(alert_class, msg)

    def clear_alert(self):
        self.alert_div.text = ''

    def debug_log(self, msg, append=True):
        if append:
            self.debug_div.text += '{}<br/>'.format(msg)
        else:
            self.debug_div.text = '{}</br>'.format(msg)
        print('debug_log: {}'.format(msg))

    def update_plot_select_state(self, gen_to_met):
        if gen_to_met:
            self.plot_select.label = SCORING_GEN_TO_MET
            self.plot_select.css_classes = ['button-genomics']
        else:
            self.plot_select.label = SCORING_MET_TO_GEN
            self.plot_select.css_classes = ['button-metabolomics']

    def plot_select_callback(self, val):
        if val: # switch to genomics selection mode
            self.score_helper.set_genomics()
            self.set_inactive_plot(self.fig_spec)
        else: # metabolomics selection mode
            self.score_helper.set_metabolomics()
            self.set_inactive_plot(self.fig_bgc)
        self.update_plot_select_state(val)
        self.sco_mode_changed()

    def search_type_callback(self, attr, old, new):
        print('Search type is now: {}={}'.format(new, SEARCH_OPTIONS.index(new)))

    def generate_bgc_info(self, bgc):
        bgc_body = '<ul>'
        for attr in ['strain', 'name', 'bigscape_class', 'product_prediction', 'description']:
            bgc_body += '<li><strong>{}</strong>: {}</li>'.format(attr, getattr(bgc, attr))
        bgc_body += '</ul>'
        return bgc_body

    def generate_gcf_info(self, gcf, shared_strains=None):
        gcf_body = '<ul class="nav nav-tabs" id="gcf_{}_tabs" role="tablist">'.format(gcf.id)
        gcf_body += '<li class="nav-item"><a class="nav-link active" id="gcf_{}_main_tab" data-toggle="tab" href="#gcf_{}_main" role="tab">Main</a></li>'.format(gcf.id, gcf.id)
        gcf_body += '<li class="nav-item"><a class="nav-link" id="gcf_{}_bgcs_tab" data-toggle="tab" href="#gcf_{}_bgcs" role="tab">BGCs</a></li>'.format(gcf.id, gcf.id)
        gcf_body += '</ul><div class="tab-content" id="gcf_{}_tab_content">'.format(gcf.id)

        # start of main tab content
        gcf_body += '<div class="tab-pane show active" id="gcf_{}_main" role="tabpanel">'.format(gcf.id)
        gcf_body += '<ul>'
        for attr in ['id', 'gcf_id', 'product_type']:
            gcf_body += '<li><strong>{}</strong>: {}</li>'.format(attr, getattr(gcf, attr))

        # add strain information
        if shared_strains is not None and len(shared_strains) > 0:
            gcf_body += '<li><strong>strains (total={}, shared={})</strong>: '.format(len(gcf.bgcs), len(shared_strains))

            non_shared = [s.strain for s in gcf.bgcs if s.strain not in shared_strains]

            for s in shared_strains:
                gcf_body += '<span style="background-color: #AAFFAA">{}</span>, '.format(s.id)
            for s in non_shared:
                gcf_body += '<span style="background-color: #DDDDDD">{}</span>, '.format(s.id)

            gcf_body += '</li>'
        else:
            gcf_body += '<li><strong>strains (total={}, shared=0)</strong>: '.format(len(gcf.bgcs))

            for s in gcf.bgcs:
                gcf_body += '<span>{}</span>, '.format(s.strain.id)

            gcf_body += '</li>'

        gcf_body += '</ul>'
        gcf_body += '</div>'

        # second tab content
        fields = ['name', 'strain', 'description', 'bigscape_class', 'product_prediction']
        gcf_body += '<div class="tab-pane" id="gcf_{}_bgcs" role="tabpanel">'.format(gcf.id)
        gcf_body += 'Type to filter: <input type="text" onkeyup="onChangeBGCTable(\'#gcf_{}_bgc_table\', \'#gcf_{}_bgc_search\')" id="gcf_{}_bgc_search">'.format(gcf.id, gcf.id, gcf.id)
        gcf_body += '<table class="table table-responsive table-striped" id="gcf_{}_bgc_table"><thead><tr>'.format(gcf.id)
        gcf_body += ''.join('<th scope="col">{}</th>'.format(x) for x in fields)
        gcf_body += '</thead><tbody>'
        for bgc in gcf.bgcs:
            gcf_body += '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(*list(getattr(bgc, x) for x in fields))
        gcf_body += '</tbody></table>'
        gcf_body += '</div></div>'

        return gcf_body

    def generate_spec_info(self, spec, shared_strains=None):
        spec_body = '<ul>'
        for attr in ['id', 'spectrum_id', 'family', 'rt', 'total_ms2_intensity', 'max_ms2_intensity', 'n_peaks', 'precursor_mz', 'parent_mz']:
            spec_body += '<li><strong>{}</strong>: {}</li>'.format(attr, getattr(spec, attr))

        # TODO possibly going to need updated later
        if 'ATTRIBUTE_SampleType' in spec.metadata:
            spec_body += '<li><strong>SampleType</strong>: {}</li>'.format(', '.join(spec.metadata['ATTRIBUTE_SampleType']))

        # add strain information (possibly empty)
        if shared_strains is not None and len(shared_strains) > 0:
            spec_body += '<li><strong>strains (total={}, shared={})</strong>: '.format(len(spec.strain_list), len(shared_strains))

            non_shared = [s for s in spec.strain_list if s not in shared_strains]

            for s in shared_strains:
                spec_body += '<span style="background-color: #AAFFAA">{} ({})</span>, '.format(s.id, spec.get_growth_medium(s))
            for s in non_shared:
                spec_body += '<span style="background-color: #DDDDDD">{} ({})</span>, '.format(s.id, spec.get_growth_medium(s))

            spec_body += '</li>'
        else:
            spec_body += '<li><strong>strains (total={}, shared=0)</strong>: '.format(len(spec.strain_list))

            for s in spec.strain_list:
                spec_body += '<span>{} ({})</span>, '.format(s.id, spec.get_growth_medium(s))

            spec_body += '</li>'
        spec_body += '</ul>'

        if len(spec.annotations) > 0:
            # keys of spec.annotations indicate the source file: "gnps" or an actual filename
            for anno_src, anno_list in spec.annotations.items():
                spec_body += '<hr width="80%"/>'
                spec_body += '<strong>{} annotations:</strong><ul>'.format(anno_src)
                for item in anno_list:
                    spec_body += '<ul>'
                    content = []
                    for k, v in item.items():
                        if k.endswith('_url'):
                            v = '<a href="{}" target="_blank">{}</a>'.format(v, k)
                        elif v.startswith('CCMSLIB'):
                            v = '<a href="{}" target="_blank">{}</a>'.format(gnps_url(v), v)
                        content.append('<strong><span class="annotation">{}</span></strong> = {}'.format(k, v))
                    spec_body += '<li>' + ', '.join(content) + '</li>'
                    spec_body += '</ul>'
                spec_body += '</ul>'
        return spec_body

    def generate_molfam_info(self, molfam, shared_strains=[]):
        pass # TODO

    def generate_search_output_bgc(self, bgcs):
        body = ''
        for i, bgc in enumerate(bgcs):
            bgc_hdr_id = 'bgc_search_header_{}'.format(i)
            bgc_body_id = 'bgc_search_body_{}'.format(i)
            bgc_title = '{}(name={})'.format(bgc.__class__.__name__, bgc.name)
            bgc_body = self.generate_bgc_info(bgc)
            body += TMPL_SEARCH.format(hdr_id=bgc_hdr_id, hdr_color='dddddd', btn_target=bgc_body_id, btn_text=bgc_title, 
                                result_index=str(i), body_id=bgc_body_id, body_parent='accordionSearch', body_body=bgc_body)

        
        return body

    def generate_search_output_gcf(self, gcfs):
        body = ''
        for i, gcf in enumerate(gcfs):
            gcf_hdr_id = 'gcf_search_header_{}'.format(i)
            gcf_body_id = 'gcf_search_body_{}'.format(i)
            gcf_title = 'GCF(gcf_id={}, strains={})'.format(gcf.gcf_id, len(gcf.bgcs))
            gcf_body = self.generate_gcf_info(gcf)
            body += TMPL_SEARCH.format(hdr_id=gcf_hdr_id, hdr_color='dddddd', btn_target=gcf_body_id, btn_text=gcf_title, 
                                result_index=str(i), body_id=gcf_body_id, body_parent='accordionSearch', body_body=gcf_body)

        return body


    def generate_search_output_spec(self, spectra):
        body = ''
        for i, spec in enumerate(spectra):
            spec_hdr_id = 'spec_search_header_{}'.format(i)
            spec_body_id = 'spec_search_body_{}'.format(i)
            spec_title = 'Spectrum(parent_mass={:.4f}, id={}, strains={})'.format(spec.parent_mz, spec.spectrum_id, len(spec.strain_list))

            spec_body = self.generate_spec_info(spec)

            # set up the chemdoodle plot so it appears when the entry is expanded 
            spec_btn_id = 'spec_search_btn_{}'.format(i)
            spec_plot_id = 'spec_search_plot_{}'.format(i)
            spec_body += '<center><canvas id="{}"></canvas></center>'.format(spec_plot_id)

            # note annoying escaping required here, TODO better way of doing this?
            spec_onclick = 'setupPlot(\'{}\', \'{}\', \'{}\');'.format(spec_btn_id, spec_plot_id, spec.to_jcamp_str())

            body += TMPL_SEARCH_ON_CLICK.format(hdr_id=spec_hdr_id, hdr_color='dddddd', btn_target=spec_body_id, btn_onclick=spec_onclick, btn_id=spec_btn_id, 
                                         result_index=str(i), btn_text=spec_title, body_id=spec_body_id, body_parent='accordionSearch', body_body=spec_body)

        return body

    def generate_search_output_molfam(self, molfams):
        # TODO
        return '<strong>Not working yet :(</strong>'

    def generate_search_output(self, results):
        # TEMP XXX TODO
        # if len(results) > 0 and isinstance(results[0], BGC):
        #     # removing BGCs that don't appear in current TSNE
        #     results = [r for r in results if r in self.nh.bgc_indices[self.bgc_tsne_id]]
        #     self.searcher.results = results

        hdr = '<h4>{} results found</h4>'.format(len(results))
        if len(results) == 0:
            self.search_div_header.text = hdr
            self.search_div_results.text = ''
            return

        hdr = '<h4><button type="button" class="btn btn-info" onclick="toggleSearchSelect()">{} results found, click to select/deselect all</button></h4>'.format(len(results))
        self.search_div_header.text = hdr

        html = ''
        if isinstance(results[0], BGC):
            html += self.generate_search_output_bgc(results)
        elif isinstance(results[0], GCF):
            html += self.generate_search_output_gcf(results)
        elif isinstance(results[0], Spectrum):
            html += self.generate_search_output_spec(results)
        elif isinstance(results[0], MolecularFamily):
            html += self.generate_search_output_molfam(results)
            
        self.search_div_results.text = html

    def search_button_callback(self):
        self.generate_search_output(self.searcher.search(self.search_type.value, self.search_input.value, len(self.search_regex.active) == 1))

    def search_score_button_callback(self):
        # check if the user responded yes/no to query (the JS callback for this button 
        # is triggered before the python callback)
        if not self.confirm_response.data['resp'][0]:
            print('Bailing out due to JS confirmation')
            return

        # retrieve the set of selected indices from the Javascript callback and create subset of results
        selected_indices = list(map(int, self.confirm_response.data['selected'][0]))
        results = [self.searcher.results[i] for i in selected_indices]
        if len(results) == 0:
            self.update_alert('No search results!')
            return

        # may need to change scoring mode here
        # if results are BGCs, switch to genomics mode and select the BGC mode
        # TODO other modes to be handled
        if isinstance(results[0], BGC):
            print('scoring based on bgc results')
            print('current scoring mode gen? {}'.format(self.score_helper.from_genomics))
            if not self.score_helper.from_genomics:
                print('switching to genomics mode')
                # set this to true to trigger callback as if button clicked
                self.plot_select.active = True
                self.update_plot_select_state(True)
            # now select bgc mode
            self.ge_mode_group.active = 0

            # and finally update selection to trigger scoring
            print('constructing indices from search results, BGC mode')
            indices = [self.nh.bgc_indices[self.bgc_tsne_id][bgc] for bgc in results]
            self.ds_bgc.selected.indices = indices
        # if results are GCFs, switch to genomics mode and select the GCF mode
        elif isinstance(results[0], GCF):
            print('scoring based on gcf results')
            print('current scoring mode gen? {}'.format(self.score_helper.from_genomics))
            if not self.score_helper.from_genomics:
                print('switching to genomics mode')
                # set this to true to trigger callback as if button clicked
                self.plot_select.active = True
                self.update_plot_select_state(True)
            # now select gcf mode
            self.ge_mode_group.active = 1

            # and finally update selection to trigger scoring, by building a list
            # of all the BGC indices corresponding to these GCFs
            bgcs = set()
            for gcf in results:
                bgcs.update(gcf.bgcs)
            indices = [self.nh.bgc_indices[self.bgc_tsne_id][bgc.name] for bgc in bgcs]
            self.ds_bgc.selected.indices = indices
        elif isinstance(results[0], Spectrum):
            print('scoring based on spectra results')
            print('current scoring mode met? {}'.format(not self.score_helper.from_genomics))

            if self.score_helper.from_genomics:
                print('switching to metabolomics mode')
                # set this to False to trigger callback as if button clicked
                self.plot_select.active = False
                self.update_plot_select_state(False)
            # now select spectrum mode
            self.mb_mode_group.active = 0

            # finally update the selection to trigger scoring
            indices = [self.nh.spec_indices[spec.spectrum_id] for spec in results]
            self.ds_spec.selected.indices = indices

    def reset_button_callback(self):
        self.clear_selections()

    def gnps_params_select_callback(self, attr, old, new):
        self.gnps_params_value.text = '<strong>{}</strong> = {}'.format(new, self.nh.nplinker.gnps_params[new])

    def current_scoring_method_names(self):
        print('NAMES', [self.scoring_methods[x] for x in self.scoring_method_checkboxes.active])
        return [self.scoring_methods[x] for x in self.scoring_method_checkboxes.active]

    def current_scoring_methods(self):
        print('METHODS', [self.scoring_objects[x] for x in self.current_scoring_method_names()])
        return [self.scoring_objects[x] for x in self.current_scoring_method_names()]

    def tables_score_met_callback(self):
        """
        Handles clicks on the "Show scores for selected spectra" button (tables mode)
        """
        print('Currently selected spectra: {}'.format(len(self.table_session_data.spec_ds.data['spectrum_id'])))
        print('Actual selections: {}'.format(len(self.table_session_data.spec_ds.selected.indices)))

        if len(self.table_session_data.spec_ds.selected.indices) == 0 and len(self.table_session_data.molfam_ds.selected.indices) == 0:
            self.hidden_alert_thing.text = 'Error: no spectra have been selected!\n\n' +\
                                            'Running scoring on {} objects may take a long time!\n'.format(len(self.table_session_data.spec_ds.data['spectrum_id'])) +\
                                             'Please make some selections and then try again.'
            # this resets the text without triggering the alert() again, so that NEXT time this happens
            # the text-changed event will be fired by the above line. yes it's a mess
            self.hidden_alert_thing.text = ''
            return

        # need to build a list of the NPLinker Spectrum objects corresponding to
        # the set of objects currently listed in the table
        # but first need to decide which set of spectra to use:
        # - the user might have selected a MolFam, in which case the spec table will have
        #   filtered down to show only the spectra in that family, BUT none of them will
        #   actually be selected, so just take all visible entries
        # - alternatively they might have selected a spectrum directly, in which case they
        #   will all still be visible but we only want the selected indices
        selected_spectra = []
        if len(self.table_session_data.spec_ds.selected.indices) > 0:
            # assume there's been an explicit selection and use those objects
            for index in self.table_session_data.spec_ds.selected.indices:
                sid = self.table_session_data.spec_ds.data['spec_pk'][index] # the internal nplinker ID
                if sid == NA_ID:
                    continue
                selected_spectra.append(self.nh.nplinker.spectra[int(sid)])
        else:
            for sid in self.table_session_data.spec_ds.data['spec_pk']: # internal nplinker IDs
                if sid == NA_ID:
                    continue
                spec = self.nh.nplinker.spectra[int(sid)] 
                selected_spectra.append(spec)

        # retrieve the currently active scoring method(s)
        current_methods = self.current_scoring_methods()
        
        # call get_links, which returns the subset of input objects which have links
        results = self.nh.nplinker.get_links(selected_spectra, current_methods)

        # TODO quick attempt to display scores in the tables (for metcalf only for now)
        metcalf_obj = self.scoring_objects['metcalf']
        # check if metcalf scoring is enabled at the moment
        if metcalf_obj in current_methods:
            # create a dict mapping GCFs in the results to their scores
            gcf_scores = {}
            for spec, result in results.links.items():
                for gcf, link_data in result.items():
                    metcalf_score = link_data.data(metcalf_obj)
                    gcf_scores[gcf] = metcalf_score

            # get a list of the GCFs currently visible in the table (note that
            # these are the actual GCF IDs, not the internal nplinker IDs)
            visible_gcfs = map(int, self.table_session_data.gcf_ds.data['gcf_id'])

            # update the data source with the scores
            self.table_session_data.gcf_ds.data['metcalf_score'] = [gcf_scores[self.nh.nplinker.lookup_gcf(gcf_id)] for gcf_id in visible_gcfs]

        # next, need to display the results using the same code as for generating the 
        # results from plot selections, but without actually triggering any unintended
        # side-effects (changing the plot selections and so on)

        # the plots are configured to update automatically when changes are made
        # to their underlying datasource (e.g. new selections). want to avoid this
        # happening here because in tables mode the plots aren't visible. this is 
        # a quick hack that just prevents the selection callback from doing anything
        # for the duration of this operation
        # TODO this should be more decoupled anyway
        self.suppress_selection_spec = True

        # the next step, before actually displaying the links, is to make sure
        # the webapp scoring mode is set to Spec->GCF. this is currently done by
        # simulating a click on the appropriate radio button, which will handle
        # the rest of the changes required
        self.mb_mode_callback(None, None, 0)

        # TODO another issue is that the various methods involved here tend to 
        # refer to member variables directly instead of having parameters passed 
        # in, which would make them easier to repurpose (i.e. they use self.ds_spec
        # instead of a generic <datasource> parameter)
        # 
        # update the set of selected indices on the spectral datasource (equivalent
        # to the user having made the same selection via the plot)
        # TODO need to allow for AND/OR here
        result_indices = [self.spec_indices[spec.spectrum_id] for spec in results.sources]
        self.ds_spec.selected.indices = list(result_indices) 

        # the ScoringHelper object expects to have this list of objects available so need to set that
        self.score_helper.spectra = selected_spectra

        # get the list of visible GCFs so we can filter out any others from the results
        include_only = set([self.nh.nplinker.gcfs[int(gcf_id)] for gcf_id in self.table_session_data.gcf_ds.data['gcf_pk'] if gcf_id != '-'])

        # FINALLY, display the link information
        self.display_links(results, mode=SCO_MODE_SPEC_GCF, div=self.results_div, include_only=include_only)

        # and then remove the hacky workaround
        self.suppress_selection_spec = False
        
    def tables_score_gen_callback(self):
        """
        Handles clicks on the "Show scores for selected GCFs" button (tables mode)
        """

        print('Currently selected bgcs: {}'.format(len(self.table_session_data.bgc_ds.data['bgc_pk'])))
        print('Actual selections: {}'.format(len(self.table_session_data.bgc_ds.selected.indices)))

        if len(self.table_session_data.bgc_ds.selected.indices) == 0 and len(self.table_session_data.gcf_ds.selected.indices) == 0:
            self.hidden_alert_thing.text = 'Error: no BGCs have been selected!\n\n' +\
                                            'Running scoring on {} objects may take a long time!\n'.format(len(self.table_session_data.bgc_ds.data['bgc_pk'])) +\
                                             'Please make some selections and then try again.'
            # this resets the text without triggering the alert() again, so that NEXT time this happens
            # the text-changed event will be fired by the above line. yes it's a mess
            self.hidden_alert_thing.text = ''
            return

        # need to build a list of the NPLinker GCF objects corresponding to
        # the set of BGC objects currently listed in the table
        # but first need to decide which set of objects to use:
        # - the user might have selected a GCF, in which case the BGC table will have
        #   filtered down to show only the BGCs in that family, BUT none of them will
        #   actually be selected, so just take all visible entries
        # - alternatively they might have selected a BGC directly, in which case they
        #   will all still be visible but we only want the selected indices
        selected_gcfs = []
        selected_bgcs = []
        gcfs = set() # to remove dups 

        if len(self.table_session_data.bgc_ds.selected.indices) > 0:
            # assume there's been an explicit selection and use those objects
            for index in self.table_session_data.bgc_ds.selected.indices:
                bgcid = self.table_session_data.bgc_ds.data['bgc_pk'][index] # internal nplinker ID
                if bgcid == NA_ID:
                    continue
                bgc = self.nh.nplinker.bgcs[int(bgcid)]
                selected_bgcs.append(bgc)
                gcfs.add(bgc.parent)
        else:
            for bgcid in self.table_session_data.bgc_ds.data['bgc_pk']: # internal nplinker IDs
                if bgcid == NA_ID:
                    continue
                bgc = self.nh.nplinker.bgcs[int(bgcid)]
                selected_bgcs.append(bgc)
                gcfs.add(bgc.parent)

        selected_gcfs = list(gcfs)

        # retrieve the currently active scoring method (for now this will always be metcalf)
        current_methods = self.current_scoring_methods()
        
        # call get_links, which returns the subset of input objects which have links
        results = self.nh.nplinker.get_links(selected_gcfs, current_methods)

        # TODO quick attempt to display scores in the tables (for metcalf only for now)
        metcalf_obj = self.scoring_objects['metcalf']
        # check if metcalf scoring is enabled at the moment
        if metcalf_obj in current_methods:
            # create a dict mapping spectra in the results to their scores
            spec_scores = {}
            for gcf, result in results.links.items():
                for spec, link_data in result.items():
                    metcalf_score = link_data.data(metcalf_obj)
                    spec_scores[spec] = metcalf_score

            # get a list of the spectra currently visible in the table (note that
            # these are the actual Spectrum IDs, not the internal nplinker IDs)
            visible_spectra = map(int, self.table_session_data.spec_ds.data['spectrum_id'])

            # update the data source with the scores
            self.table_session_data.spec_ds.data['metcalf_score'] = [spec_scores[self.nh.nplinker.lookup_spectrum(spec_id)] for spec_id in visible_spectra]

        # next, need to display the results using the same code as for generating the 
        # results from plot selections, but without actually triggering any unintended
        # side-effects (changing the plot selections and so on)

        # the plots are configured to update automatically when changes are made
        # to their underlying datasource (e.g. new selections). want to avoid this
        # happening here because in tables mode the plots aren't visible. this is 
        # a quick hack that just prevents the selection callback from doing anything
        # for the duration of this operation
        # TODO this should be more decoupled anyway
        self.suppress_selection_bgc = True

        # the next step, before actually displaying the links, is to make sure
        # the webapp scoring mode is set to GCF->Spec. this is currently done by
        # simulating a click on the appropriate radio button, which will handle
        # the rest of the changes required
        self.ge_mode_callback(None, None, 0)

        # TODO another issue is that the various methods involved here tend to 
        # refer to member variables directly instead of having parameters passed 
        # in, which would make them easier to repurpose (i.e. they use self.ds_bgc
        # instead of a generic <datasource> parameter)
        # 
        # updated the set of selected indices on the spectral datasource (equivalent
        # to the user having made the same selection via the plot)
        self.ds_bgc.selected.indices = [bgc.id for bgc in selected_bgcs]

        # the ScoringHelper object expects to have this list of objects available so need to set that
        self.score_helper.bgcs = selected_bgcs
        self.score_helper.gcfs = selected_gcfs

        # get the list of visible spectra so we can filter out any others from the results
        # TODO MolFam mode...
        include_only = set([self.nh.nplinker.spectra[int(spec_id)] for spec_id in self.table_session_data.spec_ds.data['spec_pk'] if spec_id != '-'])

        # FINALLY, display the link information
        self.display_links(results, mode=SCO_MODE_BGC_SPEC, div=self.results_div, include_only=include_only)

        # and then remove the hacky workaround
        self.suppress_selection_bgc = False

    def tables_reset_callback(self):
        for dt in self.table_session_data.data_tables.values():
            dt.selectable = True

    def bokeh_layout(self):
        widgets = []
        self.results_div = Div(text=RESULTS_PLACEHOLDER, name='results_div', width_policy='max', height_policy='fit')
        widgets.append(self.results_div)

        # bgc plot selected by default so enable the selection change listener
        self.ds_bgc.selected.on_change('indices', self.bgc_selchanged)

        self.plot_toggles = CheckboxGroup(active=[PLOT_PRESERVE_COLOUR, PLOT_ONLY_SHARED_STRAINS], labels=PLOT_TOGGLES, name='plot_toggles')
        self.plot_toggles.on_change('active', self.plot_toggles_callback)
        widgets.append(self.plot_toggles)

        self.tsne_id_select = Select(title='BGC TSNE:', value=self.bgc_tsne_id, options=self.bgc_tsne_id_list, name='tsne_id_select')
        self.tsne_id_select.on_change('value', self.tsne_id_callback)
        widgets.append(self.tsne_id_select)

        self.mb_mode_group = RadioGroup(labels=METABOLOMICS_SCORING_MODES, active=0, name='mb_mode_group', css_classes=['modes-metabolomics'])
        self.mb_mode_group.on_change('active', self.mb_mode_callback)
        widgets.append(self.mb_mode_group)

        self.plot_select = Toggle(label=SCORING_GEN_TO_MET, name='plot_select', active=True, css_classes=['button-genomics'], sizing_mode='scale_width')
        self.plot_select.on_click(self.plot_select_callback)
        widgets.append(self.plot_select)

        self.ge_mode_group = RadioGroup(labels=GENOMICS_SCORING_MODES, active=0, name='ge_mode_group', sizing_mode='scale_width')
        self.ge_mode_group.on_change('active', self.ge_mode_callback)
        widgets.append(self.ge_mode_group)

        # scoring objects and related stuff
        # TODO this should be drawn from nplinker config, not hardcoded here? or config file
        self.scoring_methods = ['metcalf', 'testscore']
        self.scoring_objects = {m: self.nh.nplinker.scoring_method(m) for m in self.scoring_methods}

        # checkbox list of scoring methods to enable/disable them
        self.scoring_method_checkboxes = CheckboxGroup(labels=[m for m in self.scoring_methods], active=[0], name='scoring_method_checkboxes')
        self.scoring_method_checkboxes.on_change('active', self.scoring_method_callback)
        widgets.append(self.scoring_method_checkboxes)

        # buttons to show the modal dialogs for configuring each scoring method
        self.scoring_method_buttons = {}
        self.scoring_method_buttons['metcalf'] = Button(label='Configure Metcalf scoring', name='metcalf_scoring_button', button_type='primary')
        self.scoring_method_buttons['testscore'] = Button(label='Configure testscore scoring', name='testscore_scoring_button', button_type='primary', visible=False)
        widgets.extend(x for x in self.scoring_method_buttons.values())

        # basic JS callbacks to show the modals that allow you to configure the enabled scoring methods
        show_modal_callback = """ $('#' + modal_name).modal(); """
        self.scoring_method_buttons['metcalf'].js_on_click(CustomJS(args=dict(modal_name='metcalfModal'), code=show_modal_callback))
        self.scoring_method_buttons['testscore'].js_on_click(CustomJS(args=dict(modal_name='testscoreModal'), code=show_modal_callback))

        # metcalf config objects
        self.metcalf_standardised = RadioGroup(labels=['Basic Metcalf scoring', 'Standardised Metcalf scoring'], name='metcalf_standardised', active=1 if self.scoring_objects['metcalf'].standardised else 0, sizing_mode='scale_width')
        self.metcalf_standardised.on_change('active', self.metcalf_standardised_callback)
        widgets.append(self.metcalf_standardised)
        self.metcalf_cutoff = Slider(start=0, end=100, value=int(self.scoring_objects['metcalf'].cutoff), step=1, title='[metcalf] cutoff = ', name='metcalf_cutoff')
        self.metcalf_cutoff.on_change('value', self.metcalf_cutoff_callback)
        widgets.append(self.metcalf_cutoff)

        # same for testscore
        self.testscore_value = Slider(start=0, end=1, step=.01, value=self.scoring_objects['testscore'].value, title='Value parameter', name='testscore_value')
        self.testscore_value.on_change('value', self.testscore_value_callback)
        widgets.append(self.testscore_value)

        # for debug output etc 
        self.debug_div = Div(text="", name='debug_div', sizing_mode='scale_width')
        widgets.append(self.debug_div)

        # status updates/errors
        self.alert_div = Div(text="", name='alert_div')
        widgets.append(self.alert_div)

        # "reset everything" button
        self.reset_button = Button(name='reset_button', label='Reset state', button_type='danger', sizing_mode='scale_width')
        # this is used to clear the selection, which isn't done by faking the reset event from the
        # javascript callback below for some reason...
        self.reset_button.on_click(self.reset_button_callback)
        # no python method to reset plots, for some reason...
        self.reset_button.js_on_click(CustomJS(args=dict(fig_bgc=self.fig_bgc, fig_spec=self.fig_spec), 
                                               code=""" 
                                                    fig_bgc.reset.emit();
                                                    fig_spec.reset.emit();
                                                """))
        widgets.append(self.reset_button)

        self.search_input = TextInput(value='', title='Search for:', name='search_input', sizing_mode='scale_width')
        # TODO this can be used to trigger a search too, but annoyingly it is triggered when
        # a) enter is pressed (which is fine) 
        #   OR
        # b) the widget is unfocused (which is a bit stupid because if the user is unfocusing to
        # go click the search button it will end up doing the search twice)
        # self.search_input.on_change('value', lambda a, o, n : self.search_button_callback())
        widgets.append(self.search_input)

        # TODO can use this to convert selections in the table to selections on the plot
        self.foo = ColumnDataSource(data={'data': [1, 2, 3, 4, 5]})
        # self.foo.on_change('data', lambda a, o, n: print('FOO', a, o, n))
        self.search_button = Button(name='search_button', label='Search', button_type='primary', sizing_mode='scale_width')
        # conveniently on_click seems to get called after js_on_click, so we can do stuff on the 
        # JS side in the CustomJS callback and then pick up the changes in the on_click handler
        # on the Python side

        self.search_button.on_click(self.search_button_callback)
        widgets.append(self.search_button)

        self.search_score_button = Button(name='search_score_button', label='Run scoring on selected results', button_type='warning', sizing_mode='scale_width')
        self.search_score_button.on_click(self.search_score_button_callback)
        self.confirm_response = ColumnDataSource(data={'resp': [None], 'selelected': [[]]})
        self.search_score_button.js_on_click(CustomJS(args=dict(resp=self.confirm_response), code="""
                // purpose of this callback is to ask the user to confirm scoring
                // is desired if the number of selected objects is > 25 (arbitrary threshold),
                // and also to extract the indices of the selected objects to return to the
                // python code (this code is executed before the python callback)

                // get the array of selected indices
                var sel_indices = $('input[class=search-input]:checked').map(function() { return this.id; }).get();
                if(sel_indices.length > 25) {
                    // use a JS confirm dialog to get a response
                    var answer = confirm(sel_indices.length + " objects selected, do you really want to run scoring? (may be slow!)");
                    // switch to scoring tab if confirmed to proceed
                    if(answer == true) {
                        $('#scoringTab').tab('show');
                    }
                    // return the answer in the data source
                    resp.data = {'resp': [answer], 'selected': [sel_indices] };
                } else if(sel_indices.length > 0) {
                    // in this case can just proceed directly to show scoring tab and return selection
                    resp.data = {'resp': [true], 'selected': [sel_indices] };
                    $('#scoringTab').tab('show');
                }
                resp.change.emit();
                """))
        widgets.append(self.search_score_button)

        self.search_div_results = Div(text='', sizing_mode='scale_both', name='search_div_results')
        widgets.append(self.search_div_results)

        self.search_div_header = Div(text='', sizing_mode='scale_height', name='search_div_header')
        widgets.append(self.search_div_header)

        self.search_type = Select(options=SEARCH_OPTIONS, value=SEARCH_OPTIONS[0], name='search_type', css_classes=['nolabel'], sizing_mode='scale_width')
        self.search_type.on_change('value', self.search_type_callback)
        widgets.append(self.search_type)

        self.search_regex = CheckboxGroup(labels=['Use regexes?'], active=[0], name='search_regex', sizing_mode='scale_width')
        widgets.append(self.search_regex)

        self.dataset_description_pre = PreText(text=self.nh.nplinker.dataset_description, sizing_mode='scale_both', name='dataset_description_pre')
        widgets.append(self.dataset_description_pre)

        gnps_params = self.nh.nplinker.gnps_params 
        defval = 'uuid' if 'uuid' in gnps_params else None
        self.gnps_params_select = Select(options=list(gnps_params.keys()), value=defval, name='gnps_params_select', css_classes=['nolabel'], sizing_mode='scale_height')
        self.gnps_params_select.on_change('value', self.gnps_params_select_callback)
        widgets.append(self.gnps_params_select)

        if defval is not None:
            deftext = '<strong>{}</strong> = {}'.format(defval, self.nh.nplinker.gnps_params[defval])
        else:
            deftext = None
        self.gnps_params_value = Div(text=deftext, sizing_mode='scale_height', name='gnps_params_value')
        widgets.append(self.gnps_params_value)

        other_info = '<table class="table table-responsive table-striped" id="other_info_table"><thead><tr>'
        other_info += '<th scope="col">Name</th><th scope="col">Value</th></tr>'
        other_info += '</thead><tbody>'
        other_info += '<tr><td>{}</td><td>{}</td></tr>'.format('BiGSCAPE clustering cutoff', self.nh.nplinker.bigscape_cutoff)
        other_info += '</tbody></table>'
        self.other_info_div = Div(text=other_info, sizing_mode='scale_height', name='other_info_div')
        widgets.append(self.other_info_div)

        legend_bgc_text = '<table><thead><tr><td><strong>Product type</strong></td></tr></thead><tbody>'
        for pt in self.nh.nplinker.product_types:
            legend_bgc_text += '<tr style="background-color: {}"><td>{}</td></tr>'.format(self.nh.bgc_cmap[pt], pt)
        legend_bgc_text += '</tbody></table>'
        self.legend_bgc = Div(text=legend_bgc_text, sizing_mode='stretch_width', name='legend_bgc')
        widgets.append(self.legend_bgc)

        legend_spec_text = '<table><thead><tr><td><strong>Parent mass</strong></td></tr></thead><tbody>'
        for text, colour in self.nh.spec_cmap:
            legend_spec_text += '<tr style="background-color: {}"><td>{}</td></tr>'.format(colour, text)
        legend_spec_text += '</tbody></table>'
        self.legend_spec = Div(text=legend_spec_text, sizing_mode='stretch_width', name='legend_spec')
        widgets.append(self.legend_spec)

        widgets.append(self.fig_spec)
        widgets.append(self.fig_bgc)

        # tables widgets
        self.table_session_data.setup()
        widgets.extend(self.table_session_data.widgets)

        # callbacks for scoring buttons
        self.table_session_data.tables_score_met.on_click(self.tables_score_met_callback)
        self.table_session_data.tables_score_gen.on_click(self.tables_score_gen_callback)

        # and reset button
        self.table_session_data.tables_reset.on_click(self.tables_reset_callback)

        self.hidden_alert_thing = PreText(text='', name='hidden_alert', visible=False)
        self.hidden_alert_thing.js_on_change('text', CustomJS(code='if(cb_obj.text.length > 0) alert(cb_obj.text);', args={}))
        widgets.append(self.hidden_alert_thing)
        print('layout done, adding widget roots')

        for w in widgets:
            curdoc().add_root(w)

        curdoc().title = 'nplinker webapp'
        print('bokeh_layout method complete')
        # curdoc().theme = 'dark_minimal'

# server_lifecycle.py adds a .nh attr to the current Document instance, use that
# to access the already-created NPLinker instance plus the TSNE data
nb = NPLinkerBokeh(curdoc().nh, curdoc().table_session_data)
nb.create_plots()
nb.set_inactive_plot(nb.fig_spec)
nb.bokeh_layout()
nb.update_alert('Initialised OK!', 'success')
