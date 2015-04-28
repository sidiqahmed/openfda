#!/usr/bin/python

''' MAUDE pipeline for downloading, joining and loading into elasticsearch
'''
from bs4 import BeautifulSoup
import collections
import csv
import glob
import logging
import multiprocessing
import os
from os.path import dirname, join
import re
import sys
import urllib2

import arrow
import elasticsearch
import luigi
import requests
import simplejson as json

from openfda import common, parallel, index_util, elasticsearch_requests
from openfda import process_metadata
from openfda.index_util import AlwaysRunTask
from openfda.maude import join_maude

# Exceed default field_size limit, need to set to sys.maxsize
csv.field_size_limit(sys.maxsize)

RUN_DIR = dirname(dirname(os.path.abspath(__file__)))
BASE_DIR = './data/'
# A directory for holding files that track Task state
META_DIR = join(BASE_DIR, 'maude/meta')
common.shell_cmd('mkdir -p %s', META_DIR)

ES_HOST = luigi.Parameter('localhost:9200', is_global=True)

# The MAUDE pipeline will always create a new index, so we append the date
prefix_name = 'deviceevent'
# Need the prefix for updating the alias and the metadata datetime
INDEX_PREFIX = luigi.Parameter(prefix_name, is_global=True)
suffix = arrow.utcnow().format('YYYY-MM-DD-HH-mm')
index_name = '.'.join([prefix_name, suffix])
INDEX = luigi.Parameter(index_name, is_global=True)

PARTITIONS = 32
CATEGORIES = ['mdrfoi', 'patient', 'foidev', 'foitext']
IGNORE_FILES = ['problem', 'add', 'change']


DEVICE_DOWNLOAD_PAGE = ('http://www.fda.gov/MedicalDevices/'
                       'DeviceRegulationandGuidance/PostmarketRequirements/'
                       'ReportingAdverseEvents/ucm127891.htm')

DEVICE_CLASS_DOWNLOAD = ('http://www.fda.gov/MedicalDevices/'
                         'DeviceRegulationandGuidance/Overview/'
                         'ClassifyYourDevice/ucm051668.htm')

DEVICE_CLASS_ZIP = ('http://www.accessdata.fda.gov/premarket/'
                    'ftparea/foiclass.zip')

CLEARED_DEVICE_URL = 'http://www.accessdata.fda.gov/premarket/ftparea/'
CLEARED_DEV_ZIPS = [CLEARED_DEVICE_URL + 'pmn96cur.zip',
  CLEARED_DEVICE_URL + 'pmn9195.zip',
  CLEARED_DEVICE_URL + 'pmn8690.zip',
  CLEARED_DEVICE_URL + 'pmn8185.zip',
  CLEARED_DEVICE_URL + 'pmn7680.zip']

# patient and text records are missing header rows
PATIENT_KEYS = ['mdr_report_key',
  'patient_sequence_number',
  'date_received',
  'sequence_number_treatment',
  'sequence_number_outcome']

TEXT_KEYS = ['mdr_report_key',
  'mdr_text_key',
  'text_type_code',
  'patient_sequence_number',
  'date_report',
  'text']

DEVICE_KEYS = [
 'mdr_report_key',
 'device_event_key',
 'implant_flag',
 'date_removed_flag',
 'device_sequence_number',
 'date_received',
 'brand_name',
 'generic_name',
 'manufacturer_d_name',
 'manufacturer_d_address_1',
 'manufacturer_d_address_2',
 'manufacturer_d_city',
 'manufacturer_d_state',
 'manufacturer_d_zip_code',
 'manufacturer_d_zip_code_ext',
 'manufacturer_d_country',
 'manufacturer_d_postal_code',
 'expiration_date_of_device',
 'model_number',
 'catalog_number',
 'lot_number',
 'other_id_number',
 'device_operator',
 'device_availability',
 'date_returned_to_manufacturer',
 'device_report_product_code',
 'device_age_text',
 'device_evaluated_by_manufacturer',
 'baseline_brand_name',
 'baseline_generic_name',
 'baseline_model_number',
 'baseline_catalog_number',
 'baseline_other_id_number',
 'baseline_device_family',
 'baseline_shelf_life_contained',
 'baseline_shelf_life_in_months',
 'baseline_pma_flag',
 'baseline_pma_number',
 'baseline_510_k__flag',
 'baseline_510_k__number',
 'baseline_preamendment_flag',
 'baseline_transitional_flag',
 'baseline_510_k__exempt_flag',
 'baseline_date_first_marketed',
 'baseline_date_ceased_marketing'
]

MDR_KEYS = [
  'mdr_report_key',
  'event_key',
  'report_number',
  'report_source_code',
  'manufacturer_link_flag',
  'number_devices_in_event',
  'number_patients_in_event',
  'date_received',
  'adverse_event_flag',
  'product_problem_flag',
  'date_report',
  'date_of_event',
  'reprocessed_and_reused_flag',
  'reporter_occupation_code',
  'health_professional',
  'initial_report_to_fda',
  'distributor_name',
  'distributor_address_1',
  'distributor_address_2',
  'distributor_city',
  'distributor_state',
  'distributor_zip_code',
  'distributor_zip_code_ext',
  'date_facility_aware',
  'type_of_report',
  'report_date',
  'report_to_fda',
  'date_report_to_fda',
  'event_location',
  'report_to_manufacturer',
  'date_report_to_manufacturer',
  'manufacturer_name',
  'manufacturer_address_1',
  'manufacturer_address_2',
  'manufacturer_city',
  'manufacturer_state',
  'manufacturer_zip_code',
  'manufacturer_zip_code_ext',
  'manufacturer_country',
  'manufacturer_postal_code',
  'manufacturer_contact_t_name',
  'manufacturer_contact_f_name',
  'manufacturer_contact_l_name',
  'manufacturer_contact_address_1',
  'manufacturer_contact_address_2',
  'manufacturer_contact_city',
  'manufacturer_contact_state',
  'manufacturer_contact_zip_code',
  'manufacturer_contact_zip_ext',
  'manufacturer_contact_country',
  'manufacturer_contact_postal_code',
  'manufacturer_contact_area_code',
  'manufacturer_contact_exchange',
  'manufacturer_contact_phone_number',
  'manufacturer_contact_extension',
  'manufacturer_contact_pcountry',
  'manufacturer_contact_pcity',
  'manufacturer_contact_plocal',
  'manufacturer_g1_name',
  'manufacturer_g1_address_1',
  'manufacturer_g1_address_2',
  'manufacturer_g1_city',
  'manufacturer_g1_state',
  'manufacturer_g1_zip_code',
  'manufacturer_g1_zip_code_ext',
  'manufacturer_g1_country',
  'manufacturer_g1_postal_code',
  'source_type',
  'date_manufacturer_received',
  'device_date_of_manufacturer',
  'single_use_flag',
  'remedial_action',
  'previous_use_code',
  'removal_correction_number',
  'event_type'
]

class DownloadDeviceEvents(luigi.Task):
  def requires(self):
    return []

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'maude/raw/events'))

  def run(self):
    # TODO(hansnelsen): copied from the FAERS pipeline, consider refactoring
    #                   into a generalized approach
    zip_urls = []
    soup = BeautifulSoup(urllib2.urlopen(DEVICE_DOWNLOAD_PAGE).read())
    for a in soup.find_all(href=re.compile('.*.zip')):
      zip_urls.append(a['href'])
    if not zip_urls:
      logging.fatal('No MAUDE Zip Files Found At %s' % DEVICE_CLASS_DOWNLOAD)
    for zip_url in zip_urls:
      filename = zip_url.split('/')[-1]
      common.download(zip_url, join(self.output().path, filename))

class DownloadFoiClass(luigi.Task):
  def requires(self):
    return []

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'maude/raw/class'))

  def run(self):
    output_filename = join(self.output().path, DEVICE_CLASS_ZIP.split('/')[-1])
    common.download(DEVICE_CLASS_ZIP, output_filename)

class Download_510K(luigi.Task):
  def requires(self):
    return []

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'maude/raw/510k'))

  def run(self):
    output_dir = self.output().path

    for zip_url in CLEARED_DEV_ZIPS:
      output_filename = join(output_dir, zip_url.split('/')[-1])
      common.download(zip_url, output_filename)

class ExtractAndCleanDownloads(luigi.Task):
  ''' Unzip each of the download files and remove all the non-UTF8 characters.
      Unzip -p streams the data directly to iconv which then writes to disk.
  '''
  def requires(self):
    return [DownloadDeviceEvents(), DownloadFoiClass(), Download_510K()]

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'maude/extracted'))

  def run(self):
    output_dir = self.output().path
    common.shell_cmd('mkdir -p %s', output_dir)
    for i in range(len(self.input())):
      input_dir = self.input()[i].path
      for zip_filename in glob.glob(input_dir + '/*.zip'):
        txt_name = zip_filename.replace('zip', 'txt')
        txt_name = txt_name.replace('raw', 'extracted')
        common.shell_cmd('mkdir -p %s', dirname(txt_name))
        cmd = 'unzip -p %s | iconv -f "ISO-8859-1//TRANSLIT" -t UTF8 -c > %s'
        logging.info('Unzipping and converting %s', zip_filename)
        common.shell_cmd(cmd, zip_filename, txt_name)

class PartionEventData(luigi.Task):
  ''' The historic files are not balanced (patient and text are split by year)
      and master and device are huge. This process partitions all of the data
      by MDR_REPORT_KEY (which is the first column in all files) and puts the
      data in an appropriate category file. This also gives us the opportunity
      to clean out records that do not have a join key as well as lower casing
      the header names.

      For example: all of the data for a particular MDR_REPORT_KEY will be in
                   four files, let's say partition 5:
                   5.mdrfoi.txt, 5.patient.txt, 5.foidev.txt, 5.foitext.txt
                   All records in these files will have a MDR_REPORT_KEY modulo
                   PARTITIONS = 5
  '''
  def requires(self):
    return ExtractAndCleanDownloads()

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'maude/partitioned/events'))

  def run(self):
    input_dir = join(self.input().path, 'events')
    output_dir = self.output().path
    fh_dict = {}
    common.shell_cmd('mkdir -p %s', output_dir)

    # Headers need to be written to the start of each partition. The patient
    # and foitext headers are manually created. The headers for mdrfoi and
    # foidev are detected from the source and placed into the header dictionary.
    header = {}
    header['patient'] = PATIENT_KEYS
    header['foitext'] = TEXT_KEYS
    header['mdrfoi'] = MDR_KEYS
    header['foidev'] = DEVICE_KEYS

    for i in range(PARTITIONS):
      for category in CATEGORIES:
        filename = str(i) + '.' + category + '.txt'
        filename = join(output_dir, filename)
        logging.info('Creating file handles for writing %s', filename)
        output_handle = open(filename, 'w')
        csv_writer = csv.writer(output_handle, delimiter='|')
        csv_writer.writerow(header[category])
        fh_dict[category + str(i)] = output_handle

    # Because we download all zips from the site, we need to ignore some of the
    # files for the partitioning process. Remove if files are excluded from
    # download.
    for filename in glob.glob(input_dir + '/*.txt'):
      logging.info('Processing: %s', filename)
      skip = False
      for ignore in IGNORE_FILES:
        if ignore in filename:
          skip = True

      if skip:
        logging.info('Skipping: %s', filename)
        continue

      for category in CATEGORIES:
        if category in filename:
          file_category = category

      # MAUDE files do not escape quote characters, we just hope that no
      # pipe characters occur in records...
      file_handle = csv.reader(open(filename, 'r'),
                               quoting=csv.QUOTE_NONE,
                               delimiter='|')
      partioned = collections.defaultdict(list)
      for i, row in enumerate(file_handle):
        # skip header rows
        if (i == 0) and ('MDR_REPORT_KEY' in row): continue

        # Only work with rows that have data and the first column is a number
        if row and row[0].isdigit():
          partioned[int(row[0]) % PARTITIONS].append(row)
        else:
          logging.warn('Skipping row: %s', row)

      for partnum, rows in partioned.iteritems():
        output_handle = fh_dict[file_category + str(partnum)]
        csv_writer = csv.writer(output_handle, delimiter='|')
        logging.info('Writing: %s %s %s %s',
                     partnum,
                     file_category + str(partnum),
                     output_handle,
                     len(rows))
        csv_writer.writerows(rows)

class JoinPartitions(luigi.Task):
  def requires(self):
    return PartionEventData()

  def output(self):
    return luigi.LocalTarget(join(BASE_DIR, 'maude/json'))

  def run(self):
    input_dir = self.input().path
    output_dir = self.output().path

    common.shell_cmd('mkdir -p %s', output_dir)
    # TODO(hansnelsen): change to the openfda.parallel version of multiprocess
    pool = multiprocessing.Pool(processes=6)
    for i in range(PARTITIONS):
      partition_dict = {}
      output_filename = join(output_dir, str(i) + '.maude.json')
      # Get all of the files for the current partition
      for filename in glob.glob(input_dir + '/' + str(i) + '.*.txt'):
        for file_type in CATEGORIES:
          if file_type in filename:
            logging.info('Using file %s for joining', filename)
            partition_dict[file_type] = filename

      logging.info('Starting Partition %d', i)
      master_file = partition_dict['mdrfoi']
      patient_file = partition_dict['patient']
      device_file = partition_dict['foidev']
      text_file = partition_dict['foitext']
      pool.apply_async(join_maude.join_maude, (master_file,
                                               patient_file,
                                               device_file,
                                               text_file,
                                               output_filename))

    pool.close()
    pool.join()

class ResetElasticSearch(AlwaysRunTask):
  ''' This will always make a new index. The approach is to make a new index
      with a date suffix and then use the alias feature in elasticsearch to
      point the deviceevent name to that new index (SwapIndex task does this).
  '''
  es_host = ES_HOST
  index_name = INDEX

  def _run(self):
    es = elasticsearch.Elasticsearch(self.es_host)
    elasticsearch_requests.load_mapping(es,
                                        self.index_name,
                                        'maude',
                                        './schemas/maude_mapping.json')

class LoadJSON(luigi.Task):
  es_host = ES_HOST
  index_name = INDEX

  def requires(self):
    return [ResetElasticSearch(), JoinPartitions()]

  def output(self):
    return luigi.LocalTarget(join(META_DIR, self.index_name + '.loadjson.done'))

  def run(self):
   json_dir = self.input()[1].path
   input_glob = glob.glob(json_dir + '/*.json')
   for file_name in input_glob:
     logging.info('Running file %s', file_name)
     parallel.mapreduce(
       input_collection=parallel.Collection.from_glob(file_name,
                                                      parallel.JSONLineInput),
       mapper=index_util.ReloadJSONMapper(self.es_host,
                                          self.index_name,
                                          'maude'),
       reducer=parallel.IdentityReducer(),
       output_prefix='/tmp/loadjson.' + index_name,
       map_workers=1)

   os.system('touch "%s"' % self.output().path)

class SwapIndex(luigi.Task):
  ''' This is a standalone task for now, until we get a feel for the pipeline in
      production. This step should be run after LoadJSON is run and some sort of
      test is run to determine the swap is realistic.

      This task can be used to rollback an index as well, just pass in the
      --swap-index-name <name of index you want to be aliased to deviceevent>
  '''
  es_host = ES_HOST
  index_prefix = INDEX_PREFIX
  swap_index_name = luigi.Parameter()

  def requires(self):
    return []

  def output(self):
    output_filename = self.swap_index_name + '.swap.done'
    return luigi.LocalTarget(join(META_DIR, output_filename))

  def run(self):
    # We parse the date from the index so that the last_update_date is correct
    # in the API meta field
    # Assumes format of: indexname.YYYY-MM-DD-HH-mm
    swap_index_date = arrow.get(self.swap_index_name.split('.')[1],
                                'YYYY-MM-DD-HH-mm')\
                           .format('YYYY-MM-DD')
    index_util.swap_index(self.es_host, self.index_prefix, self.swap_index_name)
    process_metadata.update_process_datetime(self.index_prefix,
                                             swap_index_date)
    os.system('touch "%s"' % self.output().path)

if __name__ == '__main__':
  logging.basicConfig(stream=sys.stderr,
                      format=\
                      '%(created)f %(filename)s:%(lineno)s \
                      [%(funcName)s] %(message)s',
                      level=logging.INFO)
  logging.getLogger('elasticsearch').setLevel(logging.WARN)
  luigi.run()
