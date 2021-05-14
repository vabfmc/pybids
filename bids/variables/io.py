""" Tools for reading/writing BIDS data files. """

from os.path import join
from pathlib import Path
import warnings
import json

import numpy as np
import pandas as pd

from bids.utils import listify, convert_JSON
from .entities import NodeIndex
from .variables import SparseRunVariable, DenseRunVariable, SimpleVariable
from .collections import BIDSRunVariableCollection

BASE_ENTITIES = ['subject', 'session', 'task', 'run']
ALL_ENTITIES = BASE_ENTITIES + ['datatype', 'suffix', 'acquisition']


def load_variables(layout, types=None, levels=None, skip_empty=True,
                   dataset=None, scope='all', **kwargs):
    """A convenience wrapper for one or more load_*_variables() calls.

    Parameters
    ----------
    layout : :obj:`bids.layout.BIDSLayout`
        BIDSLayout containing variable files.
    types : str or list
        Types of variables to retrieve. All valid values
        reflect the filename stipulated in the BIDS spec for each kind of
        variable. Valid values include: 'events', 'physio', 'stim',
        'scans', 'participants', 'sessions', and 'regressors'.
    levels : str or list
        Optional level(s) of variables to load. Valid
        values are 'run', 'session', 'subject', or 'dataset'. This is
        simply a shorthand way to specify types--e.g., 'run' will be
        converted to types=['events', 'physio', 'stim', 'regressors'].
    skip_empty : bool
        Whether or not to skip empty Variables (i.e.,
        where there are no rows/records in a file after applying any
        filtering operations like dropping NaNs).
    dataset : NodeIndex
        An existing NodeIndex container to store the
        loaded data in. Can be used to iteratively construct a dataset
        that contains otherwise heterogeneous sets of variables. If None,
        a new NodeIndex is used.
    scope : str or list
        The scope of the space to search for variables. See
        docstring for BIDSLayout for details and valid predefined values.
    kwargs : dict
        Optional keyword arguments to pass onto the individual
        load_*_variables() calls.

    Returns
    -------
    A NodeIndex instance.

    Examples
    --------
    >>> load_variables(layout, ['events', 'physio'], subject='01')  # doctest: +SKIP
    # returns all variables stored in _events.tsv and _physio.tsv.gz files
    # for runs that belong to subject with id '01'.
    """

    TYPES = ['events', 'physio', 'stim', 'scans', 'participants', 'sessions',
             'regressors']

    types = listify(types)

    if types is None:
        if levels is not None:
            types = []
            lev_map = {
                'run': ['events', 'physio', 'stim', 'regressors'],
                'session': ['scans'],
                'subject': ['sessions'],
                'dataset': ['participants']
            }
            [types.extend(lev_map[l.lower()]) for l in listify(levels)]
        else:
            types = TYPES

    bad_types = set(types) - set(TYPES)
    if bad_types:
        raise ValueError("Invalid variable types: %s" % bad_types)

    dataset = dataset or NodeIndex()

    run_types = list({'events', 'physio', 'stim', 'regressors'} - set(types))
    type_flags = {t: False for t in run_types}
    if len(type_flags) < 4:
        _kwargs = kwargs.copy()
        _kwargs.update(type_flags)
        dataset = _load_time_variables(layout, dataset, scope=scope, **_kwargs)

    for t in ({'scans', 'sessions', 'participants'} & set(types)):
        kwargs.pop('suffix', None) # suffix is always one of values aboves
        dataset = _load_tsv_variables(layout, t, dataset, scope=scope,
                                      **kwargs)

    return dataset


def _get_nvols(img_f):
    import nibabel as nb
    img = nb.load(img_f)
    nvols = 0
    if isinstance(img, nb.Nifti1Pair):
        nvols = img.shape[3]
    elif isinstance(img, nb.Cifti2Image):
        for ax in map(img.header.get_axis, range(len(img.header.matrix))):
            if isinstance(ax, nb.cifti2.SeriesAxis):
                nvols = ax.size
                break
        else:
            raise ValueError("No series axis found in %s" % img_f)
    elif isinstance(img, nb.GiftiImage):
        nvols = len(img.get_arrays_from_intent('time series'))
    else:
        raise ValueError("Unknown image type %s: %s" % img.__class__, img_f)

    return nvols

def get_events_collection(_data, run_info, drop_na=True, columns=None, entities=None):
    """
    This is an attempt to minimally implement:
    https://github.com/bids-standard/pybids/blob/statsmodels/bids/variables/io.py

    in a way that will still work for bids io, but will also work without layout.
    """

    run_info
    if entities is None:
        entities = run_info.entities
    if 'amplitude' in _data.columns:
        if (_data['amplitude'].astype(int) == 1).all() and \
                'trial_type' in _data.columns:
            msg = ("Column 'amplitude' with constant value 1 "
                   "is unnecessary in event files; ignoring it.")
            _data = _data.drop('amplitude', axis=1)
        else:
            msg = ("Column name 'amplitude' is reserved; "
                   "renaming it to 'amplitude_'.")
            _data = _data.rename(
                columns={'amplitude': 'amplitude_'})
        warnings.warn(msg)

    _data = _data.replace('n/a', np.nan)  # Replace BIDS' n/a
    _data = _data.apply(pd.to_numeric, errors='ignore')

    _cols = columns or list(set(_data.columns.tolist()) -
                            {'onset', 'duration'})
    colls_output = []
    # Construct a DataFrame for each extra column
    for col in _cols:
        df = _data[['onset', 'duration']].copy()
        df['amplitude'] = _data[col].values

        # Add in all of the run's entities as new columns for
        # index
        for entity, value in entities.items():
            if entity in ALL_ENTITIES:
                df[entity] = value

        if drop_na:
            df = df.dropna(subset=['amplitude'])

        if df.empty:
            continue

        var = SparseRunVariable(
            name=col, data=df, run_info=run_info, source='events')
        colls_output.append(var)
    return colls_output


def get_regressors_collection(_data, run_info, columns=None, entities=None):
    
    colls_output = []
    if entities is None:
        entities = run_info.entities

    if columns is not None:
        conf_cols = list(set(_data.columns) & set(columns))
        _data = _data.loc[:, conf_cols]
    for col in _data.columns:
        sr = 1. / run_info.tr
        var = DenseRunVariable(name=col, values=_data[[col]],
                       run_info=run_info, source='regressors',
                       sampling_rate=sr)
        colls_output.append(var)
    return colls_output


def get_rec_collection(data,run_info,metadata,source,columns=None,entities=None):

    colls_output = []
    freq = metadata['SamplingFrequency']
    st = metadata['StartTime']
    rf_cols = metadata['Columns']
    data.columns = rf_cols

    # Filter columns if user passed names
    if columns is not None:
        rf_cols = list(set(rf_cols) & set(columns))
        data = data.loc[:, rf_cols]

    n_cols = len(rf_cols)
    if not n_cols:
        # nothing to do
        return []

    # Keep only in-scan samples
    if st < 0:
        start_ind = np.floor(-st * freq)
        values = data.values[start_ind:, :]
    else:
        values = data.values

    if st > 0:
        n_pad = int(freq * st)
        pad = np.zeros((n_pad, n_cols))
        values = np.r_[pad, values]

    n_rows = int(run_info.duration * freq)
    if len(values) > n_rows:
        values = values[:n_rows, :]
    elif len(values) < n_rows:
        pad = np.zeros((n_rows - len(values), n_cols))
        values = np.r_[values, pad]

    df = pd.DataFrame(values, columns=rf_cols)
    for col in df.columns:
        var = DenseRunVariable(name=col, values=df[[col]], run_info=run_info,
                               source=source, sampling_rate=freq)
        colls_output.append(var)
    return colls_output


def _load_time_variables(layout, dataset=None, columns=None, scan_length=None,
                         drop_na=True, events=True, physio=True, stim=True,
                         regressors=True, skip_empty=True, scope='all',
                         **selectors):
    """Loads all variables found in *_events.tsv files and returns them as a
    BIDSVariableCollection.

    Parameters
    ----------
    layout : :obj:`bids.layout.BIDSLayout`
        A BIDSLayout to scan.
    dataset : NodeIndex
        A BIDS NodeIndex container. If None, a new one is
        initialized.
    columns : list
        Optional list of names specifying which columns in the
        event files to read. By default, reads all columns found.
    scan_length : float
        Optional duration of runs (in seconds). By
        default, this will be extracted from the BOLD image. However, in
        cases where the user doesn't have access to the images (e.g.,
        because only file handles are locally available), a fixed duration
        can be manually specified as a fallback.
    drop_na : bool
        If True, removes all events where amplitude is n/a. If
        False, leaves n/a values intact. Note that in the latter case,
        transformations that requires numeric values may fail.
    events : bool
        If True, extracts variables from events.tsv files.
    physio : bool
        If True, extracts variables from _physio files.
    stim : bool
        If True, extracts variables from _stim files.
    skip_empty : bool
        Whether or not to skip empty Variables (i.e.,
        where there are no rows/records in a file, or all onsets,
        durations, and amplitudes are 0).
    scope : str or list
        The scope of the space to search for variables. See
        docstring for BIDSLayout for details and valid predefined values.
    selectors : dict
        Optional keyword arguments passed on to the
        BIDSLayout instance's get() method; can be used to constrain
        which data are loaded.

    Returns
    -------
    A NodeIndex instance.
    """

    # Extract any non-keyword arguments
    selectors = selectors.copy()

    if dataset is None:
        dataset = NodeIndex()

    selectors['datatype'] = 'func'
    selectors['suffix'] = 'bold'
    exts = selectors.pop('extension', ['.nii', '.nii.gz', '.func.gii', '.dtseries.nii'])
    images = layout.get(return_type='object', scope=scope, extension=exts, **selectors)

    if not images:
        raise ValueError("No functional images that match criteria found.")

    # Main loop over images
    for img_obj in images:

        entities = img_obj.entities
        img_f = img_obj.path

        # Run is not mandatory, but we need a default for proper indexing
        if 'run' in entities:
            entities['run'] = int(entities['run'])

        tr = img_obj.get_metadata()["RepetitionTime"]

        # Get duration of run: first try to get it directly from the image
        # header; if that fails, look for a scan_length argument.
        try:
            nvols = _get_nvols(img_f)
            duration = nvols * tr
        except Exception as e:
            if scan_length is not None:
                duration = scan_length
                nvols = int(np.rint(scan_length / tr))
            else:
                msg = ("Unable to extract scan duration from one or more "
                       "BOLD runs, and no scan_length argument was provided "
                       "as a fallback. Please check that the image files are "
                       "available, or manually specify the scan duration.")
                raise ValueError(msg) from e

        # We don't want to pass all the image file's entities onto get_node(),
        # as there can be unhashable nested slice timing values, and this also
        # slows down querying unnecessarily. Instead, pick out files only based
        # on the core BIDS entities and any entities explicitly passed as
        # selectors.
        # TODO: one downside of this approach is the stripped entities also
        # won't be returned in the resulting node due to the way things are
        # implemented. Consider adding a flag to control this.
        select_on = {k: v for (k, v) in entities.items()
                     if k in BASE_ENTITIES or k in selectors}

        # If a matching node already exists, return it
        result = dataset.get_nodes('run', select_on)

        if result:
            if len(result) > 1:
                raise ValueError("More than one existing Node matches the "
                                 "specified entities! You may need to pass "
                                 "additional selectors to narrow the search.")
            run_info = result[0].get_info()

        else:
            # Otherwise create a new node and use that.
            # We first convert any entity values that are currently collections to
            # JSON strings to prevent nasty hashing problems downstream. Note that
            # isinstance() isn't as foolproof as actually trying to hash the
            # value, but the latter is likely to be slower, and since values are
            # coming from JSON or filenames, there's no real chance of encountering
            # anything but a list or dict.
            entities = {
                k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                for (k, v) in entities.items()
            }

            run = dataset.create_node('run', entities, image_file=img_f,
                                      duration=duration, repetition_time=tr,
                                      n_vols=nvols)
            run_info = run.get_info()

        # Process event files
        if events:
            efiles = layout.get_nearest(
                img_f, extension='.tsv', suffix='events', all_=True,
                full_search=True, ignore_strict_entities=['suffix', 'extension'])
            for ef in efiles:
                _data = pd.read_csv(ef, sep='\t')
                event_cols = get_events_collection(_data, run.get_info(), drop_na=drop_na, columns=columns)
                for ec in event_cols:
                    run.add_variable(ec)

        # Process confound files
        if regressors:
            sub_ents = {k: v for k, v in entities.items()
                        if k in BASE_ENTITIES}
            confound_files = layout.get(suffix='regressors', scope=scope,
                                        **sub_ents)
            for cf in confound_files:
                _data = pd.read_csv(cf.path, sep='\t', na_values='n/a')
                reg_colls = get_regressors_collection(_data, run.get_info(), columns=columns)
                for rc in reg_colls:
                    run.add_variable(rc)

        # Process recordinging files
        rec_types = []
        if physio:
            rec_types.append('physio')
        if stim:
            rec_types.append('stim')

        if rec_types:
            rec_files = layout.get_nearest(
                img_f, extension='.tsv.gz', all_=True, suffix=rec_types,
                ignore_strict_entities=['suffix', 'extension'], full_search=True)
            for rf in rec_files:
                metadata = layout.get_metadata(rf)
                if not metadata:
                    raise ValueError("No .json sidecar found for '%s'." % rf)
                # rec_file passed in for now because rec_type needs to be inferred
                source = 'physio' if '_physio.tsv' in rf else 'stim'
                data = pd.read_csv(rf, sep='\t')
                rec_colls = get_rec_collection(
                                         data,
                                         run.get_info(),
                                         metadata,
                                         source,
                                         columns=columns)
                for rc in rec_colls:
                    run.add_variable(rc)

    return dataset


def _load_tsv_variables(layout, suffix, dataset=None, columns=None,
                        prepend_type=False, scope='all', **selectors):
    """Reads variables from scans.tsv, sessions.tsv, and participants.tsv.

    Parameters
    ----------
    layout : :obj:`bids.layout.BIDSLayout`
        The BIDSLayout to use.
    suffix : str
        The suffix of file to read from. Must be one of 'scans',
        'sessions', or 'participants'.
    dataset : NodeIndex
        A BIDS NodeIndex container. If None, a new one is
        initialized.
    columns : list
        Optional list of names specifying which columns in the
        files to return. If None, all columns are returned.
    prepend_type : bool
        If True, variable names are prepended with the
        type name (e.g., 'age' becomes 'participants.age').
    scope : str or list
        The scope of the space to search for variables. See
        docstring for BIDSLayout for details and valid predefined values.
    selectors : dict
        Optional keyword arguments passed onto the
        BIDSLayout instance's get() method; can be used to constrain
        which data are loaded.

    Returns
    -------
    A NodeIndex instance.
    """

    # Sanitize the selectors: only keep entities at current level or above
    remap = {'scans': 'run', 'sessions': 'session', 'participants': 'subject'}
    level = remap[suffix]
    valid_entities = BASE_ENTITIES[:BASE_ENTITIES.index(level)]
    layout_kwargs = {k: v for k, v in selectors.items() if k in valid_entities}

    if dataset is None:
        dataset = NodeIndex()

    files = layout.get(extension='.tsv', suffix=suffix, scope=scope,
                       **layout_kwargs)

    for f in files:

        _data = f.get_df(include_timing=False)

        # Entities can be defined either within the first column of the .tsv
        # file (for entities that vary by row), or from the full file path
        # (for entities constant over all rows in the file). We extract both
        # and store them in the main DataFrame alongside other variables (as
        # they'll be extracted when the BIDSVariable is initialized anyway).
        for ent_name, ent_val in f.entities.items():
            if ent_name in ALL_ENTITIES:
                _data[ent_name] = ent_val

        # Handling is a bit more convoluted for scans.tsv, because the first
        # column contains the run filename, which we also need to parse.
        if suffix == 'scans':

            # Suffix is guaranteed to be present in each filename, so drop the
            # constant column with value 'scans' to make way for it and prevent
            # two 'suffix' columns.
            _data.drop(columns=['suffix'], inplace=True)

            image = _data['filename']
            _data = _data.drop('filename', axis=1)
            dn = f.dirname
            paths = [join(dn, p) for p in image.values]
            ent_recs = [dict(layout.files[p].entities) for p in paths
                        if p in layout.files]
            ent_cols = pd.DataFrame.from_records(ent_recs)

            # Remove entity columns found in both DFs
            dupes = list(set(ent_cols.columns) & set(_data.columns))
            to_drop = ['extension'] + dupes
            ent_cols.drop(columns=to_drop, inplace=True)

            _data = pd.concat([_data, ent_cols], axis=1, sort=True)

        # The BIDS spec requires ID columns to be named 'session_id', 'run_id',
        # etc., and IDs begin with entity prefixes (e.g., 'sub-01'). To ensure
        # consistent internal handling, we strip these suffixes and prefixes.
        elif suffix == 'sessions':
            _data = _data.rename(columns={'session_id': 'session'})
            _data['session'] = _data['session'].str.replace('ses-', '')
        elif suffix == 'participants':
            _data = _data.rename(columns={'participant_id': 'subject'})
            _data['subject'] = _data['subject'].str.replace('sub-', '')

        def make_patt(x, regex_search=False):
            patt = '%s' % x
            if isinstance(x, (int, float)):
                # allow for leading zeros if a number was specified
                # regardless of regex_search
                patt = '0*' + patt
            if not regex_search:
                patt = '^%s$' % patt
            return patt

        # Filter rows on all selectors
        comm_cols = list(set(_data.columns) & set(selectors.keys()))
        for col in comm_cols:
            ent_patts = [make_patt(x, regex_search=layout.regex_search)
                            for x in listify(selectors.get(col))]
            patt = '|'.join(ent_patts)

            _data = _data[_data[col].str.contains(patt)]

        level = {'scans': 'session', 'sessions': 'subject',
                 'participants': 'dataset'}[suffix]

        node = dataset.get_or_create_node(level, f.entities)

        ent_cols = list(set(ALL_ENTITIES) & set(_data.columns))
        amp_cols = list(set(_data.columns) - set(ent_cols))

        if columns is not None:
            amp_cols = list(set(amp_cols) & set(columns))

        for col_name in amp_cols:

            # Rename colummns: values must be in 'amplitude'
            df = _data.loc[:, [col_name] + ent_cols]
            df.columns = ['amplitude'] + ent_cols

            if prepend_type:
                col_name = '%s.%s' % (suffix, col_name)

            node.add_variable(SimpleVariable(name=col_name, data=df, source=suffix))

    return dataset


def parse_transforms(transforms_in, validate=True,level="run"):
    """ Adapted from bids.modeling.statsmodels.BIDSStatsModelsGraph. Also
    handles files/jsons that only define the transformations section of the
    model.json """

    # input is JSON as a file or dict
    if isinstance(transforms_in, str):
        if not Path(transforms_in).exists():
            raise ValueError(f"Cannot find path: {transforms_in}")
        with open(transforms_in, 'r', encoding='utf-8') as fobj:
            transforms_raw = json.load(fobj)
    else:
        transforms_raw = transforms_in

    # Convert JSON from CamelCase to snake_case keys
    transforms_raw = convert_JSON(transforms_raw)

    if validate:
       # TODO
       # validate_transforms(transforms_raw)
       pass

    # Process transformations
    # TODO: some basic error checking to confirm the correct level of
    # transformations has been obtained. This will most likely be the case since
    # transformations at higher levels will no longer be required when the new
    # "flow" approach is used.
    if "transformations" in transforms_raw:
        transforms = transforms_raw["transformations"]
    elif any(k in transforms_raw for k in ["nodes","steps"]):
        nodes_key = "nodes" if "nodes" in transforms_raw else "steps"
        transforms = transforms_raw[nodes_key][0]["transformations"]
    else:
        raise ValueError("Cannot find a key for nodes in the json input representing the model")
    return transforms


