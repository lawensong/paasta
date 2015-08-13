import json
import logging
import os
import re
import urlparse

import chronos
import isodate

import monitoring_tools
import service_configuration_lib
from paasta_tools.utils import get_code_sha_from_dockerurl
from paasta_tools.utils import get_config_hash
from paasta_tools.utils import get_default_branch
from paasta_tools.utils import get_docker_url
from paasta_tools.utils import InstanceConfig
from paasta_tools.utils import load_deployments_json
from paasta_tools.utils import load_system_paasta_config
from paasta_tools.utils import PATH_TO_SYSTEM_PAASTA_CONFIG_DIR


# In Marathon spaces are not allowed, in Chronos periods are not allowed.
# In the Chronos docs a space is suggested as the natural separator
SPACER = " "
# Until Chronos supports dots in the job name, we use this separator internally
INTERNAL_SPACER = '.'

PATH_TO_CHRONOS_CONFIG = os.path.join(PATH_TO_SYSTEM_PAASTA_CONFIG_DIR, 'chronos.json')
DEFAULT_SOA_DIR = service_configuration_lib.DEFAULT_SOA_DIR
log = logging.getLogger('__main__')


class ChronosNotConfigured(Exception):
    pass


class ChronosConfig(dict):

    def __init__(self, config, path):
        self.path = path
        super(ChronosConfig, self).__init__(config)

    def get_url(self):
        """:returns: The Chronos API endpoint"""
        try:
            return self['url']
        except KeyError:
            raise ChronosNotConfigured('Could not find chronos url in system chronos config: %s' % self.path)

    def get_username(self):
        """:returns: The Chronos API username"""
        try:
            return self['user']
        except KeyError:
            raise ChronosNotConfigured('Could not find chronos user in system chronos config: %s' % self.path)

    def get_password(self):
        """:returns: The Chronos API password"""
        try:
            return self['password']
        except KeyError:
            raise ChronosNotConfigured('Could not find chronos password in system chronos config: %s' % self.path)


def load_chronos_config(path=PATH_TO_CHRONOS_CONFIG):
    try:
        with open(path) as f:
            return ChronosConfig(json.load(f), path)
    except IOError as e:
        raise ChronosNotConfigured("Could not load chronos config file %s: %s" % (e.filename, e.strerror))


def get_chronos_client(config):
    """Returns a chronos client object for interacting with the API"""
    chronos_url = config.get_url()[0]
    chronos_hostname = urlparse.urlsplit(chronos_url).netloc
    log.info("Connecting to Chronos server at: %s", chronos_url)
    return chronos.connect(hostname=chronos_hostname,
                           username=config.get_username(),
                           password=config.get_password())


def get_job_id(service, instance, tag=None):
    output = "%s%s%s" % (service, SPACER, instance)
    if tag:
        output = "%s%s%s%s%s" % (service, SPACER, instance, SPACER, tag)
    return output


class InvalidChronosConfigError(Exception):
    pass


def read_chronos_jobs_for_service(service_name, cluster, soa_dir=DEFAULT_SOA_DIR):
    chronos_conf_file = 'chronos-%s' % cluster
    log.info("Reading Chronos configuration file: %s/%s/chronos-%s.yaml", (soa_dir, service_name, cluster))

    return service_configuration_lib.read_extra_service_information(
        service_name,
        chronos_conf_file,
        soa_dir=soa_dir
    )


def load_chronos_job_config(service_name, job_name, cluster, soa_dir=DEFAULT_SOA_DIR):
    service_chronos_jobs = read_chronos_jobs_for_service(service_name, cluster, soa_dir=soa_dir)

    if job_name not in service_chronos_jobs:
        raise InvalidChronosConfigError('No job named "%s" in config file chronos-%s.yaml' % (job_name, cluster))

    deployments_json = load_deployments_json(service_name, soa_dir=soa_dir)
    branch = get_default_branch(cluster, job_name)
    branch_dict = deployments_json.get_branch_dict(service_name, branch)

    return ChronosJobConfig(service_name, job_name, service_chronos_jobs[job_name], branch_dict)


class ChronosJobConfig(InstanceConfig):

    def __init__(self, service_name, job_name, config_dict, branch_dict):
        super(ChronosJobConfig, self).__init__(config_dict, branch_dict)
        self.service_name = service_name
        self.job_name = job_name
        self.config_dict = config_dict
        self.branch_dict = branch_dict

    def __eq__(self, other):
        return ((self.service_name == other.service_name)
                and (self.job_name == other.job_name)
                and (self.config_dict == other.config_dict)
                and (self.branch_dict == other.branch_dict))

    def get_service_name(self):
        return self.service_name

    def get_job_name(self):
        return self.job_name

    def get_owner(self):
        return monitoring_tools.get_team_email_address(self.get_service_name(), overrides=self.get_monitoring())

    def get_args(self):
        return self.config_dict.get('args')

    def get_env(self):
        return self.config_dict.get('env', [])

    def get_constraints(self):
        return self.config_dict.get('constraints')

    def get_epsilon(self):
        return self.config_dict.get('epsilon', 'PT60S')

    def get_retries(self):
        return self.config_dict.get('retries', 2)

    def get_disabled(self):
        return self.config_dict.get('disabled', False)

    def get_schedule(self):
        return self.config_dict.get('schedule')

    def get_schedule_time_zone(self):
        return self.config_dict.get('schedule_time_zone')

    def check_epsilon(self):
        epsilon = self.get_epsilon()
        try:
            isodate.parse_duration(epsilon)
        except isodate.ISO8601Error:
            return False, 'The specified epsilon value "%s" does not conform to the ISO8601 format.' % epsilon
        return True, ''

    def check_retries(self):
        retries = self.get_retries()
        if retries is not None:
            if not isinstance(retries, int):
                return False, 'The specified retries value "%s" is not a valid int.' % retries
        return True, ''

    # a valid 'repeat_string' is 'R' or 'Rn', where n is a positive integer representing the number of times to repeat
    # more info: https://en.wikipedia.org/wiki/ISO_8601#Repeating_intervals
    def _check_schedule_repeat_helper(self, repeat_string):
        pattern = re.compile('^R\d*$')
        return pattern.match(repeat_string) is not None

    def check_schedule(self):
        msgs = []
        schedule = self.get_schedule()

        if schedule is not None:
            try:
                repeat, start_time, interval = str.split(schedule, '/')  # the parts have separate validators
            except ValueError:
                return False, 'The specified schedule "%s" is invalid' % schedule

            # an empty start time is not valid ISO8601 but Chronos accepts it: '' == current time
            if start_time == '':
                msgs.append('The specified schedule "%s" does not contain a start time' % schedule)
            else:
                # Check if start time contains time zone information
                try:
                    dt = isodate.parse_datetime(start_time)
                    if not hasattr(dt, 'tzinfo'):
                        msgs.append('The specified start time "%s" must contain a time zone' % start_time)
                except isodate.ISO8601Error as exc:
                    msgs.append('The specified start time "%s" in schedule "%s" '
                                'does not conform to the ISO 8601 format:\n%s' % (start_time, schedule, str(exc)))

            try:
                isodate.parse_duration(interval)  # 'interval' and 'duration' are interchangeable terms
            except isodate.ISO8601Error:
                msgs.append('The specified interval "%s" in schedule "%s" '
                            'does not conform to the ISO 8601 format.' % (interval, schedule))

            if not self._check_schedule_repeat_helper(repeat):
                msgs.append('The specified repeat "%s" in schedule "%s" '
                            'does not conform to the ISO 8601 format.' % (repeat, schedule))
        else:
            msgs.append('You must specify a "schedule" in your configuration')

        return len(msgs) == 0, '\n'.join(msgs)

    # TODO we should use pytz for best possible tz info and validation
    # TODO if tz specified in start_time, compare to the schedule_time_zone and warn if they differ
    # TODO if tz not specified in start_time, set it to time_zone
    # NOTE confusingly, the accepted time zone format for 'schedule_time_zone' is different than in 'schedule'!
    # 'schedule_time_zone': tz database format (https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
    # 'schedule': ISO 8601 format (https://en.wikipedia.org/wiki/ISO_8601#Time_zone_designators)
    # TODO maybe we don't even want to support this a a separate parameter? instead require it to be specified
    # as a component of the 'schedule' parameter?
    def check_schedule_time_zone(self):
        time_zone = self.get_schedule_time_zone()
        if time_zone is not None:
            return True, ''
            # try:
            # TODO validate tz format
            # except isodate.ISO8601Error as exc:
            #     return False, ('The specified time zone "%s" does not conform to the tz database format:\n%s'
            #                    % (time_zone, str(exc)))
        return True, ''

    def check(self, param):
        check_methods = {
            'epsilon': self.check_epsilon,
            'retries': self.check_retries,
            'cpus': self.check_cpus,
            'mem': self.check_mem,
            'schedule': self.check_schedule,
            'scheduleTimeZone': self.check_schedule_time_zone,
        }
        supported_params_without_checks = ['description', 'command', 'owner', 'disabled']
        if param in check_methods:
            return check_methods[param]()
        elif param in supported_params_without_checks:
            return True, ''
        else:
            return False, 'Your Chronos config specifies "%s", an unsupported parameter.' % param

    def format_chronos_job_dict(self, docker_url, docker_volumes):

        valid, error_msgs = self.validate()
        if not valid:
            raise InvalidChronosConfigError("\n".join(error_msgs))

        complete_config = {
            'name': self.get_job_name(),
            'container': {
                'image': docker_url,
                'network': 'BRIDGE',
                'type': 'DOCKER',
                'volumes': docker_volumes
            },
            'environmentVariables': self.get_env(),
            'mem': self.get_mem(),
            'cpus': self.get_cpus(),
            'constraints': self.get_constraints(),
            'command': self.get_cmd(),
            'arguments': self.get_args(),
            'epsilon': self.get_epsilon(),
            'retries': self.get_retries(),
            'async': False,  # we don't support async jobs
            'disabled': self.get_disabled(),
            'owner': self.get_owner(),
            'schedule': self.get_schedule(),
            'scheduleTimeZone': self.get_schedule_time_zone(),
        }
        log.info("Complete configuration for instance is: %s", complete_config)
        return complete_config

    # 'docker job' requirements: https://mesos.github.io/chronos/docs/api.html#adding-a-docker-job
    def validate(self):
        error_msgs = []
        # Use InstanceConfig to validate shared config keys like cpus and mem
        error_msgs.extend(super(ChronosJobConfig, self).validate())

        for param in ['epsilon', 'retries', 'cpus', 'mem', 'schedule', 'scheduleTimeZone']:
            check_passed, check_msg = self.check(param)
            if not check_passed:
                error_msgs.append(check_msg)

        return len(error_msgs) == 0, error_msgs


def list_job_names(service_name, cluster=None, soa_dir=DEFAULT_SOA_DIR):
    """Enumerate the Chronos jobs defined for a service as a list of tuples.

    :param name: The service name
    :param cluster: The cluster to read the configuration for
    :param soa_dir: The SOA config directory to read from
    :returns: A list of tuples of (name, job) for each job defined for the service name"""
    job_list = []
    if not cluster:
        cluster = load_system_paasta_config().get_cluster()
    chronos_conf_file = "chronos-%s" % cluster
    log.info("Enumerating all jobs from config file: %s/*/%s.yaml", soa_dir, chronos_conf_file)

    for job in read_chronos_jobs_for_service(service_name, cluster, soa_dir=soa_dir):
        job_list.append((service_name, job))
    log.debug("Enumerated the following jobs: %s", job_list)
    return job_list


def get_chronos_jobs_for_cluster(cluster=None, soa_dir=DEFAULT_SOA_DIR):
    """Retrieve all Chronos jobs defined to run on a cluster.

    :param cluster: The cluster to read the configuration for
    :param soa_dir: The SOA config directory to read from
    :returns: A list of tuples of (service_name, job_name)"""
    if not cluster:
        cluster = load_system_paasta_config().get_cluster()
    rootdir = os.path.abspath(soa_dir)
    log.info("Retrieving all Chronos job names from %s for cluster %s", rootdir, cluster)
    job_list = []
    for service in os.listdir(rootdir):
        job_list.extend(list_job_names(service, cluster, soa_dir))
    return job_list


def create_complete_config(service, job_name, soa_dir=DEFAULT_SOA_DIR):
    """Generates a complete dictionary to be POST'ed to create a job on Chronos"""
    system_paasta_config = load_system_paasta_config()
    chronos_job_config = load_chronos_job_config(
        service, job_name, system_paasta_config.get_cluster(), soa_dir=soa_dir)
    docker_url = get_docker_url(
        system_paasta_config.get_docker_registry(), chronos_job_config.get_docker_image())

    complete_config = chronos_job_config.format_chronos_job_dict(
        docker_url,
        system_paasta_config.get_volumes(),
    )
    code_sha = get_code_sha_from_dockerurl(docker_url)
    config_hash = get_config_hash(complete_config)
    tag = "%s%s%s" % (code_sha, SPACER, config_hash)

    # Chronos clears the history for a job whenever it is updated, so we use a new job name for each revision
    # so that we can keep history of old job revisions rather than just the latest version
    full_id = get_job_id(service, job_name, tag)
    complete_config['name'] = full_id
    desired_state = chronos_job_config.get_desired_state()

    # If the job was previously stopped, we should stop the new job as well
    if desired_state == 'start':
        complete_config['disabled'] = False
    elif desired_state == 'stop':
        complete_config['disabled'] = True

    return complete_config


def lookup_chronos_jobs(pattern, client, max_expected=None, include_disabled=False):
    """Retrieves Chronos jobs with names that match a specified pattern.

    :param pattern: a Python style regular expression that the job name will be matched against
                    (after being passed to re.compile)
    :param client: Chronos client object
    :param max_expected: maximum number of results that is expected. If exceeded, raises a ValueError
    :param include_disabled: boolean indicating if disabled jobs should be included in matches
    """
    try:
        regexp = re.compile(pattern)
    except re.error:
        raise ValueError("Invalid regex pattern '%s'" % pattern)
    jobs = client.list()
    matching_jobs = []
    for job in jobs:
        if regexp.search(job['name']):
            if job['disabled'] and not include_disabled:
                continue
            else:
                matching_jobs.append(job)

    if max_expected and len(matching_jobs) > max_expected:
        matching_ids = [job['name'] for job in matching_jobs]
        raise ValueError("Found %d jobs for pattern '%s', but max_expected is set to %d (ids: %s)" %
                         (len(matching_jobs), pattern, max_expected, ', '.join(matching_ids)))

    return matching_jobs