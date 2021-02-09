import sys
import os
import zipfile
import json
import io
import re
import tarfile
import csv

import httpx
from bs4 import BeautifulSoup
from xdg import XDG_CONFIG_HOME
from progress.bar import Bar
from progress.spinner import Spinner

from ..strains import StrainCollection, Strain

from ..logconfig import LogConfig
logger = LogConfig.getLogger(__file__)

from .runbigscape import run_bigscape

PAIREDOMICS_PROJECT_DATA_ENDPOINT = 'http://pairedomicsdata.bioinformatics.nl/api/projects'
PAIREDOMICS_PROJECT_URL = 'https://pairedomicsdata.bioinformatics.nl/api/projects/{}'
GNPS_DATA_DOWNLOAD_URL = 'https://gnps.ucsd.edu/ProteoSAFe/DownloadResult?task={}&view=download_clustered_spectra'

ANTISMASH_DB_PAGE_URL = 'https://antismash-db.secondarymetabolites.org/output/{}/'
ANTISMASH_DB_DOWNLOAD_URL = 'https://antismash-db.secondarymetabolites.org/output/{}/{}'

ANTISMASH_DBV2_PAGE_URL = 'https://antismash-dbv2.secondarymetabolites.org/output/{}/'
ANTISMASH_DBV2_DOWNLOAD_URL = 'https://antismash-dbv2.secondarymetabolites.org/output/{}/{}'

NCBI_GENBANK_LOOKUP_URL = 'https://www.ncbi.nlm.nih.gov/nuccore/{}?report=docsum'
NCBI_ASSEMBLY_LOOKUP_URL = 'https://www.ncbi.nlm.nih.gov/assembly?LinkName=nuccore_assembly&from_uid={}'

JGI_GENOME_LOOKUP_URL = 'https://img.jgi.doe.gov/cgi-bin/m/main.cgi?section=TaxonDetail&page=taxonDetail&taxon_oid={}'

MIBIG_JSON_URL = 'https://dl.secondarymetabolites.org/mibig/mibig_json_{}.tar.gz'

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:86.0) Gecko/20100101 Firefox/86.0'

class GenomeStatus:

    def __init__(self, original_id, resolved_id, attempted=False, filename=""):
        self.original_id = original_id
        self.resolved_id = None if resolved_id == 'None' else resolved_id
        self.attempted = True if attempted == 'True' else False
        self.filename = filename

    @classmethod
    def from_csv(cls, original_id, resolved_id, attempted, filename):
        return cls(original_id, resolved_id, attempted, filename)

    def to_csv(self):
        return ','.join([self.original_id, str(self.resolved_id), str(self.attempted), self.filename])

def download_and_extract_mibig_json(download_path, output_path, version='1.4'):
    archive_path = os.path.join(download_path, 'mibig_json_{}.tar.gz'.format(version))
    logger.debug('Checking for existing MiBIG archive at {}'.format(archive_path))
    cached = False
    if os.path.exists(archive_path):
        logger.info('Found cached file at {}'.format(archive_path))
        try:
            _ = tarfile.open(archive_path)
            cached = True
        except:
            logger.info('Invalid MiBIG archive found, will download again')
            os.unlink(archive_path)

    if not cached:
        url = MIBIG_JSON_URL.format(version)
        with open(archive_path, 'wb') as f:
            total_bytes, last_total = 0, 0
            with httpx.stream('GET', url) as r:
                filesize = int(r.headers['content-length'])
                bar = Bar(url, max=filesize, suffix='%(percent)d%%')
                for data in r.iter_bytes():
                    f.write(data)
                    total_bytes += len(data)
                    bar.next(len(data))
                bar.finish()
    
    logger.debug('Extracting MiBIG JSON data')

    if os.path.exists(os.path.join(output_path, 'completed')):
        return True

    mibig_gz = tarfile.open(archive_path, 'r:gz')
    # extract and rename to "mibig_json"
    # TODO annoyingly the 2.0 version has been archived with a subdirectory, while
    # 1.4 just dumps all the files into the current directory, so if/when 2.0 support
    # is required this will need to handle both cases
    mibig_gz.extractall(path=os.path.join(output_path))
    # os.rename(os.path.join(self.project_file_cache, 'mibig_json_{}'.format(version)), os.path.join(self.project_file_cache, 'mibig_json'))

    open(os.path.join(output_path, 'completed'), 'w').close()

    return True

def generate_strain_mappings(strains, strain_mappings_file, antismash_dir):
    # first time downloading, this file will not exist, should only need done once
    if not os.path.exists(strain_mappings_file):
        logger.info('Generating strain mappings file')
        for root, dirs, files in os.walk(antismash_dir):
            for f in files: 
                if not f.endswith('.gbk'):
                    continue

                # use the containing folder of the .gbk file as the strain name,
                # and then take everything but ".gbk" from the filename and use 
                # that as an alias
                strain_name = os.path.split(root)[1]
                strain_alias = os.path.splitext(f)[0]
                if strain_alias.find('.') != -1:
                    strain_alias = strain_alias[:strain_alias.index('.')]
                if strains.lookup(strain_name) is not None:
                    strains.lookup(strain_name).add_alias(strain_alias)
                else:
                    logger.warning('Failed to lookup strain name: {}'.format(strain_name))
        logger.info('Saving strains to {}'.format(strain_mappings_file))
        strains.save_to_file(strain_mappings_file)
    else:
        logger.info('Strain mappings already generated')

    return strains

class Downloader(object):

    def __init__(self, platform_id, force_download=False):
        self.gnps_massive_id = platform_id
        self.pairedomics_id = None
        self.gnps_task_id = None
        self.local_cache = os.path.join(os.getenv('HOME'), 'nplinker_data', 'pairedomics')
        self.local_download_cache = os.path.join(self.local_cache, 'downloads')
        self.local_file_cache = os.path.join(self.local_cache, 'extracted')
        self.all_project_json_file = os.path.join(self.local_cache, 'all_projects.json')
        self.all_project_json = None
        self.project_json_file = os.path.join(self.local_cache, '{}.json'.format(self.gnps_massive_id))
        self.project_json = None
        os.makedirs(self.local_cache, exist_ok=True)

        self.json_data = None
        self.strains = StrainCollection()
        self.growth_media = {}
        
        logger.info('Downloader for {}, caching to {}'.format(platform_id, self.local_cache))

        if not os.path.exists(self.project_json_file) or force_download:
            logger.info('Downloading new copy of platform project data...')
            self.all_project_json = self._download_platform_json_to_file(PAIREDOMICS_PROJECT_DATA_ENDPOINT, self.all_project_json_file)
        else:
            logger.info('Using existing copy of platform project data')
            with open(self.all_project_json_file, 'r') as f:
                self.all_project_json = json.load(f)

        # query the pairedomics webservice with the project ID to retrieve the data. unfortunately
        # this is not the MSV... ID, but an internal GUID string. To get that, first need to get the
        # list of all projects, find the one with a 'metabolite_id' value matching the MSV... ID, and
        # then extract its '_id' value to get the GUID

        # find the specified project and store its ID
        for project in self.all_project_json['data']:
            pairedomics_id = project['_id']
            gnps_massive_id = project['metabolite_id']

            if gnps_massive_id == platform_id:
                self.pairedomics_id = pairedomics_id
                logger.debug('platform_id {} matched to pairedomics_id {}'.format(self.gnps_massive_id, self.pairedomics_id))
                break

        if self.pairedomics_id is None:
            raise Exception('Failed to find a pairedomics project with ID {}'.format(self.gnps_massive_id))

        # now get the project JSON data
        logger.info('Found project, retrieving JSON data...')
        self.project_json = self._download_platform_json_to_file(PAIREDOMICS_PROJECT_URL.format(self.pairedomics_id), self.project_json_file)
        
        if 'molecular_network' not in self.project_json['metabolomics']['project']:
            raise Exception('Dataset has no GNPS data URL!')

        self.gnps_task_id = self.project_json['metabolomics']['project']['molecular_network']

        # create local cache folders for this dataset
        self.project_download_cache = os.path.join(self.local_download_cache, self.gnps_massive_id)
        os.makedirs(self.project_download_cache, exist_ok=True)

        self.project_file_cache = os.path.join(self.local_file_cache, self.gnps_massive_id)
        os.makedirs(self.project_file_cache, exist_ok=True)

        # placeholder directories
        for d in ['antismash', 'bigscape']:
            os.makedirs(os.path.join(self.project_file_cache, d), exist_ok=True)

        with io.open(os.path.join(self.project_file_cache, 'platform_data.json'), 'w', encoding='utf-8') as f:
            f.write(str(self.project_json))

        self.strain_mappings_file = os.path.join(self.project_file_cache, 'strain_mappings.csv')

    def get(self, do_bigscape, extra_bigscape_parameters):
        logger.info('Going to download the metabolomics data file')

        self._download_metabolomics_zipfile(self.gnps_task_id)
        self._download_genomics_data(self.project_json['genomes'])
        self._parse_genome_labels(self.project_json['genome_metabolome_links'], self.project_json['genomes'])
        self._generate_strain_mappings()
        self._download_mibig_json() # TODO version
        self._run_bigscape(do_bigscape, extra_bigscape_parameters) 

    def _is_new_gnps_format(self, directory):
        # TODO this should test for existence of quantification table instead
        return os.path.exists(os.path.join(directory, 'qiime2_output'))

    def _run_bigscape(self, do_bigscape, extra_bigscape_parameters):
        # TODO this currently assumes docker environment, allow customisation?
        # can check if in container with: https://stackoverflow.com/questions/20010199/how-to-determine-if-a-process-runs-inside-lxc-docker
        if not do_bigscape:
            logger.info('BiG-SCAPE disabled by configuration, not running it')
            return

        logger.info('Running BiG-SCAPE! extra_bigscape_parameters="{}"'.format(extra_bigscape_parameters))
        try:
            run_bigscape('/app/BiG-SCAPE/bigscape.py', os.path.join(self.project_file_cache, 'antismash'), os.path.join(self.project_file_cache, 'bigscape'), '/app', cutoffs=[0.3], extra_params=extra_bigscape_parameters)
        except Exception as e:
            logger.warning('Failed to run BiG-SCAPE on antismash data, error was "{}"'.format(e))

    def _generate_strain_mappings(self):
        gen_strains = generate_strain_mappings(self.strains, self.strain_mappings_file, os.path.join(self.project_file_cache, 'antismash'))


    def _resolve_genbank_accession(self, genbank_id):
        """
        Super hacky way of trying to resolve the GenBank accession into RefSeq accession.
        """
        logger.info('Attempting to resolve RefSeq accession from Genbank accession {}'.format(genbank_id))
        # genbank id => genbank seq => refseq

        # The GenBank accession can have several formats:
        # 1: BAFR00000000.1
        # 2: NZ_BAGG00000000.1
        # 3: NC_016887.1
        # Case 1 is the default.
        if '_' in genbank_id:
            # case 2
            if len(genbank_id.split('_')[-1].split('.')[0]) == 12:
                genbank_id = genbank_id.split('_')[-1]
            # case 3
            else:
                genbank_id = genbank_id.lower() 

        # Look up genbank ID
        try:
            url = NCBI_GENBANK_LOOKUP_URL.format(genbank_id)
            resp = httpx.get(url)
            soup = BeautifulSoup(resp.content, 'html.parser')
            ids = soup.find('dl', {'class': 'rprtid'})
            for field_idx, field in enumerate(ids.findChildren()):
                if field.getText().strip() == 'GI:':
                    seq_id = ids.findChildren()[field_idx + 1].getText().strip()
                    break

            # Look up assembly
            url = NCBI_ASSEMBLY_LOOKUP_URL.format(seq_id)
            resp = httpx.get(url)
            soup = BeautifulSoup(resp.content, 'html.parser')
            title_href = soup.find('p', {'class': 'title'}).a['href']
            refseq_id = title_href.split('/')[-1].split('.')[0]
            return refseq_id
        except Exception as e:
            logger.warning('Failed resolving GenBank accession {}, error {}'.format(genbank_id, e))

        return None

    def _resolve_jgi_accession(self, jgi_id):
        url = JGI_GENOME_LOOKUP_URL.format(jgi_id)
        # no User-Agent header produces a 403 Forbidden error on this site...
        resp = httpx.get(url, headers={'User-Agent': USER_AGENT})
        soup = BeautifulSoup(resp.content, 'html.parser')
        # find the table entry giving the NCBI assembly accession ID
        link = soup.find('a', href=re.compile('https://www.ncbi.nlm.nih.gov/nuccore/.*'))
        if link is None:
            return None
        
        return self._resolve_genbank_accession(link.text)

    def _get_best_available_genome_id(self, genome_id_data):
        if 'RefSeq_accession' in genome_id_data:
            return genome_id_data['RefSeq_accession']
        elif 'GenBank_accession' in genome_id_data:
            return genome_id_data['GenBank_accession']
        elif 'JGI_Genome_ID' in genome_id_data:
            return genome_id_data['JGI_Genome_ID']

        return None

    def _resolve_genome_id_data(self, genome_id_data):
        if 'RefSeq_accession' in genome_id_data:
            # best case, can use this directly
            return genome_id_data['RefSeq_accession']
        elif 'GenBank_accession' in genome_id_data:
            # resolve via NCBI
            return self._resolve_genbank_accession(genome_id_data['GenBank_accession'])
        elif 'JGI_Genome_ID' in genome_id_data:
            # resolve via JGI => NCBI
            return self._resolve_jgi_accession(genome_id_data['JGI_Genome_ID'])

        logger.warning('Unable to resolve genome_ID: {}'.format(genome_id_data))
        return None

    def _download_genomics_data(self, genome_records):
        genome_status = {}

        # this file records genome IDs and local filenames to avoid having to repeat HTTP requests
        # each time the app is loaded (this can take a lot of time if there are dozens of genomes)
        genome_status_file = os.path.join(self.project_download_cache, 'genome_status.txt')

        # genome lookup status info
        if os.path.exists(genome_status_file):
            with open(genome_status_file, 'r') as f:
                for line in csv.reader(f):
                    asobj = GenomeStatus.from_csv(*line)
                    genome_status[asobj.original_id] = asobj

        for i, genome_record in enumerate(genome_records):
            label = genome_record['genome_label']

            # get the best available ID from the dict
            best_id = self._get_best_available_genome_id(genome_record['genome_ID'])

            # use this to check if the lookup has already been attempted and if
            # so if the file is cached locally
            if best_id not in genome_status:
                genome_status[best_id] = GenomeStatus(best_id, None)

            genome_obj = genome_status[best_id]
                
            logger.info('Checking for antismash data {}/{}, current genome ID={}'.format(i+1, len(genome_records), best_id))
            # first check if file is cached locally
            if os.path.exists(genome_obj.filename):
                # file already downloaded
                logger.info('Genome ID {} already downloaded to {}'.format(best_id, genome_obj.filename))
                genome_record['resolved_id'] = genome_obj.resolved_id
            elif genome_obj.attempted:
                # lookup attempted previously but failed
                logger.info('Genome ID {} skipped due to previous failure'.format(best_id))
                genome_record['resolved_id'] = genome_obj.resolved_id
            else:
                # if no existing file and no lookup attempted, can start process of
                # trying to retrieve the data

                # lookup the ID
                logger.info('Beginning lookup process for genome ID {}'.format(best_id))

                genome_obj.resolved_id = self._resolve_genome_id_data(genome_record['genome_ID'])
                genome_obj.attempted = True

                if genome_obj.resolved_id is None:
                    # give up on this one
                    logger.warning('Failed lookup for genome ID {}'.format(best_id))
                    with open(genome_status_file, 'a+') as f:
                        f.write(genome_obj.to_csv()+'\n')
                    continue

                # if we got a refseq ID, now try to download the data from antismash
                if self._download_antismash_zip(genome_obj):
                    logger.info('Genome data successfully downloaded for {}'.format(best_id))
                    genome_record['resolved_id'] = genome_obj.resolved_id
                else:
                    logger.warning('Failed to download antiSMASH data for genome ID {} ({})'.format(genome_obj.resolved_id, genome_obj.original_id))

                with open(genome_status_file, 'a+') as f:
                    f.write(genome_obj.to_csv()+'\n')

            self._extract_antismash_zip(genome_obj)

        missing = len([x for x in genome_status.values() if len(x.filename) == 0])
        logger.info('Dataset has {} missing sets of antiSMASH data (from a total of {})'.format(missing, len(genome_records)))

        with open(genome_status_file, 'w') as f:
            for obj in genome_status.values():
                f.write(obj.to_csv()+'\n')

        if missing == len(genome_records):
            logger.warning('Failed to successfully retrieve ANY genome data!')

    def _download_mibig_json(self, version='1.4'):
        output_path = os.path.join(self.project_file_cache, 'mibig_json')

        download_and_extract_mibig_json(self.project_download_cache, output_path, version)

        open(os.path.join(output_path, 'completed'), 'w').close()

        return True

    def _get_antismash_db_page(self, genome_obj):
        # want to try up to 4 different links here, v1 and v2 databases, each
        # with and without the .1 suffix on the accesssion ID

        accesssions = [genome_obj.resolved_id, genome_obj.resolved_id + '.1']
        for base_url in [ANTISMASH_DB_PAGE_URL, ANTISMASH_DBV2_PAGE_URL]:
            for accession in accesssions:
                url = base_url.format(accession)
                link = None

                logger.info('antismash DB lookup for {} => {}'.format(accession, url))
                try:
                    resp = httpx.get(url)
                    soup = BeautifulSoup(resp.content, 'html.parser')
                    # retrieve .zip file download link
                    link = soup.find('a', {'href': lambda url: url.endswith('.zip')})
                except Exception as e:
                    logger.debug('antiSMASH DB page load failed: {}'.format(e))

                if link is not None:
                    logger.info('antiSMASH lookup succeeded! Filename is {}'.format(link['href']))
                    # save with the .1 suffix if that worked
                    genome_obj.resolved_id = accession
                    return link['href']

        return None

    def _get_antismash_zip_data(self, accession_id, filename, local_path):
        for base_url in [ANTISMASH_DB_DOWNLOAD_URL, ANTISMASH_DBV2_DOWNLOAD_URL]:
            zipfile_url = base_url.format(accession_id, filename)
            with open(local_path, 'wb') as f:
                total_bytes = 0
                try: 
                    with httpx.stream('GET', zipfile_url) as r:
                        if r.status_code == 404:
                            logger.debug('antiSMASH download URL was a 404')
                            continue

                        logger.info('Downloading from antiSMASH: {}'.format(zipfile_url))
                        filesize = int(r.headers['content-length'])
                        bar = Bar(filename, max=filesize, suffix='%(percent)d%%')
                        for data in r.iter_bytes():
                            f.write(data)
                            total_bytes += len(data)
                            bar.next(len(data))
                        bar.finish()
                except Exception as e:
                    logger.warning('antiSMASH zip download failed: {}'.format(e))
                    continue
        
            return True

        return False
    
    def _download_antismash_zip(self, antismash_obj):
        # save zip files to avoid having to repeat above lookup every time
        local_path = os.path.join(self.project_download_cache, '{}.zip'.format(antismash_obj.resolved_id))
        logger.debug('Checking for existing antismash zip at {}'.format(local_path))

        cached = False
        # if the file exists locally
        if os.path.exists(local_path):
            logger.info('Found cached file at {}'.format(local_path))
            try:
                # check if it's a valid zip file, if so treat it as cached
                _ = zipfile.ZipFile(local_path)
                cached = True
                antismash_obj.filename = local_path
            except zipfile.BadZipFile as bzf:
                # otherwise delete and redownload
                logger.info('Invalid antismash zipfile found ({}). Will download again'.format(bzf))
                os.unlink(local_path)
                antismash_obj.filename = ""

        if not cached:
            filename = self._get_antismash_db_page(antismash_obj)
            if filename is None:
                return False

            self._get_antismash_zip_data(antismash_obj.resolved_id, filename, local_path)    
            antismash_obj.filename = local_path

        return True

    def _extract_antismash_zip(self, antismash_obj):
        if antismash_obj.filename is None or len(antismash_obj.filename) == 0:
            return False

        output_path = os.path.join(self.project_file_cache, 'antismash', antismash_obj.resolved_id)
        exists_already = os.path.exists(output_path) and os.path.exists(os.path.join(output_path, 'completed'))

        logger.debug('Extracting antismash data to {}, exists_already = {}'.format(output_path, exists_already))
        if exists_already:
            return True

        # create a subfolder for each set of genome data (the zip files used to be
        # constructed with path info but that seems to have changed recently)
        if not os.path.exists(output_path):
            os.makedirs(output_path, exist_ok=True)

        antismash_zip = zipfile.ZipFile(antismash_obj.filename)
        kc_prefix1 = '{}/knownclusterblast'.format(antismash_obj.resolved_id)
        kc_prefix2 = 'knownclusterblast'
        for zip_member in antismash_zip.namelist():
            # TODO other files here?
            if zip_member.endswith('.gbk') or zip_member.endswith('.json'):
                antismash_zip.extract(zip_member, path=output_path)
            elif zip_member.startswith(kc_prefix1) or zip_member.startswith(kc_prefix2):
                antismash_zip.extract(zip_member, path=output_path)

        open(os.path.join(output_path, 'completed'), 'w').close()

        return True

    def _parse_genome_labels(self, met_records, gen_records):
        temp = {}
        mc, gc = 0, 0

        # this method is supposed to extract the fields from the JSON data 
        # which map strain names to mzXML files on the metabolomics side, 
        # and to BGCs on the genomics side, and use that data to build a set
        # of NPLinker Strain objects for the current dataset

        # metabolomics: each of the JSON records should contain a field named
        # "genome_label", which is the one that should be used as the canonical
        # name for this strain by nplinker. Another field is called "metabolomics_file",
        # and this contains a URL to the corresponding mzXML file. so we want to
        # create a set of mappings from one to the other, with the complication that
        # there might be mappings from 2 or more mzXMLs to a single strain. 
        # also should record the growth medium using the "sample_preparation_label" field. 
        for rec in met_records:
            # this is the global strain identifier we should use
            label = rec['genome_label']
            # only want to record the actual filename of the mzXML URL
            filename = os.path.split(rec['metabolomics_file'])[1]

            # add the mzXML mapping for this strain
            if label in temp:
                temp[label].append(filename)
            else:
                temp[label] = [filename]
            mc += 1

            if label in self.growth_media:
                self.growth_media[label].add(rec['sample_preparation_label'])
            else:
                self.growth_media[label] = set([rec['sample_preparation_label']])

        for rec in gen_records:
            label = rec['genome_label']
            accession = rec.get('resolved_id', None)
            if accession is None:
                # this will happen for genomes where we couldn't retrieve data or resolve the ID
                logger.warning('Failed to extract accession from genome with label {}'.format(label))
                continue

            if label in temp:
                temp[label].append(accession)
            else:
                temp[label] = [accession]
                gc += 1

        logger.info('Extracted {} strains from JSON (met={}, gen={})'.format(len(temp), mc, gc))
        for strain_label, strain_aliases in temp.items():
            strain = Strain(strain_label)
            for alias in strain_aliases:
                strain.add_alias(alias)
            self.strains.add(strain)

    def _download_metabolomics_zipfile(self, gnps_task_id):
        url = GNPS_DATA_DOWNLOAD_URL.format(gnps_task_id)

        self.metabolomics_zip = os.path.join(self.project_download_cache, 'metabolomics_data.zip')

        cached = False
        if os.path.exists(self.metabolomics_zip):
            logger.info('Found existing metabolomics_zip at {}'.format(self.metabolomics_zip))
            try:
                mbzip = zipfile.ZipFile(self.metabolomics_zip)
                cached = True
            except zipfile.BadZipFile as bzf:
                logger.info('Invalid metabolomics zipfile found, will download again!')
                os.unlink(self.metabolomics_zip)
        
        if not cached:
            logger.info('Downloading metabolomics data from {}'.format(url))
            with open(self.metabolomics_zip, 'wb') as f:
                # note that this requires a POST, not a GET
                total_bytes, last_total = 0, 0
                spinner = Spinner('Downloading metabolomics data... ')
                with httpx.stream('POST', url) as r:
                    for data in r.iter_bytes():
                        f.write(data)
                        total_bytes += len(data)
                        spinner.next()
                spinner.finish()

        logger.info('Downloaded metabolomics data!')

        # this should throw an exception if zip is malformed etc
        mbzip = zipfile.ZipFile(self.metabolomics_zip)

        logger.info('Extracting files to {}'.format(self.project_file_cache))
        # extract the contents to the file cache folder. only want some of the files
        # so pick them out and only extract those:
        # - root/spectra/*.mgf 
        # - root/clusterinfosummarygroup_attributes_withIDs_withcomponentID/*.tsv
        # - root/networkedges_selfloop/*.pairsinfo
        # - root/quantification_table*
        # - root/metadata_table*
        # - root/DB_result*
        for member in mbzip.namelist():
            if member.startswith('clusterinfosummarygroup_attributes_withIDs_withcomponentID')\
                or member.startswith('networkedges_selfloop')\
                or member.startswith('quantification_table')\
                or member.startswith('metadata_table')\
                or member.startswith('DB_result')\
                or member.startswith('result_specnets_DB'):
                    mbzip.extract(member, path=self.project_file_cache)
            # move the MGF file to a /spectra subdirectory to better fit expected structure
            elif member.endswith('.mgf'):
                os.makedirs(os.path.join(self.project_file_cache, 'spectra'), exist_ok=True)
                mbzip.extract(member, path=os.path.join(self.project_file_cache, 'spectra'))
                        
        if self._is_new_gnps_format(self.project_file_cache):
            logger.info('Found NEW GNPS structure')
        else:
            logger.info('Found OLD GNPS structure')

    def _download_platform_json_to_file(self, url, local_path):
        resp = httpx.get(url)
        if not resp.status_code == 200:
            raise Exception('Failed to download {} (status code {})'.format(url, resp.status_code))

        content = json.loads(resp.content)
        with open(local_path, 'w') as f:
            json.dump(content, f)

        logger.debug('Downloaded {} to {}'.format(url, local_path))

        return content

if __name__ == "__main__":
    # salinispora dataset 
    # d = Downloader('MSV000079284')

    d = Downloader('MSV000078836').get(False, "")
    # d = Downloader('MSV000079284').get(False, "")
