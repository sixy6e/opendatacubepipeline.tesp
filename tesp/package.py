#!/usr/bin/env python

import os
from os.path import join as pjoin, basename, dirname, splitext, exists
from posixpath import join as ppjoin
from pathlib import Path
from subprocess import check_call
import tempfile
import glob
import re
from pkg_resources import resource_stream
import numpy
import h5py
from rasterio.enums import Resampling
import rasterio

import yaml
from yaml.representer import Representer

from wagl.acquisition import acquisitions
from wagl.constants import DatasetName, GroupName
from wagl.data import write_img
from wagl.hdf5 import find
from wagl.geobox import GriddedGeoBox

import tesp
from tesp.checksum import checksum
from tesp.contrast import quicklook
from tesp.html_geojson import html_map
from tesp.yaml_merge import merge_metadata
from tesp.constants import ProductPackage
from tesp.prepare import extract_level1_metadata

from eugl.fmask import fmask_cogtif
from eugl.contiguity import contiguity
from eugl.metadata import get_fmask_metadata

yaml.add_representer(numpy.int8, Representer.represent_int)
yaml.add_representer(numpy.uint8, Representer.represent_int)
yaml.add_representer(numpy.int16, Representer.represent_int)
yaml.add_representer(numpy.uint16, Representer.represent_int)
yaml.add_representer(numpy.int32, Representer.represent_int)
yaml.add_representer(numpy.uint32, Representer.represent_int)
yaml.add_representer(numpy.int, Representer.represent_int)
yaml.add_representer(numpy.int64, Representer.represent_int)
yaml.add_representer(numpy.uint64, Representer.represent_int)
yaml.add_representer(numpy.float, Representer.represent_float)
yaml.add_representer(numpy.float32, Representer.represent_float)
yaml.add_representer(numpy.float64, Representer.represent_float)
yaml.add_representer(numpy.ndarray, Representer.represent_list)

ALIAS_FMT = {'LAMBERTIAN': 'lambertian_{}', 'NBAR': 'nbar_{}', 'NBART': 'nbart_{}', 'SBT': 'sbt_{}'}
LEVELS = [2, 4, 8, 16, 32]
PATTERN1 = re.compile(
    r'(?P<prefix>(?:.*_)?)(?P<band_name>B[0-9][A0-9]|B[0-9]*|B[0-9a-zA-z]*)'
    r'(?P<extension>\.TIF)')
PATTERN2 = re.compile('(L1[GTPCS]{1,2})')
ARD = 'ARD'
QA = 'QA'
SUPPS = 'SUPPLEMENTARY'


def run_command(command, work_dir):
    """
    A simple utility to execute a subprocess command.
    """
    check_call(' '.join(command), shell=True, cwd=work_dir)


def _clean(alias):
    """
    A quick fix for cleaning json unfriendly alias strings.
    """
    replace = {'-': '_',
               '[': '',
               ']': ''}
    for k, v in replace.items():
        alias = alias.replace(k, v)

    return alias.lower()


def _write_tif(dataset, out_fname, cogtif=True, platform=None):
    """
    Easy wrapper for writing a tif or cogtif, that takes care of datasets
    that are written row by row rather square(ish) blocks.
    All the overview level's block size is set to 512 x 512 for USGS dataset
    """
    if dataset.chunks[1] == dataset.shape[1]:
        data = dataset[:]
    else:
        data = dataset

    # setting the overview block size depending on the specific sensor.
    # Current, only USGS dataset are tiled at 512 x 512 for standardizing
    # Level 2 ARD products. Sentinel-2 tile size are inherited from the
    # L1C products and its overview's blocksize are default value of GDAL's
    # overview block size of 128 x 128

    # TODO Standardizing the Sentinel-2's overview tile size with external inputs

    if platform == "LANDSAT":
        blockxsize = 512
        blockysize = 512
        config_options = {'GDAL_TIFF_OVR_BLOCKSIZE': blockxsize}
    else:
        blockysize, blockxsize = dataset.chunks
        config_options = None

    options = {'blockxsize': blockxsize,
               'blockysize': blockysize,
               'compress': 'deflate',
               'zlevel': 4}

    nodata = dataset.attrs.get('no_data_value')
    geobox = GriddedGeoBox.from_dataset(dataset)

    # path existence
    if not exists(dirname(out_fname)):
        os.makedirs(dirname(out_fname))

    write_img(data, out_fname, cogtif=cogtif, levels=LEVELS, nodata=nodata,
              geobox=geobox, resampling=Resampling.average, options=options, config_options=config_options)


def get_img_dataset_info(dataset, path, layer=1):
    """
    Returns metadata for raster datasets
    """
    geobox = GriddedGeoBox.from_dataset(dataset)
    return {
        'path': path,
        'layer': layer,
        'info': {
            'width': geobox.x_size(),
            'height': geobox.y_size(),
            'geotransform': list(geobox.transform.to_gdal())
        }
    }


def get_platform(container, granule):
    """
    retuns the satellite platform
    """
    acq = container.get_acquisitions(None, granule, False)[0]
    if 'SENTINEL' in acq.platform_id:
        platform = "SENTINEL"
    elif 'LANDSAT' in acq.platform_id:
        platform = "LANDSAT"
    else:
        msg = "Sensor not supported"
        raise Exception(msg)

    return platform


def unpack_products(product_list, container, granule, h5group, outdir, platform):
    """
    Unpack and package the NBAR and NBART products.
    """
    # listing of all datasets of IMAGE CLASS type
    img_paths = find(h5group, 'IMAGE')

    # relative paths of each dataset for ODC metadata doc
    rel_paths = {}

    # TODO pass products through from the scheduler rather than hard code
    for product in product_list:
        for pathname in [p for p in img_paths if '/{}/'.format(product) in p]:

            dataset = h5group[pathname]

            acqs = container.get_acquisitions(group=pathname.split('/')[0],
                                              granule=granule)
            acq = [a for a in acqs if
                   a.band_name == dataset.attrs['band_name']][0]

            base_fname = '{}.TIF'.format(splitext(basename(acq.uri))[0])
            match_dict = PATTERN1.match(base_fname).groupdict()
            fname = '{}{}_{}{}'.format(match_dict.get('prefix'), product,
                                       match_dict.get('band_name'),
                                       match_dict.get('extension'))
            rel_path = pjoin(product, re.sub(PATTERN2, ARD, fname))
            out_fname = pjoin(outdir, rel_path)

            _write_tif(dataset, out_fname, cogtif=True, platform=platform)

            # alias name for ODC metadata doc
            alias = _clean(ALIAS_FMT[product].format(dataset.attrs['alias']))

            # Band Metadata
            rel_paths[alias] = get_img_dataset_info(dataset, rel_path)

    # retrieve metadata
    scalar_paths = find(h5group, 'SCALAR')
    pathnames = [pth for pth in scalar_paths if 'NBAR-METADATA' in pth]

    def tags():
        result = yaml.load(h5group[pathnames[0]][()])
        for path in pathnames[1:]:
            other = yaml.load(h5group[path][()])
            result['ancillary'].update(other['ancillary'])
        return result

    return tags(), rel_paths


def unpack_supplementary(container, granule, h5group, outdir, platform):
    """
    Unpack the angles + other supplementary datasets produced by wagl.
    Currently only the mode resolution group gets extracted.
    """
    def _write(dataset_names, h5_group, granule_id, basedir, cogtif=False, platform_name=None):
        """
        An internal util for serialising the supplementary
        H5Datasets to tif.
        """
        fmt = '{}_{}.TIF'
        paths = {}
        for dname in dataset_names:
            rel_path = pjoin(basedir,
                             fmt.format(granule_id, dname.replace('-', '_')))
            out_fname = pjoin(outdir, rel_path)
            dset = h5_group[dname]
            alias = _clean(dset.attrs['alias'])
            paths[alias] = get_img_dataset_info(dset, rel_path)
            _write_tif(dset, out_fname, cogtif=cogtif, platform=platform_name)

        return paths

    _, res_grp = container.get_mode_resolution(granule)
    grn_id = re.sub(PATTERN2, ARD, granule)

    # relative paths of each dataset for ODC metadata doc
    rel_paths = {}

    # satellite and solar angles
    grp = h5group[ppjoin(res_grp, GroupName.SAT_SOL_GROUP.value)]
    dnames = [DatasetName.SATELLITE_VIEW.value,
              DatasetName.SATELLITE_AZIMUTH.value,
              DatasetName.SOLAR_ZENITH.value,
              DatasetName.SOLAR_AZIMUTH.value,
              DatasetName.RELATIVE_AZIMUTH.value,
              DatasetName.TIME.value]
    paths = _write(dnames, grp, grn_id, SUPPS, cogtif=False, platform_name=platform)
    for key in paths:
        rel_paths[key] = paths[key]

    # timedelta data
    timedelta_data = grp[DatasetName.TIME.value]

    # incident angles
    grp = h5group[ppjoin(res_grp, GroupName.INCIDENT_GROUP.value)]
    dnames = [DatasetName.INCIDENT.value,
              DatasetName.AZIMUTHAL_INCIDENT.value]
    paths = _write(dnames, grp, grn_id, SUPPS, cogtif=False, platform_name=platform)
    for key in paths:
        rel_paths[key] = paths[key]

    # exiting angles
    grp = h5group[ppjoin(res_grp, GroupName.EXITING_GROUP.value)]
    dnames = [DatasetName.EXITING.value,
              DatasetName.AZIMUTHAL_EXITING.value]
    paths = _write(dnames, grp, grn_id, SUPPS, cogtif=False, platform_name=platform)
    for key in paths:
        rel_paths[key] = paths[key]

    # relative slope
    grp = h5group[ppjoin(res_grp, GroupName.REL_SLP_GROUP.value)]
    dnames = [DatasetName.RELATIVE_SLOPE.value]
    paths = _write(dnames, grp, grn_id, SUPPS, cogtif=False, platform_name=platform)
    for key in paths:
        rel_paths[key] = paths[key]

    # terrain shadow
    grp = h5group[ppjoin(res_grp, GroupName.SHADOW_GROUP.value)]
    dnames = [DatasetName.COMBINED_SHADOW.value]
    paths = _write(dnames, grp, grn_id, QA, cogtif=True, platform_name=platform)
    for key in paths:
        rel_paths[key] = paths[key]

    # TODO do we also include slope and aspect?

    return rel_paths, timedelta_data


def create_contiguity(product_list, container, granule, outdir, platform):
    """
    Create the contiguity (all pixels valid) dataset.
    """
    # quick decision to use the mode resolution to form contiguity
    # this rule is expected to change once more people get involved
    # in the decision making process
    acqs, _ = container.get_mode_resolution(granule)
    grn_id = re.sub(PATTERN2, ARD, granule)

    nbar_contiguity = None
    # relative paths of each dataset for ODC metadata doc
    rel_paths = {}

    with tempfile.TemporaryDirectory(dir=outdir,
                                     prefix='contiguity-') as tmpdir:
        for product in product_list:
            search_path = pjoin(outdir, product)
            fnames = [str(f) for f in Path(search_path).glob('*.TIF')]

            # quick work around for products that aren't being packaged
            if not fnames:
                continue

            # 2018-09-11 Please forgive me; quick hack to remove troublesome quicklook
            # Issue arises on re-running from previous checkpoint
            for _idx, name in enumerate(fnames):
                if 'QUICKLOOK' in name:
                    fnames.pop(_idx)
                    break

            # output filename
            base_fname = '{}_{}_CONTIGUITY.TIF'.format(grn_id, product)
            rel_path = pjoin(QA, base_fname)
            out_fname = pjoin(outdir, rel_path)

            if not exists(dirname(out_fname)):
                os.makedirs(dirname(out_fname))

            alias = ALIAS_FMT[product].format('contiguity')

            # temp vrt
            tmp_fname = pjoin(tmpdir, '{}.vrt'.format(product))
            cmd = ['gdalbuildvrt',
                   '-resolution',
                   'user',
                   '-tr',
                   str(acqs[0].resolution[1]),
                   str(acqs[0].resolution[0]),
                   '-separate',
                   tmp_fname]
            cmd.extend(fnames)
            run_command(cmd, tmpdir)

            # contiguity mask for nbar product
            contiguity_mask = contiguity(tmp_fname, out_fname, platform)

            if base_fname.endswith('NBAR_CONTIGUITY.TIF'):
                nbar_contiguity = contiguity_mask

            with rasterio.open(out_fname) as ds:
                rel_paths[alias] = get_img_dataset_info(ds, rel_path)

    return rel_paths, nbar_contiguity


def create_html_map(outdir):
    """
    Create the html map and GeoJSON valid data extents files.
    """
    expr = pjoin(outdir, QA, '*_FMASK.TIF')
    contiguity_fname = glob.glob(expr)[0]
    html_fname = pjoin(outdir, 'map.html')
    json_fname = pjoin(outdir, 'bounds.geojson')

    # html valid data extents
    html_map(contiguity_fname, html_fname, json_fname)


def create_quicklook(product_list, container, outdir):
    """
    Create the quicklook and thumbnail images.
    """
    acq = container.get_acquisitions(None, None, False)[0]

    # are quicklooks still needed?
    # this wildcard mechanism needs to change if quicklooks are to
    # persist
    band_wcards = {'LANDSAT_5': ['L*_B{}.TIF'.format(i) for i in [3, 2, 1]],
                   'LANDSAT_7': ['L*_B{}.TIF'.format(i) for i in [3, 2, 1]],
                   'LANDSAT_8': ['L*_B{}.TIF'.format(i) for i in [4, 3, 2]],
                   'SENTINEL_2A': ['*_B0{}.TIF'.format(i) for i in [4, 3, 2]],
                   'SENTINEL_2B': ['*_B0{}.TIF'.format(i) for i in [4, 3, 2]]}

    # appropriate wildcards
    wcards = band_wcards[acq.platform_id]

    with tempfile.TemporaryDirectory(dir=outdir,
                                     prefix='quicklook-') as tmpdir:

        for product in product_list:
            if product == 'SBT':
                # no sbt quicklook for the time being
                continue

            out_path = Path(pjoin(outdir, product))
            fnames = []
            for wcard in wcards:
                fnames.extend([str(f) for f in out_path.glob(wcard)])

            # quick work around for products that aren't being packaged
            if not fnames:
                continue

            # output filenames
            match = PATTERN1.match(fnames[0]).groupdict()
            out_fname1 = '{}{}{}'.format(match.get('prefix'),
                                         'QUICKLOOK',
                                         match.get('extension'))
            out_fname2 = '{}{}{}'.format(match.get('prefix'),
                                         'THUMBNAIL',
                                         '.JPG')

            # initial vrt of required rgb bands
            tmp_fname1 = pjoin(tmpdir, '{}.vrt'.format(product))
            cmd = ['gdalbuildvrt',
                   '-separate',
                   '-overwrite',
                   tmp_fname1]
            cmd.extend(fnames)
            run_command(cmd, tmpdir)

            # quicklook with contrast scaling
            tmp_fname2 = pjoin(tmpdir, '{}_{}.tif'.format(product, 'qlook'))
            quicklook(tmp_fname1, out_fname=tmp_fname2, src_min=1,
                      src_max=3500, out_min=1)

            # warp to Lon/Lat WGS84
            tmp_fname3 = pjoin(tmpdir, '{}_{}.tif'.format(product, 'warp'))
            cmd = ['gdalwarp',
                   '-t_srs',
                   '"EPSG:4326"',
                   '-co',
                   'COMPRESS=JPEG',
                   '-co',
                   'PHOTOMETRIC=YCBCR',
                   '-co',
                   'TILED=YES',
                   tmp_fname2,
                   tmp_fname3]
            run_command(cmd, tmpdir)

            # build overviews/pyramids
            cmd = ['gdaladdo',
                   '-r',
                   'average',
                   tmp_fname3,
                   '2',
                   '4',
                   '8',
                   '16',
                   '32']
            run_command(cmd, tmpdir)

            # create the cogtif
            cmd = ['gdal_translate',
                   '-co',
                   'TILED=YES',
                   '-co',
                   'COPY_SRC_OVERVIEWS=YES',
                   '-co',
                   'COMPRESS=JPEG',
                   '-co',
                   'PHOTOMETRIC=YCBCR',
                   tmp_fname3,
                   out_fname1]
            run_command(cmd, tmpdir)

            # create the thumbnail
            cmd = ['gdal_translate',
                   '-of',
                   'JPEG',
                   '-outsize',
                   '10%',
                   '10%',
                   out_fname1,
                   out_fname2]
            run_command(cmd, tmpdir)


def create_readme(outdir):
    """
    Create the readme file.
    """
    with resource_stream(tesp.__name__, '_README.md') as src:
        with open(pjoin(outdir, 'README.md'), 'w') as out_src:
            out_src.writelines([l.decode('utf-8') for l in src.readlines()])


def create_checksum(outdir):
    """
    Create the checksum file.
    """
    out_fname = pjoin(outdir, 'CHECKSUM.sha1')
    checksum(out_fname)


def get_level1_tags(container, granule=None, yamls_path=None):
    _acq = container.get_all_acquisitions()[0]
    if yamls_path:
        # TODO define a consistent file structure where yaml metadata exists
        yaml_fname = pjoin(yamls_path,
                           basename(dirname(_acq.pathname)),
                           '{}.yaml'.format(container.label))

        # quick workaround if no source yaml
        if not exists(yaml_fname):
            raise IOError('yaml file not found: {}'.format(yaml_fname))

        with open(yaml_fname, 'r') as src:

            # TODO harmonise field names for different sensors

            l1_documents = {
                granule: doc
                for doc in yaml.load_all(src)
            }
            l1_tags = l1_documents[granule]
    else:
        docs = extract_level1_metadata(_acq)
        # Sentinel-2 may contain multiple scenes in a granule
        if isinstance(docs, list):
            l1_tags = [doc for doc in docs
                       if doc.get('tile_id', doc.get('label')) == granule][0]
        else:
            l1_tags = docs
    return l1_tags


def package(l1_path, antecedents, yamls_path, outdir,
            granule, products=ProductPackage.all(), acq_parser_hint=None):
    """
    Package an L2 product.

    :param l1_path:
        A string containing the full file pathname to the Level-1
        dataset.

    :param antecedents:
        A dictionary describing antecedent task outputs
        (currently supporting wagl, eugl-gqa, eugl-fmask)
        to package.

    :param yamls_path:
        A string containing the full file pathname to the yaml
        documents for the indexed Level-1 datasets.

    :param outdir:
        A string containing the full file pathname to the directory
        that will contain the packaged Level-2 datasets.

    :param granule:
        The identifier for the granule

    :param products:
        A list of imagery products to include in the package.
        Defaults to all products.

    :param acq_parser_hint:
        A string that hints at which acquisition parser should be used.

    :return:
        None; The packages will be written to disk directly.
    """
    container = acquisitions(l1_path, acq_parser_hint)
    l1_tags = get_level1_tags(container, granule, yamls_path)
    antecedent_metadata = {}

    # get sensor platform
    platform = get_platform(container, granule)

    with h5py.File(antecedents['wagl'], 'r') as fid:
        grn_id = re.sub(PATTERN2, ARD, granule)
        out_path = pjoin(outdir, grn_id)

        if not exists(out_path):
            os.makedirs(out_path)

        # unpack the standardised products produced by wagl
        wagl_tags, img_paths = unpack_products(products, container, granule,
                                               fid[granule], out_path, platform)

        # unpack supplementary datasets produced by wagl
        supp_paths, timedelta_data = unpack_supplementary(container, granule, fid[granule],
                                                          out_path, platform)

        # add in supplementary paths
        for key in supp_paths:
            img_paths[key] = supp_paths[key]

        # file based globbing, so can't have any other tifs on disk
        qa_paths, contiguity_ones_mask = create_contiguity(products, container, granule, out_path, platform)

        # masking the timedelta_data with contiguity mask to get max and min timedelta within the NBAR product
        # footprint for Landsat sensor. For Sentinel sensor, it inherits from level 1 yaml file
        if platform == 'LANDSAT':
            valid_timedelta_data = numpy.ma.masked_where(contiguity_ones_mask == 0, timedelta_data)
            wagl_tags['timedelta_min'] = numpy.ma.min(valid_timedelta_data)
            wagl_tags['timedelta_max'] = numpy.ma.max(valid_timedelta_data)

        # add in qa paths
        for key in qa_paths:
            img_paths[key] = qa_paths[key]

        # fmask cogtif conversion
        if 'fmask' in antecedents:
            rel_path = pjoin(QA, '{}_FMASK.TIF'.format(grn_id))
            fmask_location = pjoin(out_path, rel_path)
            fmask_cogtif(antecedents['fmask'], fmask_location, platform)
            antecedent_metadata['fmask'] = get_fmask_metadata()

            with rasterio.open(fmask_location) as ds:
                img_paths['fmask'] = get_img_dataset_info(ds, rel_path)

        # map, quicklook/thumbnail, readme, checksum
        create_html_map(out_path)
        create_quicklook(products, container, out_path)
        create_readme(out_path)

        # merge all the yaml documents
        if 'gqa' in antecedents:
            with open(antecedents['gqa']) as fl:
                antecedent_metadata['gqa'] = yaml.load(fl)
        else:
            antecedent_metadata['gqa'] = {
                'error_message': 'GQA has not been configured for this product'
            }

        tags = merge_metadata(l1_tags, wagl_tags, granule,
                              img_paths, platform, **antecedent_metadata)

        with open(pjoin(out_path, 'ARD-METADATA.yaml'), 'w') as src:
            yaml.dump(tags, src, default_flow_style=False, indent=4)

        # finally the checksum
        create_checksum(out_path)
